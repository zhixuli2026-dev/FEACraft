"""Observe source I/O and concurrent writes through actual run derivation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import time
import uuid

import httpx

from case_copy_source_lock_smoke import RUNTIME, digest_response, read_observation, writer_available
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


BASE = 'C-run/derive-source'
TARGET = 'C-run/derive-copy'
LARGE = BASE + '/0/source-field.bin'
EDIT = BASE + '/0/initial-note.txt'
SIZE = 40 * 1024 * 1024
CONTROL = '''FoamFile
{
    version 2.0;
    format ascii;
    class dictionary;
    object controlDict;
}
application laplacianFoam;
startFrom startTime;
startTime 0;
stopAt endTime;
endTime 1;
deltaT 0.1;
writeControl timeStep;
writeInterval 1;
'''
EXCLUDED = ('0.2/T', '-1/T', '1e+02/T', 'processor0/0/T', 'checkMesh.log', 'source.foam')
FIXTURE = r'''
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time

from fastapi import Depends
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.routers.workspaces import get_owned_case
from app.services import file_transactions, node_storage_budget, owner_storage_budget
from app.services import process_identity_native, run_derive, s3_live_store


root = Path(__file__).parent
target = 'C-run/derive-copy/'
large = '/C-run/derive-source/0/source-field.bin'
observation = ContextVar('derive_observation', default=None)
first_inventory = ContextVar('derive_first_inventory', default=None)
real_open = Path.open
real_get = s3_live_store.Client.get_object
real_local_inventory = run_derive.source_files
real_remote_inventory = run_derive.remote_inventory
real_prepare = run_derive.DerivedRun.prepare
real_write = file_transactions.write


def record(name, value):
    path = root / name
    scratch = path.with_suffix('.writing')
    scratch.write_text(json.dumps(value, sort_keys=True), encoding='utf8')
    scratch.replace(path)


class ObservedBody:
    def __init__(self, body, state):
        self.body, self.state = body, state

    def __getattr__(self, name):
        return getattr(self.body, name)

    def __enter__(self):
        self.body.__enter__()
        return self

    def __exit__(self, *args):
        return self.body.__exit__(*args)

    def read(self, amount):
        data = self.body.read(amount)
        if data:
            state = self.state
            state['source_bytes'] += len(data)
            state['blocks'] += 1
            state['largest_block'] = max(state['largest_block'], len(data))
            if state['blocks'] == 1:
                record('first-block.json', state)
                until = time.monotonic() + 25
                while not (root / 'release-read').exists():
                    if time.monotonic() >= until:
                        raise TimeoutError('Isolated derivation read was not released')
                    time.sleep(.03)
        return data


def observe_local(self, *args, **kwargs):
    body = real_open(self, *args, **kwargs)
    state = first_inventory.get()
    if state is not None and args == ('rb',) and self.as_posix().endswith(large):
        return ObservedBody(body,state)
    return body


def observe_remote(self, **options):
    response = real_get(self, **options)
    state = first_inventory.get()
    if state is not None and options['Key'].endswith(large):
        response['Body'] = ObservedBody(response['Body'],state)
    return response


def observe_inventory(function, *args, **kwargs):
    state = observation.get()
    if state is None or state['source_bytes']:
        return function(*args, **kwargs)
    state['owner_grant'] = owner_storage_budget.current() is not None
    token = first_inventory.set(state)
    try:
        return function(*args, **kwargs)
    finally:
        first_inventory.reset(token)


def observe_prepare(self):
    state = observation.get()
    if state is not None:
        state['destination_writes_started'] += 1
    return real_prepare(self)


async def observe_write(db, case, changes, **options):
    state = observation.get()
    if state is not None and any(path.startswith(target) for path in changes):
        state['destination_writes_started'] += 1
    return await real_write(db, case, changes, **options)


Path.open = observe_local
s3_live_store.Client.get_object = observe_remote
run_derive.source_files = lambda *args, **kwargs: observe_inventory(real_local_inventory, *args, **kwargs)
run_derive.remote_inventory = lambda *args, **kwargs: observe_inventory(real_remote_inventory, *args, **kwargs)
run_derive.DerivedRun.prepare = observe_prepare
file_transactions.write = observe_write


@app.get('/fixture/run-source-binding/{case_id}')
async def binding(case_id:int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id,user,db,write=False)
    return {'prefix':case.s3_prefix, 'pid':process_identity_native.observe(os.getpid())}


@app.get('/fixture/run-source-budget')
def budget(user=Depends(get_current_user)):
    return node_storage_budget.observe()


class InspectDerive:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get('headers', ()))
        if not scope.get('path', '').endswith('/runs:derive') or b'x-fixture-observe-derive' not in headers:
            return await self.app(scope, receive, send)
        state = {'source_bytes':0, 'blocks':0, 'largest_block':0, 'destination_writes_started':0}
        token = observation.set(state)

        async def observed_send(message):
            if message['type'] == 'http.response.start':
                state['status'] = message['status']
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            observation.reset(token)
            record('derive-trace.json', state)


app = InspectDerive(app)
'''


def seed(api, store, prefix):
    path = api.root / 'actual-derive-field.bin'
    digest = hashlib.sha256()
    with path.open('xb') as stream:
        for _ in range(40):
            block = os.urandom(1024 * 1024)
            stream.write(block)
            digest.update(block)
    if store is not None:
        with path.open('rb') as stream:
            store.client.put_object(Bucket=store.bucket, Key=prefix.rstrip('/') + '/' + LARGE,
                                    Body=stream, IfNoneMatch='*')
    else:
        case_root = (api.root / 'files' / prefix).resolve()
        target = (case_root / LARGE).resolve()
        assert target.is_relative_to(case_root) and case_root.is_relative_to(api.root.resolve())
        assert target.parent.is_dir() and not target.exists()
        shutil.copyfile(path, target)
    return {'size':SIZE, 'sha256':digest.hexdigest()}


def related_events(api):
    path = (api.root / 'test.sqlite').resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        return [identifier for identifier, payload in db.execute(
            "SELECT id,payload_json FROM durable_events WHERE case_id=? AND type='files.changed' ORDER BY seq",
            (api.case_id,)) if TARGET in payload]


def absent(api, client, store, prefix):
    missing = client.get(api.prefix + '/raw', params={'path':TARGET + '/system/controlDict'})
    assert missing.status_code == 404, missing.text
    if store is None:
        visible = (api.root / 'files' / prefix / TARGET).exists()
    else:
        listing = store.client.list_objects_v2(Bucket=store.bucket, Prefix=prefix.rstrip('/') + '/' + TARGET + '/')
        visible = bool(listing.get('Contents'))
    events = related_events(api)
    assert not visible and not events, (visible, events)
    return {'raw_status':missing.status_code, 'target_visible':visible, 'target_file_events':events}


def exercise(api, store, report, baseline):
    case = api.create_case('Actual engineering derivation during edit')
    binding = api.call('GET', f'/fixture/run-source-binding/{case["id"]}', api_prefix=False)
    report['api_process'] = binding['pid']
    prefix = binding['prefix']
    previous, revised = 'old initial field note', 'new initial field note'
    api.put(BASE + '/system/controlDict', CONTROL)
    api.put(EDIT, previous)
    for name in EXCLUDED:
        api.put(BASE + '/' + name, 'Unselected simulation output')
    expected = report['source'] = seed(api,store,prefix)
    command = {'base':BASE, 'name':TARGET.rsplit('/',1)[1]}
    edit = {'path':EDIT, 'content':revised, 'expected_sha256':hashlib.sha256(previous.encode()).hexdigest()}
    headers = {'Authorization':'Bearer ' + api.token}
    endpoint = api.prefix + '/runs:derive'
    with httpx.Client(base_url=api.base + '/api/v1', headers=headers, trust_env=False, timeout=150) as client:
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(client.post, endpoint, json=command, headers={'X-Fixture-Observe-Derive':'1'})
            try:
                block = report['first_block'] = read_observation(api,'first-block.json')
                assert 0 < block['source_bytes'] <= 1024*1024 and not pending.done(), block
                report['sqlite_writer_available'] = writer_available(api)
                save = client.put(api.prefix + '/file', json=edit, timeout=15)
                report['concurrent_save'] = {'status':save.status_code, 'document':save.json()}
            finally:
                (api.root / 'release-read').touch()
            result = pending.result(timeout=150)
        report['attempt'] = {'status':result.status_code, 'document':result.json()}
        trace = report['trace'] = read_observation(api,'derive-trace.json')
        assert trace['source_bytes'] == SIZE and trace['largest_block'] <= 1024*1024, trace
        assert report['sqlite_writer_available'] is not baseline
        if baseline:
            assert save.status_code == 409 and save.json()['detail']['code'] == 'storage_quota_busy', save.text
            assert result.status_code == 200 and trace['destination_writes_started'] == 1, (result.text, trace)
            wanted = previous
            assert client.put(api.prefix + '/file', json=edit).status_code == 200
        else:
            assert save.status_code == 200 and result.status_code == 409, (save.text, result.text)
            assert '源工况已变化' in result.json()['detail'], result.text
            assert trace['destination_writes_started'] == 0 and not block['owner_grant'], trace
            report['stale_rolled_back'] = absent(api,client,store,prefix)
            result = client.post(endpoint,json=command)
            report['retry'] = {'status':result.status_code, 'document':result.json()}
            assert result.status_code == 200, result.text
            wanted = revised
        assert result.json() == {'ok':True, 'created':TARGET, 'files':3}, result.text
        assert client.get(api.prefix + '/raw', params={'path':TARGET+'/0/initial-note.txt'}).text == wanted
        assert client.get(api.prefix + '/raw', params={'path':TARGET+'/system/controlDict'}).text == CONTROL
        for name in EXCLUDED:
            assert client.get(api.prefix + '/raw', params={'path':TARGET+'/'+name}).status_code == 404
        report['copied'] = digest_response(client, case['id'], TARGET+'/0/source-field.bin')
        assert report['copied'] == expected
        events = report['committed_destination_events'] = related_events(api)
        assert events
        assert client.post(endpoint, json=command).status_code == 409
        if not baseline:
            outsider = api.call('POST', '/auth/register', {'email':uuid.uuid4().hex+'@example.com',
                'name':'Another derivation owner', 'password':'isolated-test-123'})
            refused = client.post(endpoint, json={'base':BASE, 'name':'unauthorized-derived'},
                                  headers={'Authorization':'Bearer ' + outsider['token']})
            report['foreign_status'] = refused.status_code
            assert refused.status_code == 404, refused.text
    report['reserved_after'] = api.call('GET','/fixture/run-source-budget',api_prefix=False)['reserved_bytes']
    assert report['reserved_after'] == 0


def run(backend, baseline=False):
    from app.services import process_identity_native

    api = IsolatedServer('run-derive-source-' + backend, app='derive_source_fixture:app')
    receipt = api.root / 'run-derive-source-lock.json'
    report = {'passed':False, 'baseline':baseline, 'backend':backend, 'api_port':api.port,
              'real_model_calls':0, 'mock_llm_calls':0}
    context = ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) if backend == 's3' else nullcontext()
    print('DERIVE_SOURCE_EVIDENCE', api.root, flush=True)
    try:
        with context as store:
            configuration = store.settings() if store else {}
            if store is not None:
                report['store_receipt'] = str(store.root / 'store.json')
            scratch = api.root / 'native-temp'
            scratch.mkdir()
            configuration.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
            module = api.root / 'derive_source_fixture.py'
            try:
                module.write_text('import os\nos.environ.update(' + repr(configuration) + ')\n' + FIXTURE, encoding='utf8')
                with api:
                    exercise(api,store,report,baseline)
            finally:
                module.unlink(missing_ok=True)
        if store is not None:
            report['store_exit_code'] = store.proc.poll()
            assert report['store_exit_code'] is not None
        report['api_exit_code'] = api.proc.poll()
        assert report['api_exit_code'] is not None
        api.proc = None
        until = time.monotonic() + 5
        while True:
            report['api_exit'] = process_identity_native.probe(report['api_process'])
            try:
                with socket.create_connection(('127.0.0.1', api.port), timeout=.2):
                    report['api_port_closed'] = False
            except ConnectionRefusedError:
                report['api_port_closed'] = True
            except TimeoutError:
                report['api_port_closed'] = None
            stopped = report['api_exit']['state'] == 'exited' and report['api_port_closed'] is True
            if stopped or time.monotonic() >= until:
                break
            time.sleep(.05)
        report['api_os_observation_pending'] = not stopped
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__, 'message':str(error)[:1400]}
        raise
    finally:
        receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    print('DERIVE_SOURCE_PASS', receipt, flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    baseline = os.environ.get('VIBEFLOW_RUN_DERIVE_BASELINE') == '1'
    run('fs', baseline=baseline)
    if not baseline:
        run('s3')
