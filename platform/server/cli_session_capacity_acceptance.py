"""Exercise durable account capacity with the installed CLI on two real API workers."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx

from cli_managed_session_checks import FIXTURE as MANAGED
from testsupport import IsolatedServer, SERVER


FIXTURE = '''
from contextlib import asynccontextmanager

get_settings().codex_concurrent_sessions = 1

@app.post('/api/v1/test/cli-capacity/prepare')
async def prepare_capacity(user=Depends(get_current_user)):
    await native_root()
    get_settings().codex_wsl_state_root = json.loads((_base/'managed-session-native-root.json').read_text())['root']
    return {'ready': True}

async def await_file(path):
    async with asyncio.timeout(30):
        while not path.exists():
            await asyncio.sleep(.025)

original_access = sessions.Session.access
@asynccontextmanager
async def synchronize_preparation(self, **kwargs):
    async with original_access(self, **kwargs) as db:
        yield db
    if kwargs.get('private_capacity') and self.client is None and (_base/'race-enabled').exists():
        (_base/('arrived-'+self.runtime)).touch()
        await await_file(_base/('arrived-'+('windows' if self.runtime=='wsl' else 'wsl')))
sessions.Session.access = synchronize_preparation

original_complete = records.complete
async def delay_commit(owner, root, stop, **kwargs):
    if (_base/'hold-completion').exists():
        assert root is not None and stop['verified']
        (_base/'verified-before-commit').touch()
        await await_file(_base/'release-completion')
    return await original_complete(owner, root, stop, **kwargs)
records.complete = delay_commit

@app.post('/api/v1/test/cli-capacity/receipt/{identifier}/{action}')
async def receipt_fault(identifier: str, action: str, user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row = await db.get(NativeCliSession, identifier)
        assert row is not None and row.created_by == user.id and row.phase == 'closed'
        backup = _base / ('isolated-stop-' + row.id)
        if action == 'damage':
            assert records.public(row)['cleanup_verified'] is True
            with backup.open('x', encoding='utf8') as stream:
                stream.write(row.stop_json)
            row.stop_json = '{}'
        else:
            assert action == 'restore' and row.stop_json == '{}'
            row.stop_json = backup.read_text(encoding='utf8')
        await db.commit()
    return {'action': action}
'''


def wait_for(action, ready, *, seconds=40):
    deadline = time.monotonic() + seconds
    while True:
        value = action()
        if ready(value):
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError('CLI capacity evidence did not settle')
        time.sleep(.1)


async def verify_exits(sessions):
    from app.services import process_identity_native as identity
    from app.services.owned_process import probe_native

    births = {}

    def collect(value):
        if isinstance(value, dict):
            if value.get('schema') == 'vibeflow.os-process.v1':
                births[json.dumps(value, sort_keys=True)] = value
            else:
                for child in value.values():
                    collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for session in sessions:
        collect(session['process'])
        collect(session['stop'])
    observed = []
    for birth in births.values():
        windows = birth['platform']=='windows'
        proof = identity.probe(birth) if windows else await probe_native('vibeflow-linux',birth)
        state = proof['state']
        if windows and proof=={'state':'mismatch','reason':'birth_or_scope_changed'}:
            try:
                current = identity.observe(birth['pid'])
            except ProcessLookupError:
                state = 'exited'
            else:
                if current['host_id']==birth['host_id'] and current['creation_time_100ns']!=birth['creation_time_100ns']:
                    state = 'pid_reused'
        observed.append({'platform':birth['platform'],'pid':birth['pid'],'state':state})
    assert {item['platform'] for item in observed}=={'windows','linux'}
    assert all(item['state'] in {'exited','pid_reused'} for item in observed), observed
    return observed


@contextmanager
def second_api(api):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    with (api.root / 'peer.log').open('wb') as log:
        peer = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', api.app,
            '--app-dir', str(SERVER), '--host', '127.0.0.1', '--port', str(port), '--no-access-log'],
            cwd=api.root, env=api.env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)

        def healthy():
            assert peer.poll() is None, 'Second API exited at startup'
            try:
                return httpx.get(base+'/api/health', trust_env=False, timeout=2).json().get('ok') is True
            except (httpx.TransportError, ValueError):
                return False
        try:
            wait_for(healthy, bool)
            yield base, peer
        finally:
            if peer.poll() is None:
                peer.terminate()
                try:
                    peer.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    peer.kill()
                    peer.wait(timeout=5)


def verify(database_url=None):
    api = IsolatedServer('cli-session-capacity', app='cli_capacity_fixture:app', database_url=database_url)
    (api.root/'cli_capacity_fixture.py').write_text('from app.main import app\n'+MANAGED+FIXTURE, encoding='utf8')
    report = {'passed': False, 'database': 'postgresql' if database_url else 'sqlite',
              'configured_limit_per_account': 1, 'actual_model_calls': 0, 'mock_llm_calls': 0, 'checks': []}
    print('CLI_SESSION_CAPACITY '+str(api.root), flush=True)

    def save():
        (api.root/'capacity.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf8')

    def call(base, method, path, body=None, *, token=None, expected=(200,), retry_busy=False):
        deadline = time.monotonic()+25
        with httpx.Client(base_url=base+'/api/v1', headers={'Authorization':'Bearer '+(token or api.token)},
                          timeout=75, trust_env=False) as client:
            while True:
                response = client.request(method, path, json=body)
                value = response.json()
                detail = value.get('detail') if isinstance(value, dict) else None
                if not (retry_busy and response.status_code==409 and isinstance(detail, dict)
                        and detail.get('code')=='storage_quota_busy' and time.monotonic()<deadline):
                    break
                time.sleep(.1)
        assert response.status_code in expected, (method, path, response.status_code, value)
        return response.status_code, value

    def start(base, body, *, token=None, expected=(200,429)):
        return call(base, 'POST', '/codex/sessions', body, token=token, expected=expected, retry_busy=True)

    def assert_capacity(result):
        status, body = result
        assert status == 429 and body['detail']['code']=='cli_session_capacity', (status, body)

    def close(base, identifier, *, token=None):
        row = call(base, 'DELETE', '/codex/sessions/'+identifier, token=token)[1]
        assert row['phase']=='closed' and row['cleanup_verified'] and row['stop']['verified']
        return row

    try:
        if database_url:
            from workflow_migration_smoke import alembic
            alembic(database_url, api.root, 'upgrade', 'head')
        with api:
            first = api.create_case('CLI capacity case A')
            workspace = api.call('GET','/workspaces')[0]
            second = api.call('POST', f"/workspaces/{workspace['id']}/cases", {'name':'CLI capacity case B'})
            conversation = api.call('POST','/conversations',{'case_id':second['id'],'title':'CLI capacity request'})
            separate = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
                'password':'isolated-capacity-123','name':'Independent capacity account'})['token']
            with second_api(api) as (peer, peer_process):
                report['ports'] = [api.port, int(peer.rsplit(':',1)[1])]
                report['api_pids'] = [api.proc.pid, peer_process.pid]
                for base in (api.base, peer):
                    call(base,'POST','/test/cli-capacity/prepare')
                windows = {'runtime':'windows','case_id':first['id']}
                wsl = {'runtime':'wsl','case_id':second['id']}
                (api.root/'race-enabled').touch()
                try:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        pending = [pool.submit(start, base, body) for base,body in ((api.base,windows),(peer,wsl))]
                        outcomes = [item.result() for item in pending]
                finally:
                    (api.root/'race-enabled').unlink()
                assert (api.root/'arrived-windows').exists() and (api.root/'arrived-wsl').exists()
                assert sorted(status for status,_ in outcomes)==[200,429], [status for status,_ in outcomes]
                win = 0 if outcomes[0][0]==200 else 1
                assert_capacity(outcomes[1-win])
                owner, observer = ((api.base, peer) if win==0 else (peer, api.base))
                active = outcomes[win][1]
                assert active['phase']=='active' and len(api.call('GET','/codex/sessions'))==1
                report['checks'].append({'name':'two_api_two_runtime_race','winner':active['runtime'],
                    'http':[item[0] for item in outcomes], 'session_id':active['id']})
                save()

                own_room = start(observer, {'runtime':'windows'}, token=separate, expected=(200,))[1]
                separate_closed = close(observer, own_room['id'], token=separate)
                report['checks'].append({'name':'independent_account','session_id':own_room['id']})

                request_path = f"/codex/conversations/{conversation['id']}/session-requests"
                requested = {'runtime':'windows','purpose':'inspect','client_request_id':uuid.uuid4().hex}
                request = call(observer,'POST',request_path,requested)[1]
                status_path = '/codex/session-requests/'+request['id']
                denied = wait_for(lambda:call(observer,'GET',status_path)[1], lambda row:row['phase'] not in {'admitted','starting'})
                assert denied['phase']=='failed' and denied['error']=='CliSessionCapacity' and denied['session'] is None, denied
                assert denied['cleanup_verified'] is True
                assert call(observer,'POST',request_path,requested)[1]['id']==request['id']
                report['checks'].append({'name':'durable_request_refused_before_launch','request_id':request['id']})

                (api.root/'hold-completion').touch()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    closure = pool.submit(close,owner,active['id'])
                    try:
                        wait_for((api.root/'verified-before-commit').exists,bool)
                        assert_capacity(start(observer,windows))
                    finally:
                        (api.root/'hold-completion').unlink()
                        (api.root/'release-completion').touch()
                    settled = closure.result()
                report['checks'].append({'name':'verified_exit_waits_for_commit','session_id':settled['id']})

                fault_path = '/test/cli-capacity/receipt/'+settled['id']
                call(owner,'POST',fault_path+'/damage')
                try:
                    assert_capacity(start(observer,wsl))
                finally:
                    call(owner,'POST',fault_path+'/restore')
                report['checks'].append({'name':'unknown_retirement_occupies_slot','session_id':settled['id']})

                next_session = start(observer,wsl,expected=(200,))[1]
                retried = close(observer,next_session['id'])
                assert retried['stop']['native_state']=='exited'
                report['checks'].append({'name':'verified_retirement_releases_slot','session_id':retried['id']})
                save()
            assert peer_process.poll() is not None
        assert api.proc.poll() is not None
        report['both_apis_exited']=True
        report['original_process_exits']=asyncio.run(verify_exits([separate_closed,settled,retried]))
        report['passed']=True
        return report
    finally:
        save()


async def verify_postgres():
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings

    configured = make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert configured.drivername=='postgresql+asyncpg' and configured.host in {'127.0.0.1','localhost','::1'}
    name = 'tmp_vf_clicapacity_'+uuid.uuid4().hex
    admin = await asyncpg.connect(host=configured.host, port=configured.port or 5432,
        user=configured.username, password=configured.password, database='postgres', timeout=10)
    created = False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"')
        created = True
        await asyncio.to_thread(verify, configured.set(database=name).render_as_string(hide_password=False))
    finally:
        try:
            if created:
                await admin.execute('DROP DATABASE "'+name+'"')
                assert await admin.fetchval('SELECT datname FROM pg_database WHERE datname=$1',name) is None
                print('TEMP_DATABASE_DROPPED '+name, flush=True)
        finally:
            await admin.close()


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true')
    args=parser.parse_args()
    if args.postgres:
        asyncio.run(verify_postgres())
    else:
        verify()
