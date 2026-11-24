"""Exercise copy worker saturation with real files, deadlines and HTTP requests."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, nullcontext
import json
from pathlib import Path
import sys
import time
import uuid

import aiohttp
import httpx

from case_copy_source_lock_smoke import LARGE, RUNTIME, digest_response, read_observation, seed, writer_available
from s3testsupport import ObjectStore
from sandbox_reserved_tree_acceptance import finish_shell, rows
from testsupport import IsolatedServer, SERVER


FIXTURE = r'''
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import threading
import time

from fastapi import Depends
from app.deps import get_current_user
from app.main import app
from app.services import case_read_capacity, node_storage_budget, process_identity_native, s3_live_store
from app.services.case_source import SourceFile
from app.services.request_deadline import current

root = Path(__file__).parent
lane = ContextVar('copy_fixture_lane', default='')
reading = ContextVar('copy_fixture_reading', default=False)
real_copy = SourceFile.copy_into
real_open = Path.open
real_get = s3_live_store.Client.get_object
real_deadline = case_read_capacity.file_read_deadline


class Body:
    def __init__(self, body):
        self.body = body
        self.first = True

    def __getattr__(self, name):
        return getattr(self.body, name)

    def __enter__(self):
        self.body.__enter__()
        return self

    def __exit__(self, *args):
        return self.body.__exit__(*args)

    def read(self, size):
        data = self.body.read(size)
        if self.first and data:
            self.first = False
            label = lane.get()
            details = {'first_bytes': len(data), 'worker': threading.current_thread().name,
                       'remaining': current().remaining(), 'observed_at': time.monotonic()}
            temporary = root / (label + '.new')
            temporary.write_text(json.dumps(details), encoding='utf8')
            temporary.replace(root / (label + '.json'))
            until = time.monotonic() + 100
            while not (root / ('release-' + label)).exists():
                if time.monotonic() >= until:
                    raise TimeoutError('Acceptance fixture did not release the actual read')
                time.sleep(.025)
        return data


def copy_into(self, destination):
    token = reading.set(bool(lane.get()) and self.path == 'Ref/archive-large.bin')
    try:
        return real_copy(self, destination)
    finally:
        reading.reset(token)


def open_file(self, *args, **kwargs):
    stream = real_open(self, *args, **kwargs)
    return Body(stream) if reading.get() and args == ('rb',) and self.name == 'archive-large.bin' else stream


def get_object(self, **kwargs):
    response = real_get(self, **kwargs)
    if reading.get() and kwargs['Key'].endswith('/Ref/archive-large.bin'):
        response['Body'] = Body(response['Body'])
    return response


@asynccontextmanager
async def deadline(code, label, *, seconds=None):
    if lane.get() == 'deadline':
        seconds = 22
    async with real_deadline(code, label, seconds=seconds):
        yield


SourceFile.copy_into = copy_into
Path.open = open_file
s3_live_store.Client.get_object = get_object
case_read_capacity.file_read_deadline = deadline


@app.get('/fixture/copy-admission-owner')
def owner(user=Depends(get_current_user)):
    return {'identity': process_identity_native.observe(os.getpid()),
            'reserved': node_storage_budget.observe()['reserved_bytes']}


class Observe:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        value = dict(scope.get('headers', ())).get(b'x-fixture-copy-lane', b'')
        token = lane.set(value.decode('ascii') if scope.get('path', '').endswith(':copy') else '')
        try:
            return await self.app(scope, receive, send)
        finally:
            lane.reset(token)


app = Observe(app)
'''


def exercise(api, store, baseline, report, check_terminal=None):
    inputs = []
    for index in range(5):
        case = api.create_case(f'Real worker source {index}')
        ws = api.call('GET', '/workspaces')[0]['id']
        # seed uses a constant local spool name; each case retains its own body.
        prefix = api.call('GET', api.prefix)['storage_prefix'] if api.database_url else None
        _, digest = seed(api, store, prefix=prefix)
        (api.root / 'actual-backup-asset.bin').unlink()
        inputs.append({'case': case['id'], 'ws': ws, 'digest': digest, 'token': api.token})

    proof = '/fixture/copy-admission-owner'
    report['api_process'] = api.call('GET', proof, api_prefix=False)['identity']
    report['inputs'] = [item['digest'] for item in inputs]
    labels = ['case-one', 'workspace-one', 'case-two', 'workspace-two' if baseline else 'deadline', 'fifth']
    with httpx.Client(base_url=api.base + '/api/v1', timeout=145, trust_env=False) as client:
        if api.database_url:
            api.token = inputs[0]['token']
            sibling = api.call('POST', f'/workspaces/{inputs[0]["ws"]}/cases', {'name':'Same owner concurrent write'})
            sibling_path = f'/cases/{sibling["id"]}'

        def copy(index, label):
            item = inputs[index]
            workspace = index % 2 == 1
            route = f'/workspaces/{item["ws"]}:copy' if workspace else f'/cases/{item["case"]}:copy'
            headers = {'Authorization': 'Bearer ' + item['token']}
            if label:
                headers['X-Fixture-Copy-Lane'] = label
            return client.post(route, headers=headers, json={'name': f'Worker target {index}'})

        def release(label):
            (api.root / ('release-' + label)).touch()

        with ThreadPoolExecutor(max_workers=5) as pool:
            pending = []
            began = []
            replies = []
            try:
                for index, label in enumerate(labels[:4]):
                    future = pool.submit(copy, index, label)
                    pending.append(future)
                    began.append(read_observation(api, label + '.json'))
                    assert not future.done()
                if api.database_url:
                    blocked = api.call('PUT', sibling_path+'/file',
                        {'path':'Ref/parallel-save.txt','content':'shares capacity'}, expect=429)
                    assert blocked['detail']['code']=='file_write_busy',blocked
                    report['same_owner_file_save_http'] = 429
                    updated = api.call('PATCH', sibling_path, {'name':'Renamed while copy slots are busy'})
                    assert api.call('GET', sibling_path)['name']==updated['name']=='Renamed while copy slots are busy'
                    report['postgres_same_owner_metadata_write_http'] = 200
                else:
                    assert writer_available(api)
                    report['sqlite_writer_available'] = True
                report['four_reads'] = began
                assert len({row['worker'] for row in began}) == 4 and all(0 < row['first_bytes'] <= 1048576 for row in began)
                if check_terminal is not None:
                    report['terminal_at_capacity'] = check_terminal()
                if baseline:
                    fifth = pool.submit(copy, 4, 'fifth')
                    report['fifth_read'] = read_observation(api, 'fifth.json')
                    assert not fifth.done() and report['fifth_read']['worker'] not in {row['worker'] for row in began}
                    pending.append(fifth)
                else:
                    assert all(row['worker'].startswith('case-copy') for row in began), began
                    rejected = copy(4, 'fifth')
                    assert rejected.status_code == 429 and rejected.headers['Retry-After'] == '1', rejected.text
                    assert rejected.json()['detail']['code'] == 'case_copy_busy' and not (api.root / 'fifth.json').exists()
                    expires = began[-1]['observed_at'] + began[-1]['remaining']
                    time.sleep(max(0, expires - time.monotonic()) + .2)
                    assert not pending[-1].done()
                    draining = copy(4, 'fifth')
                    assert draining.status_code == 429 and not (api.root / 'fifth.json').exists(), draining.text
                    if check_terminal is not None:
                        report['terminal_at_expired_drain'] = check_terminal()
                    release('deadline')
                    expired = pending.pop().result(timeout=35)
                    assert expired.status_code == 504 and expired.json()['detail']['code'] == 'workspace_copy_timeout', expired.text
                    assert all(not request.done() for request in pending)
                    available = copy(4, '')
                    assert available.status_code == 200, available.text
                    with httpx.Client(base_url=api.base + '/api/v1', trust_env=False, timeout=40,
                                      headers={'Authorization': 'Bearer ' + inputs[4]['token']}) as reader:
                        assert digest_response(reader, available.json()['id'], LARGE) == inputs[4]['digest']
                    api.token = inputs[3]['token']
                    assert 'Worker target 3' not in {row['name'] for row in api.call('GET', '/workspaces')}
                    report.update(overload_http=429, post_deadline_draining_http=429, deadline_http=504,
                                  released_slot_retry_http=200)
                # Publish the remaining requests one at a time. SQLite's
                # independent short write admission may reject competing commits.
                for label, future in zip(labels, pending):
                    release(label)
                    replies.append(future.result(timeout=140))
            finally:
                for label in labels:
                    release(label)
        report['parallel_responses'] = [response.status_code for response in replies]
        assert all(response.status_code == 200 for response in replies), [response.text for response in replies]
    report['node_reserved_after'] = api.call('GET', proof, api_prefix=False)['reserved']
    assert report['node_reserved_after'] == report.get('terminal_reservation', 0)


async def exercise_with_terminal(api, store, report):
    await asyncio.to_thread(api.create_case, 'Nonprivileged terminal during full copy capacity')
    token, case_id = api.token, api.case_id
    loop = asyncio.get_running_loop()
    async with AsyncExitStack() as stack, aiohttp.ClientSession() as client:
        database = None
        if api.database_url:
            import asyncpg

            database = await asyncpg.connect(api.database_url.replace('postgresql+asyncpg:', 'postgresql:', 1), timeout=10)
            stack.push_async_callback(database.close)

        async def heartbeat(sid):
            if database is None:
                return rows(api, 'SELECT heartbeat_at,phase FROM native_sessions WHERE id=?', sid)[0]
            stamp, phase = await database.fetchrow('SELECT heartbeat_at,phase FROM native_sessions WHERE id=$1', sid)
            return stamp.isoformat(), phase

        async with client.ws_connect(api.base+'/ws/terminal', headers={'Authorization': 'Bearer ' + token},
                                     params={'case': case_id, 'shell': 'wsl-sandbox'}) as ws:
            ready = await ws.receive_json(timeout=105)
            assert ready['type'] == 'ready', ready
            sid = ready['session']
            proof_route = '/fixture/copy-admission-owner'
            baseline = await asyncio.to_thread(api.call, 'GET', proof_route, api_prefix=False)
            report['terminal_reservation'] = baseline['reserved']
            assert report['terminal_reservation'] > 0, baseline
            frames = asyncio.Queue(maxsize=64)

            async def receive():
                # Continuously service WebSocket ping/pong while S3 inputs upload.
                async for message in ws:
                    await frames.put(message)

            async def check():
                previous, phase = await heartbeat(sid)
                assert phase == 'active', phase
                proof = uuid.uuid4().hex
                await ws.send_json({'type': 'input', 'data':
                    "test \"$(id -u)\" = '65534' && printf '%s%s\\n' 'RB14_' '"+proof+"'\r"})
                output = b''
                async with asyncio.timeout(12):
                    while ('RB14_'+proof).encode() not in output:
                        message = await frames.get()
                        assert message.type == aiohttp.WSMsgType.BINARY, message
                        output += message.data
                        assert len(output) < 65536
                    while True:
                        latest, phase = await heartbeat(sid)
                        assert phase == 'active', phase
                        if latest != previous:
                            break
                        await asyncio.sleep(.15)
                return {'session_id': sid, 'uid': 65534, 'command_executed': True,
                        'heartbeat_before': previous, 'heartbeat_after': latest}

            def observe_terminal():
                return asyncio.run_coroutine_threadsafe(check(), loop).result(timeout=14)

            receiver = asyncio.create_task(receive())
            try:
                await asyncio.to_thread(exercise, api, store, False, report, observe_terminal)
                api.token = token
                await ws.send_json({'type': 'input', 'data': 'exit\r'})
                state = await finish_shell(api, sid)
                assert state['phase'] == 'closed', state
                report['terminal_stop'] = {'cleanup_verified': state['cleanup_verified'], 'exit_code': state['exit_code']}
                recorded = await asyncio.to_thread(api.call, 'GET', proof_route, api_prefix=False)
                report['node_reserved_after_terminal_close'] = recorded['reserved']
                assert recorded['reserved'] == 0
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)


def run(backend, baseline, terminal=False, database_url=None):
    api = IsolatedServer(f'copy-workers-{backend}', app='copy_worker_fixture:app', database_url=database_url)
    fixture = api.root / 'copy_worker_fixture.py'
    report = {'passed': False, 'baseline': baseline, 'backend': backend, 'api_port': api.port, 'combined_terminal': terminal,
              'database': 'postgresql' if database_url else 'sqlite', 'actual_model_calls': 0, 'mock_llm_calls': 0}
    print('COPY_WORKERS_EVIDENCE', api.root, flush=True)
    storage = ObjectStore(executable=RUNTIME, volume_max=40, authenticated=True) if backend == 's3' else nullcontext()
    try:
        with storage as store:
            settings = store.settings() if store else {}
            if store:
                report['s3_receipt'] = str(store.root / 'store.json')
            fixture.write_text('import os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE, encoding='utf8')
            try:
                with api:
                    if terminal:
                        asyncio.run(exercise_with_terminal(api, store, report))
                    else:
                        exercise(api, store, baseline, report)
            finally:
                fixture.unlink(missing_ok=True)
        assert api.proc.poll() is not None and (store is None or store.proc.poll() is not None)
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:1300]}
        raise
    finally:
        (api.root / 'workers.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')


async def run_postgres(backend, terminal):
    from live_llm_terminal_agent_acceptance import disposable_database

    async with disposable_database(True) as url:
        await asyncio.to_thread(run, backend, False, terminal, url)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['fs', 's3'], default='fs')
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--terminal', action='store_true')
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    if args.baseline and args.terminal:
        parser.error('The combined terminal check requires bounded copy admission')
    if args.postgres:
        if args.baseline:
            parser.error('The PostgreSQL check requires bounded copy admission')
        asyncio.run(run_postgres(args.backend, args.terminal))
    else:
        run(args.backend, args.baseline, args.terminal)
