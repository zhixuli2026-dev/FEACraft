"""Two actual API workers share durable CLI/API ownership on disposable stores."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic
from codex_turn_api_smoke import CONTROLLED, WORKER
from codex_session_smoke import wait_for
from app.services import process_identity_native as identity

FIXTURE = CONTROLLED + '''
from app.models import ToolCall
@app.post('/api/v1/fixture/startup-markers/{cid}')
async def markers(cid:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        db.add(ToolCall(id=__import__('uuid').uuid4().hex,conversation_id=cid,tool='exec',args='{}',status='running'))
        rows=(await db.scalars(select(AgentEvent).where(AgentEvent.conversation_id==cid,AgentEvent.type=='run_state').order_by(AgentEvent.id.desc()))).all()
        rid=json.loads(rows[0].payload)['run_id']
        await agent._append_request_state(db,cid,rid,'owner-fixture-request','started')
        await db.commit();return {'ok':True}
@app.get('/api/v1/fixture/owner-state/{cid}')
async def owner_state(cid:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        events=(await db.scalars(select(AgentEvent).where(AgentEvent.conversation_id==cid).order_by(AgentEvent.id))).all()
        tools=(await db.scalars(select(ToolCall).where(ToolCall.conversation_id==cid))).all()
        owner=await db.get(ConversationExecution,cid)
        return {'events':[{'type':row.type,'payload':json.loads(row.payload)} for row in events],
            'tools':[row.status for row in tools],'control':owner.phase}
'''


def run_database(url, folder):
    folder.mkdir(parents=True)
    if url is not None:
        alembic(url, folder, 'upgrade', 'head')
    api = IsolatedServer('cli-turn-owner', app='owner_fixture:app', database_url=url)
    (api.root / 'cli.py').write_text(WORKER, encoding='utf8')
    (api.root / 'owner_fixture.py').write_text(FIXTURE, encoding='utf8')
    checks, receipts = [], []
    with api:
        case = api.create_case('Two API workers')
        provider = api.call('POST', '/fixture/profile')['id']
        def create():
            conv = api.call('POST', '/conversations', {'case_id': case['id'], 'permission_mode': 'approve_writes'})
            session = api.call('POST', '/codex/sessions', {'case_id': case['id'], 'conversation_id': conv['id'],
                'provider_profile_id': provider, 'provider_wire_api': 'responses'})
            return conv, session
        conv, session = create()
        body = {'content': 'normal', 'client_message_id': uuid.uuid4().hex}
        complete = api.call('POST', '/codex/sessions/' + session['id'] + '/turns', body)
        wait_for(lambda: api.call('GET', '/codex/turns/' + complete['id']), lambda row: row['phase'] == 'completed')
        pending_conv, pending_session = create()
        pending_body = {'content': 'command', 'client_message_id': uuid.uuid4().hex}
        turn = api.call('POST', '/codex/sessions/' + pending_session['id'] + '/turns', pending_body)
        turn_path = '/codex/turns/' + turn['id']
        approval = wait_for(lambda: api.call('GET', turn_path + '/approvals'), bool)[0]
        api_conv = api.call('POST', '/conversations', {'case_id': case['id']})
        with ThreadPoolExecutor(1) as pool:
            operation = pool.submit(api.call, 'POST', '/conversations/' + str(api_conv['id']) + '/messages',
                {'content': 'Held API fixture.', 'provider_profile_id': provider}, raw=True)
            wait_for(lambda: (api.root / ('api-held-' + str(api_conv['id']))).exists(), bool)
            api.call('POST', '/fixture/startup-markers/' + str(api_conv['id']))
            second = IsolatedServer('owner-client')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            second.port, second.base, second.token = port, 'http://127.0.0.1:' + str(port), api.token
            log = (api.root / 'second-api.log').open('w', encoding='utf8')
            def spawn():
                return subprocess.Popen([sys.executable, '-m', 'uvicorn', api.app, '--app-dir', str(SERVER),
                    '--host', '127.0.0.1', '--port', str(port), '--no-access-log'], cwd=api.root, env=api.env,
                    stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            def healthy():
                try:
                    return second.call('GET', '/api/health', api_prefix=False).get('ok')
                except OSError:
                    return False
            proc = spawn()
            try:
                wait_for(healthy, bool)
                state = second.call('GET', '/fixture/owner-state/' + str(api_conv['id']))
                assert state['tools'] == ['running'], state
                for event_type in ('run_state', 'request_state'):
                    assert [e['payload']['status'] for e in state['events'] if e['type'] == event_type][-1] == 'started', state
                checks.append('second_worker_startup_preserves_live_api_run_and_tool_history')
                durable = second.call('GET', '/codex/turns/' + complete['id'])
                assert durable['phase'] == 'completed' and durable['cleanup_verified']
                checks.append('completed_native_history_readable_from_other_worker')
                assert second.call('GET', turn_path)['phase'] == 'awaiting_input'
                second.call('POST', turn_path + '/approvals/' + approval['id'] + ':respond', {'decision': 'accept'}, expect=409)
                second.call('POST', turn_path + ':cancel', expect=409)
                same = second.call('POST', '/codex/sessions/' + pending_session['id'] + '/turns', pending_body)
                assert same['id'] == turn['id']
                second.call('POST', '/conversations/' + str(pending_conv['id']) + '/messages',
                    {'content': 'Must not run.', 'provider_profile_id': provider}, expect=409)
                second.call('DELETE', '/conversations/' + str(pending_conv['id']), expect=409)
                checks.append('foreign_worker_pending_approval_read_only_and_no_effect_replay')
                before = (api.root / 'requests.log').read_text().splitlines()
                internal = api.call('GET', '/fixture/internal/' + pending_session['id'])
                api.proc.kill()
                api.proc.wait(timeout=10)
                try:
                    operation.result(timeout=10)
                except (OSError, http.client.IncompleteRead, http.client.RemoteDisconnected):
                    pass
                wait_for(lambda: identity.probe(internal['state']['process']['root']['host']), lambda value: value['state'] == 'exited')
                unknown = wait_for(lambda: second.call('GET', turn_path), lambda row: row['phase'] == 'recovery_required')
                receipts.append({'owner_death': unknown})
                second.call('POST', turn_path + '/approvals/' + approval['id'] + ':respond', {'decision': 'accept'}, expect=409)
                second.call('DELETE', '/conversations/' + str(pending_conv['id']), expect=409)
                checks.append('actual_owner_death_retires_scope_and_leaves_unknown_turn_read_only')
                proc.terminate()
                proc.wait(timeout=10)
                proc = spawn()
                wait_for(healthy, bool)
                state = second.call('GET', '/fixture/owner-state/' + str(api_conv['id']))
                assert state['tools'] == ['interrupted'], state
                for event_type in ('run_state', 'request_state'):
                    assert [e['payload']['status'] for e in state['events'] if e['type'] == event_type][-1] == 'interrupted', state
                checks.append('restart_recovers_api_history_only_after_real_owner_exit')
                assert second.call('GET', turn_path)['phase'] == 'recovery_required'
                same = second.call('POST', '/codex/sessions/' + pending_session['id'] + '/turns', pending_body)
                assert same['id'] == turn['id'] and (api.root / 'requests.log').read_text().splitlines() == before
                checks.append('restart_never_replays_accepted_native_message_or_approval')
                receipts.append({'after_restart': state, 'turn': same, 'owner_root': internal})
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                log.close()
    result = {'passed': True, 'count': len(checks), 'checks': checks, 'actual_api_workers': 2,
              'fixture_cli': True, 'evidence': str(api.root)}
    (api.root / 'owner-receipts.json').write_text(json.dumps(receipts, indent=2), encoding='utf8')
    (folder / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='cli-turn-owner-evidence-', dir=SERVER.parents[1] / 'tmp'))
    results = {'sqlite': await asyncio.to_thread(run_database, None, root / 'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url = make_url(Settings(_env_file=SERVER / '.env').database_url)
        assert url.host in {'127.0.0.1', 'localhost', '::1'} and url.drivername == 'postgresql+asyncpg'
        name = 'tmp_vf_turnowner_' + uuid.uuid4().hex
        assert re.fullmatch('tmp_vf_turnowner_[0-9a-f]{32}', name)
        admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
            password=url.password, database='postgres', timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "' + name + '"')
            created = True
            results['postgresql'] = await asyncio.to_thread(run_database,
                url.set(database=name).render_as_string(hide_password=False), root / 'postgresql')
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()') == 'postgres'
                await admin.execute('DROP DATABASE "' + name + '"')
            await admin.close()
        results['postgresql']['temporary_database_removed'] = True
    (root / 'result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print(json.dumps({'passed': True, 'counts': {key: value['count'] for key, value in results.items()}, 'evidence': str(root)}))


if __name__ == '__main__':
    asyncio.run(main())
