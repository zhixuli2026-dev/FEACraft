"""Exercise a shared audit chain with real APIs and committed native jobs."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine
from websockets.sync.client import connect

from live_llm_terminal_agent_acceptance import disposable_database
from testsupport import IsolatedServer, SERVER


def configure(api, primary, jwt, dist):
    env = {'VIBEFLOW_JWT_SECRET': jwt, 'VIBEFLOW_STORAGE_FS_ROOT': str(primary.root/'files')}
    if api.database_url is None:
        env['VIBEFLOW_DATABASE_URL'] = 'sqlite+aiosqlite:///' + (primary.root/'test.sqlite').as_posix()
    fixture = 'import os\nos.environ.update('+repr(env)+')\nfrom app.main import app\n'
    if dist:
        fixture += ('from fastapi.staticfiles import StaticFiles\n'
                    f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n')
    (api.root/'org_audit_fixture.py').write_text(fixture, encoding='utf8')


def legacy(api, org, user):
    root = api.root/'audit'
    root.mkdir(exist_ok=True)
    event = {'schema': 'vibeflow.audit.v1', 'event_id': uuid.uuid4().hex, 'org_id': org,
             'actor_user_id': user, 'action': 'runtime.config.updated',
             'resource_type': 'runtime_config', 'resource_id': 'global',
             'detail': {'keys': 'pandoc'}, 'previous_hash': '',
             'created_at': datetime.now(timezone.utc).isoformat()}
    event['event_hash'] = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False,
                                                   separators=(',', ':')).encode()).hexdigest()
    (root/f'org-{org}.jsonl').write_text(json.dumps(event)+'\n', encoding='utf8')
    return event


async def replace_first(database, org, payload=None):
    from app.audit_models import OrganizationAuditRecord as Record

    engine = create_async_engine(database)
    try:
        async with engine.begin() as db:
            where = (Record.org_id == org, Record.sequence == 1)
            original = await db.scalar(select(Record.payload).where(*where))
            if payload is None:
                event = json.loads(original)
                event['detail']['keys'] = 'isolated integrity check'
                payload = json.dumps(event)
            await db.execute(update(Record).where(*where).values(payload=payload))
            return original
    finally:
        await engine.dispose()


def exercise(primary, peer, report):
    case = primary.create_case('Audit chain validation')
    peer.token = primary.token
    user = primary.call('GET', '/auth/me')['id']
    org = primary.call('GET', '/org/mine')['org']['id']
    path = f'/org/{org}/audit'
    archive = legacy(primary, org, user)
    assert primary.call('GET', path)['events'] == [archive]
    member = peer.call('POST', '/auth/register', {'email': 'isolated-audit-member@example.com',
        'password': 'isolated-pass-001', 'name': 'Audit member'})
    member_headers = {'Authorization': 'Bearer '+member['token']}
    peer.call('GET', path, expect=403, extra_headers=member_headers)
    primary.call('POST', f'/org/{org}/members', {'email': member['user']['email'], 'role': 'member'}, expect=201)
    peer.call('POST', f'/org/{org}/invitation:accept', {}, extra_headers=member_headers)
    assert peer.call('GET', path+'?actor_user_id='+str(member['user']['id']))['total'] == 1

    # Create conversations serially, then write settings concurrently from both APIs.
    ids = [primary.call('POST', '/conversations', {'case_id': case['id'], 'title': 'Decision '+str(i)})['id']
           for i in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        writes = [pool.submit(api.call, 'PATCH', f'/conversations/{cid}/permission', {'permission_mode': 'plan'})
                  for api, cid in zip((primary, peer)*4, ids)]
        assert all(future.result()['permission_mode'] == 'plan' for future in writes)

    canary = 'echo QUOTA_AUDIT_COMMAND_CANARY'
    job = primary.call('POST', primary.prefix+'/jobs', {'type': 'shell', 'command': canary},
                       extra_headers={'Idempotency-Key': 'audit-real-command-001'})
    repeated = peer.call('POST', primary.prefix+'/jobs', {'type': 'shell', 'command': canary},
                         extra_headers={'Idempotency-Key': 'audit-real-command-001'})
    assert job['id'] == repeated['id']
    assert 'QUOTA_AUDIT_COMMAND_CANARY' in primary.wait_job(job['id'])['log_backlog']
    running = primary.call('POST', primary.prefix+'/jobs',
        {'type': 'shell', 'command': 'echo AUDIT_CANCEL_READY & ping -n 45 127.0.0.1'})
    deadline = time.monotonic()+15
    while 'AUDIT_CANCEL_READY' not in primary.call('GET', f'/jobs/{running["id"]}')['log_backlog']:
        assert time.monotonic() < deadline
        time.sleep(.15)
    assert peer.call('POST', f'/jobs/{running["id"]}:cancel')['ok']
    deadline = time.monotonic()+15
    while primary.call('GET', f'/jobs/{running["id"]}')['attempt']['phase'] != 'cancelled':
        assert time.monotonic() < deadline
        time.sleep(.15)

    ticket = peer.call('POST', '/connection-tickets', {'scope':'terminal','case_id':case['id']})['ticket']
    url = peer.base.replace('http:', 'ws:', 1)+'/ws/terminal?'+urlencode({'case':case['id'],'token':ticket})
    with connect(url, open_timeout=25) as connection:
        ready = json.loads(connection.recv(timeout=15))
        assert ready['type'] == 'ready', ready
        terminal = '/api/v1/terminal/sessions/'+ready['session']
        assert peer.call('DELETE', terminal, api_prefix=False)['ok']
        closed = peer.call('GET', terminal, api_prefix=False)
        assert closed['phase'] == 'closed' and closed['cleanup_verified'], closed['phase']

    before = primary.call('GET', path+'?limit=100')
    assert before == peer.call('GET', path+'?limit=100') and before['chain_valid']
    assert before['total'] == before['head_sequence'] == 16, before['total']
    assert before['events'][-1] == archive and before['events'][0]['event_hash'] == before['head_hash']
    assert canary not in json.dumps(before)
    job_events = [event for event in before['events'] if event['action'] == 'job.submitted']
    completed = next(event for event in job_events if event['resource_id'] == str(job['id']))
    assert completed['detail']['command_sha256'] == hashlib.sha256(canary.encode()).hexdigest()
    assert {'job.cancel.requested','terminal.launch.admitted','terminal.close.requested'} <= {
        event['action'] for event in before['events']}
    assert primary.call('GET', path+'?limit=2&offset=2')['events'] == before['events'][2:4]
    database = primary.database_url or 'sqlite+aiosqlite:///'+(primary.root/'test.sqlite').as_posix()
    original = asyncio.run(replace_first(database, org))
    altered = peer.call('GET', path+'?action=job.submitted')
    assert altered['total'] == 2 and altered['chain_valid'] is False
    asyncio.run(replace_first(database, org, original))
    assert primary.call('GET', path+'?limit=100') == before
    report.update(head_sequence=before['head_sequence'], head_hash=before['head_hash'],
                  identical_replicas=True, legacy_import=True, old_record_tamper_detected=True,
                  command_hash_only=True, job_idempotent=True, native_job_cancelled=True, terminal_closed=True)
    return path, before


async def verify_pinned(client, bucket, receipt, database):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.audit_checkpoints import verify

    engine = create_async_engine(database)
    try:
        outcome = await verify(client, bucket, 'isolated-audit', receipt, async_sessionmaker(engine))
        assert outcome['verified'] and outcome['version_id'] == receipt['version_id']
        original = await replace_first(database, receipt['org_id'])
        try:
            try:
                await verify(client, bucket, 'isolated-audit', receipt, async_sessionmaker(engine))
            except ValueError:
                pass
            else:
                raise AssertionError('Altered database was accepted against the pinned version')
        finally:
            await replace_first(database, receipt['org_id'], original)
    finally:
        await engine.dispose()


def verify_checkpoint(page, database):
    from botocore.exceptions import ClientError

    from app.services.audit_checkpoints import store
    from s3testsupport import ObjectStore

    executable = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    with ObjectStore(executable=executable, authenticated=True) as external:
        client = external.raw_client
        bucket = 'test-audit-checkpoint-'+uuid.uuid4().hex
        client.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
        args = (client, bucket, 'isolated-audit', page['org_id'], page['head_sequence'])
        receipt = store(*args, page['head_hash'], 1)
        repeat = store(*args, page['head_hash'], 1)
        assert receipt['created'] is True and repeat['created'] is False
        assert receipt['version_id'] == repeat['version_id']
        try:
            store(*args, 'a'*64, 1)
        except ValueError:
            pass
        else:
            raise AssertionError('Different digest reused the same anchor position')
        try:
            client.delete_object(Bucket=bucket, Key=receipt['key'], VersionId=receipt['version_id'])
        except ClientError as error:
            status = error.response['ResponseMetadata']['HTTPStatusCode']
            assert status == 403, status
        else:
            raise AssertionError('Version deletion was accepted before retention expired')
        assert store(*args, page['head_hash'], 1)['version_id'] == receipt['version_id']
        asyncio.run(verify_pinned(client, bucket, receipt, database))
    return {'revision': receipt['sequence'], 'checksum': receipt['chain_sha256'],
            'retained_version': receipt['version_id'], 'retained_until': receipt['retain_until'],
            'delete_status': status, 'pinned_verification': True, 'store_exited': external.proc.poll() is not None}


BROWSER = '''
import json, sys
from playwright.sync_api import expect, sync_playwright

settings = json.load(sys.stdin)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel='msedge', headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.add_init_script('localStorage.setItem("vf_token", '+json.dumps(settings['token'])+');')
    page.goto(settings['base']+'/', wait_until='domcontentloaded')
    page.locator('.dv-tab').filter(has_text='设置').first.click(timeout=30000)
    page.get_by_test_id('org-audit-load').click(timeout=30000)
    audit = page.get_by_test_id('org-audit')
    expect(audit).to_contain_text('哈希链有效')
    expect(audit).to_contain_text('16 条')
    expect(audit).to_contain_text('terminal.close.requested')
    page.get_by_label('审计动作过滤').select_option('job.submitted')
    expect(audit.get_by_test_id('org-audit-event')).to_have_count(2)
    for row in audit.get_by_test_id('org-audit-event').all():
        expect(row).to_contain_text('job.submitted')
        assert 'command_sha256' in row.get_attribute('title')
        assert 'AUDIT_COMMAND_CANARY' not in row.get_attribute('title')
    audit.screenshot(path=settings['screenshot'])
    browser.close()
'''


def run(database, args):
    primary = IsolatedServer('org-audit-primary', app='org_audit_fixture:app', database_url=database)
    peer = IsolatedServer('org-audit-peer', app='org_audit_fixture:app', database_url=database)
    jwt = uuid.uuid4().hex
    configure(primary, primary, jwt, args.dist)
    configure(peer, primary, jwt, args.dist)
    report = {'passed': False, 'database': 'postgresql' if database else 'sqlite', 'model_calls': 0}
    print('ORGANIZATION AUDIT EVIDENCE', primary.root, peer.root, flush=True)
    try:
        with primary, peer:
            path, expected = exercise(primary, peer, report)
        with peer:
            assert peer.call('GET', path+'?limit=100') == expected
            if args.browser_python:
                screenshot = primary.root/'audit-center.png'
                payload = {'base': peer.base, 'token': peer.token, 'screenshot': str(screenshot)}
                subprocess.run([str(args.browser_python), '-X', 'utf8', '-c', BROWSER], input=json.dumps(payload),
                               text=True, timeout=75, check=True)
                report['edge_audit_center'] = screenshot.name
        if args.checkpoint:
            url = database or 'sqlite+aiosqlite:///'+(primary.root/'test.sqlite').as_posix()
            report['checkpoint'] = verify_checkpoint(expected, url)
        report['passed'] = True
        report['peer_restart_verified'] = True
    finally:
        (primary.root/'organization-audit.json').write_text(json.dumps(report, indent=2), encoding='utf8')


async def main(args):
    async with disposable_database(args.postgres) as database:
        await asyncio.to_thread(run, database, args)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--checkpoint', action='store_true')
    parser.add_argument('--dist', type=lambda path: Path(path).resolve(strict=True))
    parser.add_argument('--browser-python', type=Path)
    args = parser.parse_args()
    if bool(args.dist) != bool(args.browser_python):
        parser.error('--dist and --browser-python must be supplied together')
    asyncio.run(main(args))
