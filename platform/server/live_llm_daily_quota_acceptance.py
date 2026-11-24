"""Verify subscription expiry and shared admissions against official DeepSeek."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urlencode
import uuid

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from websockets.sync.client import connect

from app.models import AgentEvent, ModelQuotaDay, Plan, Subscription
from app.services.tools import goal_payload
from live_llm_acceptance import FIXTURE, source
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from live_llm_terminal_agent_acceptance import disposable_database
from testsupport import IsolatedServer


def configure(api, primary):
    shared = dict(
        VIBEFLOW_STORAGE_FS_ROOT=str(primary.root / 'files'),
        VIBEFLOW_JWT_SECRET=primary.env['VIBEFLOW_JWT_SECRET'] if api is not primary else uuid.uuid4().hex,
        VIBEFLOW_RATE_LIMIT_BACKEND='memory',
    )
    if primary.database_url is None:
        shared['VIBEFLOW_DATABASE_URL'] = 'sqlite+aiosqlite:///' + (primary.root / 'test.sqlite').as_posix()
    fixture = 'import os\nos.environ.update(' + repr(shared) + ')\nlive_request_limit=3\n' + FIXTURE
    fixture += '\nfrom app.services.process_identity_native import observe\n'
    fixture += "(root/'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')\n"
    fixture += '\nllm._VAULT_KEY_PATH=Path(' + repr(str(primary.root / '.quota-vault')) + ')\n'
    (api.root / 'quota_fixture.py').write_text(fixture, encoding='utf8')
    return shared['VIBEFLOW_JWT_SECRET']


async def set_limits(db, code, allowed):
    quotas = {'viz_concurrent': allowed, 'job_concurrent': allowed, 'storage_mb': 32 if allowed else 0,
              'llm_msgs_per_day': 10 if code == 'pro' else 4, 'seats': 2}
    await db.execute(update(Plan).where(Plan.code == code).values(quotas=json.dumps(quotas)))


def request_translation(base, headers, profile_id):
    return httpx.post(base + '/api/v1/llm/translate', headers=headers, timeout=65, trust_env=False,
                     json={'text': 'The inlet velocity is 7 metres per second.', 'target': 'zh',
                           'provider_profile_id': profile_id})


def record_history(db, cid, user_messages, *, imported=False):
    for seq, message in enumerate(user_messages, 1):
        payload = {'role': 'user', 'content': message}
        if imported or seq > 1:
            payload['history_origin'] = 'import'
        db.add(AgentEvent(conversation_id=cid, seq=seq, event_id=uuid.uuid4().hex,
                          type='message', payload=json.dumps(payload)))


def mutate(api, operation):
    async def commit():
        engine = create_async_engine(api.env['VIBEFLOW_DATABASE_URL'])
        try:
            async with async_sessionmaker(engine).begin() as db:
                await operation(db)
        finally:
            await engine.dispose()
    asyncio.run(commit())


def verify(primary, peer, config, report):
    case = primary.create_case('Shared daily model quota')
    peer.token = primary.token
    org_id = primary.call('GET', '/org/mine')['org']['id']
    primary.call('GET', '/org/plans')
    profile = primary.call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'Official DeepSeek',
        'base_url': config['base_url'], 'default_model': config['model'], 'api_key': config['key']})['id']
    primary.put('E-report/report.md', '# Quota report\n\nExplicit test evidence.\n')
    primary.put('Ref/keep.txt', 'KEEP_REFERENCE')
    first = primary.call('POST', '/conversations', {'case_id': case['id'], 'title': 'Quota conversation',
        'provider_profile_id': profile, 'permission_mode': 'ask'})['id']
    fresh = primary.call('POST', '/conversations', {'case_id': case['id'], 'title': 'Quota rejection',
        'provider_profile_id': profile, 'permission_mode': 'ask'})['id']
    point = datetime.now(timezone.utc)
    user_id = primary.call('GET', '/auth/me')['id']

    async def seed(db):
        await set_limits(db, 'pro', 4)
        await set_limits(db, 'free', 0)
        await db.execute(update(Subscription).where(Subscription.org_id == org_id).values(status='superseded'))
        plan_id = await db.scalar(select(Plan.id).where(Plan.code == 'pro'))
        db.add(Subscription(org_id=org_id, plan_id=plan_id, status='active', expires_at=point+timedelta(hours=1)))
        db.add(ModelQuotaDay(user_id=user_id, day=point.date()-timedelta(days=1), kind='translation', used=40))
        record_history(db, first, ['Historical inlet evidence QCHECK193.', 'Imported description of the mesh.'])
        record_history(db, fresh, ['Archived mesh', 'Imported log', 'Imported solver settings'], imported=True)
        db.add(AgentEvent(conversation_id=fresh, seq=4, event_id=uuid.uuid4().hex, type='goal',
                          payload=json.dumps(goal_payload({'objective': 'Check conservation'}))))
    mutate(primary, seed)
    before = primary.call('GET', '/org/mine')
    assert before['plan']['code'] == 'pro' and before['usage']['llm_msgs_today'] == 1, before

    headers = {'Authorization': 'Bearer ' + primary.token}
    chat = {'content': '请用一句中文解释有限体积法为什么要求通量守恒。', 'provider_profile_id': profile}
    raw = primary.call('POST', f'/conversations/{first}/messages', chat, raw=True).decode()
    assert any(json.loads(line[5:]).get('type') == 'run.completed' for line in raw.splitlines() if line.startswith('data:'))
    summary = peer.call('POST', f'/conversations/{first}/compress', {'provider_profile_id': profile, 'keep_last': 2})
    assert summary['ok'] and summary['summary'].strip()
    async def expire(db):
        await db.execute(update(Subscription).where(Subscription.org_id == org_id, Subscription.status == 'active')
                         .values(expires_at=point-timedelta(seconds=1)))
    mutate(primary, expire)

    fallback = peer.call('GET', '/org/mine')
    assert fallback['plan']['code'] == 'free' and fallback['usage']['llm_msgs_today'] == 3, fallback
    assert fallback['usage']['quotas'] == {'viz_concurrent': 0, 'job_concurrent': 0, 'storage_mb': 0,
                                             'llm_msgs_per_day': 4, 'seats': 2}
    assert fallback['usage']['llm_resets_at'].endswith('00:00:00+00:00')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request_translation, api.base, headers, profile) for api in (primary, peer)]
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 429], [r.status_code for r in responses]
    assert '7' in next(r.json()['translation'] for r in responses if r.status_code == 200)

    denied = [
        ('POST', f'/conversations/{fresh}/messages', chat),
        ('POST', f'/conversations/{fresh}/compress', {'provider_profile_id': profile, 'keep_last': 2}),
        ('POST', f'{primary.prefix}/jobs', {'type': 'shell', 'command': 'echo QUOTA_DENIED'}),
        ('POST', f'{primary.prefix}/report:build', None),
        ('POST', f'{primary.prefix}/viz:sessions', {'run_dir': 'C-run/case-1'}),
        ('PUT', f'{primary.prefix}/file', {'path': 'Ref/keep.txt', 'content': 'QUOTA_MUST_NOT_REPLACE'}),
    ]
    for method, path, body in denied:
        refused = peer.call(method, path, body, expect=429)
        if path.startswith('/conversations/'):
            assert refused['detail']['code'] == 'model_quota_exceeded', refused
    goal = peer.call('POST', f'/conversations/{fresh}/goal:run', {'max_turns': 1}, raw=True).decode()
    frames = [json.loads(line[5:]) for line in goal.splitlines() if line.startswith('data:')]
    assert any(frame.get('status') == 429 and frame.get('code') == 'model_quota_exceeded' for frame in frames)
    assert any(frame.get('goal', {}).get('status') == 'blocked' for frame in frames)
    assert primary.call('GET', primary.prefix + '/file?path=Ref/keep.txt')['content'] == 'KEEP_REFERENCE'
    assert primary.call('GET', primary.prefix + '/viz:sessions') == []
    final_usage = primary.call('GET', '/org/mine')['usage']
    assert final_usage['llm_msgs_today'] == 4
    assert final_usage['llm_requests_by_kind'] == {'legacy': 1, 'agent': 1, 'compression': 1, 'translation': 1}

    mutate(primary, lambda db: set_limits(db, 'free', 2))
    ticket = peer.call('POST', '/connection-tickets', {'scope': 'terminal', 'case_id': case['id']})['ticket']
    query = urlencode({'case': case['id'], 'token': ticket, 'provider_profile_id': profile, 'cli_harness': 'codex'})
    with connect(peer.base.replace('http:', 'ws:', 1) + '/ws/terminal?' + query, open_timeout=15) as ws:
        refused_terminal = json.loads(ws.recv(timeout=10))
        assert refused_terminal['status'] == 429 and refused_terminal['code'] == 'model_quota_exceeded', refused_terminal
    job = primary.call('POST', primary.prefix + '/jobs', {'type': 'shell', 'command': 'echo QUOTA_RESTORED'})
    assert 'QUOTA_RESTORED' in primary.wait_job(job['id'])['log_backlog']
    report.update(expired_plan=fallback['plan']['code'], quota=final_usage['quotas'],
                  model_by_kind=final_usage['llm_requests_by_kind'], denied_routes=[path for _, path, _ in denied],
                  translation_statuses=[response.status_code for response in responses], real_job_restored=True,
                  goal_refused=True, credentialed_terminal_refused=True)


def run(config, database):
    primary = IsolatedServer('live-j08-quota-primary', app='quota_fixture:app', database_url=database)
    peer = IsolatedServer('live-j08-quota-peer', app='quota_fixture:app', database_url=database)
    report = {'passed': False, 'simulated_llm': False, 'model': config['model'],
              'backend': 'postgresql' if database else 'sqlite'}
    shared_secret = configure(primary, primary)
    print('J08 EVIDENCE', primary.root, peer.root, flush=True)
    try:
        with primary:
            primary.env['VIBEFLOW_JWT_SECRET'] = shared_secret
            configure(peer, primary)
            with peer:
                verify(primary, peer, config, report)
        report['receipts'] = [json.loads(line) for api in (primary, peer)
                              for line in (api.root / 'upstream.jsonl').read_text(encoding='utf8').splitlines()]
        assert len([r for r in report['receipts'] if r['kind'] == 'http' and r['status'] == 200]) == 3
        report['api_exits'] = [wait_for_api_exit(api.root, api.port) for api in (primary, peer)]
        report['passed'] = True
    finally:
        (primary.root / '.quota-vault').unlink(missing_ok=True)
        (primary.root / 'quota-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')


async def main(args):
    config = await source(args.source_profile_id)
    async with disposable_database(args.postgres) as database:
        await asyncio.to_thread(run, config, database)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', required=True, type=int)
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args))
