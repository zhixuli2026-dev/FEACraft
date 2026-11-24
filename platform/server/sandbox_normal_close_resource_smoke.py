"""Observe normal WSL sandbox close on authenticated SeaweedFS with real file I/O."""
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

import aiohttp

from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


SIZE = 40 * 1024 * 1024
UNCHANGED = 'Ref/retained.bin'
UPDATED = 'Ref/normal-close.txt'
TREE_STATE = '.vibeflow/tree-state.json'
RUNTIME = SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed'
FIXTURE = r'''
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time

from fastapi import Depends
from botocore.client import BaseClient
from app.deps import get_current_user
from app.main import app
from app.services import node_storage_budget, process_identity_native
from app.services import sandbox_export_publication, tree_transactions_s3

root = Path(__file__).parent
current = ContextVar('observed_sandbox_publication', default=None)
publish = sandbox_export_publication.publish
make_api_call = BaseClient._make_api_call
require_current = tree_transactions_s3._require_current


def write_json(name, data):
    path = root / name
    temporary = path.with_suffix('.writing')
    temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temporary.replace(path)


class ObserveBody:
    def __init__(self, body, state, trace):
        self.body = body
        self.state = state
        self.trace = trace

    def read(self, amount=None):
        data = self.body.read(amount)
        if data:
            first = not self.state['blocked']
            self.trace['bytes'] += len(data)
            self.trace['max_chunk'] = max(self.trace['max_chunk'], len(data))
            if first:
                self.state['blocked'] = True
                write_json('actual-first-block.json', self.trace)
                until = time.monotonic() + 17
                while not (root / 'release-read').exists():
                    if time.monotonic() >= until:
                        raise TimeoutError('Fixture did not release the actual S3 read')
                    time.sleep(.03)
        return data

    def close(self):
        self.trace['closed'] = True
        return self.body.close()


def observed_call(self, operation, args):
    response = make_api_call(self, operation, args)
    state = current.get()
    if (operation == 'GetObject' and state is not None and args['Key'].startswith(state['prefix']+'/')
            and args['Key'].endswith('/retained.bin')):
        trace = {'if_match': args.get('IfMatch'), 'length': response['ContentLength'],
                 'bytes': 0, 'max_chunk': 0, 'closed': False}
        state['reads'].append(trace)
        response['Body'] = ObserveBody(response['Body'], state, trace)
    return response


def observed_require(prefix, prepared):
    state = current.get()
    if state is not None and (root / 'arm-stale').exists():
        version = prepared.version[3].get('.vibeflow/tree-state.json')
        write_json('prepared-tree-boundary.json', {'metadata_version': version['etag'],
                    'fully_read_retained_bytes': sum(row['bytes'] for row in state['reads'])})
        until = time.monotonic() + 17
        while not (root / 'release-stale').exists():
            if time.monotonic() >= until:
                raise TimeoutError('Fixture did not release the version recheck')
            time.sleep(.03)
    return require_current(prefix, prepared)


async def observed_publish(exporter, session_id, case_id, prefix, authorize):
    state = {'session_id': session_id, 'case_id': case_id, 'prefix': prefix.rstrip('/'),
             'blocked': False, 'reads': []}
    marker = current.set(state)
    try:
        result = await publish(exporter, session_id, case_id, prefix, authorize)
        state['event_id'] = result['event_id']
        return result
    except BaseException as error:
        state['failure'] = {'type': type(error).__name__, 'message': str(error)[:600]}
        raise
    finally:
        current.reset(marker)
        write_json('actual-publication-trace.json', state)


BaseClient._make_api_call = observed_call
sandbox_export_publication.publish = observed_publish
tree_transactions_s3._require_current = observed_require


@app.get('/fixture/sandbox-normal-close')
def observation(user=Depends(get_current_user)):
    return {'process': process_identity_native.observe(os.getpid()),
            'reserved_bytes': node_storage_budget.observe()['reserved_bytes']}
'''


def source(root):
    target = root / 'retained.bin'
    digest = hashlib.sha256()
    block = bytes(range(256)) * 256
    with target.open('xb') as body:
        for _ in range(SIZE // len(block)):
            body.write(block)
            digest.update(block)
    return target, {'size': SIZE, 'sha256': digest.hexdigest()}


def case_prefix(api):
    url = (api.root / 'test.sqlite').resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(url, uri=True)) as db:
        return db.execute('SELECT s3_prefix FROM cases WHERE id=?', (api.case_id,)).fetchone()[0]


def writer_available(api):
    with closing(sqlite3.connect(api.root / 'test.sqlite', timeout=.2)) as db:
        try:
            db.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as error:
            if 'locked' not in str(error):
                raise
            return False
        db.rollback()
        return True


def inspect_source(store, key):
    digest, size = hashlib.sha256(), 0
    reply = store.client.get_object(Bucket=store.bucket, Key=key)
    with closing(reply['Body']) as body:
        while block := body.read(65536):
            size += len(block)
            digest.update(block)
    return {'size': size, 'sha256': digest.hexdigest(), 'etag': reply['ETag']}


async def call(api, method, path, body=None, **kw):
    return await asyncio.to_thread(api.call, method, path, body, **kw)


async def wait_file(path, seconds):
    until = time.monotonic() + seconds
    while not path.is_file():
        if time.monotonic() >= until:
            raise TimeoutError('Missing real observation: ' + path.name)
        await asyncio.sleep(.03)
    return json.loads(path.read_text(encoding='utf-8'))


def change_tree_metadata(store, prefix, etag):
    key = prefix.rstrip('/') + '/' + TREE_STATE
    reply = store.client.get_object(Bucket=store.bucket, Key=key, IfMatch=etag)
    with closing(reply['Body']) as body:
        document = json.loads(body.read())
    entry = document['entries'][UNCHANGED]
    old = entry['mode']
    entry['mode'] ^= 0o100
    data = json.dumps(document, sort_keys=True, separators=(',', ':')).encode()
    changed = store.client.put_object(Bucket=store.bucket, Key=key, Body=data, IfMatch=etag,
                                      ContentType='application/octet-stream')
    assert changed['ETag'] != etag
    return {'old_mode': old, 'new_mode': entry['mode'], 'etag': changed['ETag']}


async def await_released(api):
    url = (api.root / 'test.sqlite').resolve().as_uri() + '?mode=ro'
    async with asyncio.timeout(15):
        while True:
            with closing(sqlite3.connect(url, uri=True)) as db:
                grants = db.execute("SELECT COUNT(*) FROM owner_storage_reservations WHERE state='active'").fetchone()[0]
            reserved = (await call(api, 'GET', '/fixture/sandbox-normal-close', api_prefix=False))['reserved_bytes']
            if grants == reserved == 0:
                return {'active_owner_grants': grants, 'reserved_bytes': reserved}
            await asyncio.sleep(.08)


async def close_shell(api, store, prefix, expected, result, *, stale=False):
    proof = 'NORMAL_CLOSE_' + uuid.uuid4().hex
    async with aiohttp.ClientSession() as client:
        async with client.ws_connect(api.base + '/ws/terminal', params={
                'token': api.token, 'case': api.case_id, 'shell': 'wsl-sandbox'}) as ws:
            ready = await ws.receive_json(timeout=120)
            assert ready['type'] == 'ready', ready
            sid = result['session_id'] = ready['session']
            # Echo the observed path instead of the unique value to avoid confusing input echo with output.
            await ws.send_json({'type': 'input', 'data':
                f"printf '%s\\n' '{proof}' > {UPDATED}; sha256sum {UNCHANGED}; wc -c < {UPDATED}\r"})
            expected_shell_output = expected['sha256'].encode() + b'  ' + UNCHANGED.encode()
            output = b''
            async with asyncio.timeout(40):
                while expected_shell_output not in output:
                    packet = await ws.receive()
                    assert packet.type == aiohttp.WSMsgType.BINARY, packet
                    output += packet.data
                    assert len(output) <= 65536
            await ws.send_json({'type': 'input', 'data': 'exit\r'})
            try:
                first = result['real_first_block'] = await wait_file(api.root / 'actual-first-block.json', 150)
                assert first['length'] == SIZE and first['bytes'] > 0 and first['if_match'], first
                result['writer_available_during_read'] = await asyncio.to_thread(writer_available, api)
            finally:
                (api.root / 'release-read').touch()

            if stale:
                try:
                    boundary = result['read_write_boundary'] = await wait_file(
                        api.root / 'prepared-tree-boundary.json', 30)
                    assert boundary['fully_read_retained_bytes'] == SIZE, boundary
                    result['external_metadata'] = await asyncio.to_thread(
                        change_tree_metadata, store, prefix, boundary['metadata_version'])
                finally:
                    (api.root / 'release-stale').touch()

            async with asyncio.timeout(150):
                while True:
                    closed = await call(api, 'GET', '/terminal/sessions/' + sid)
                    if closed['phase'] in {'closed', 'failed'}:
                        break
                    await asyncio.sleep(.1)
            result['terminal'] = closed
            result['trace'] = await wait_file(api.root / 'actual-publication-trace.json', 10)
    assert closed['cleanup_verified'] is True and closed['exit_code'] == 0, closed
    assert result['writer_available_during_read'] is True, result['trace']
    reads = result['trace']['reads']
    assert len(reads) == 1 and reads[0]['bytes'] == SIZE and reads[0]['closed'], reads
    if stale:
        assert closed['phase'] == 'failed' and closed['error'] == 'CaseWriteConflict', closed
        assert 'export' not in closed['process'] and 'event_id' not in result['trace']
    else:
        assert closed['phase'] == 'closed', closed
        assert closed['process']['export']['event_id'] == result['trace']['event_id']
    result['release'] = await await_released(api)
    return proof + '\n'


async def exercise(api, store, result):
    await asyncio.to_thread(api.create_case, 'Observed WSL sandbox normal close')
    result['api'] = await call(api, 'GET', '/fixture/sandbox-normal-close', api_prefix=False)
    prefix = case_prefix(api)
    key = prefix.rstrip('/') + '/' + UNCHANGED
    retained, expected = source(api.root)
    with retained.open('rb') as body:
        store.client.put_object(Bucket=store.bucket, Key=key, Body=body,
                                ContentType='application/octet-stream', IfNoneMatch='*')
    result['before'] = inspect_source(store, key)
    assert all(result['before'][name] == value for name, value in expected.items())
    await asyncio.to_thread(api.put, UPDATED, 'before-shell\n')

    result['normal'] = {}
    published = await close_shell(api, store, prefix, expected, result['normal'])
    assert await call(api, 'GET', api.prefix + '/raw?path=' + UPDATED, raw=True) == published.encode()
    await asyncio.to_thread(api.put, 'Ref/after-normal.txt', 'same owner can save again')
    for name in ('actual-first-block.json', 'actual-publication-trace.json', 'release-read'):
        (api.root / name).unlink()
    (api.root / 'arm-stale').touch()

    result['stale'] = {}
    retained_text = await close_shell(api, store, prefix, expected, result['stale'], stale=True)
    assert await call(api, 'GET', api.prefix + '/raw?path=' + UPDATED, raw=True) == published.encode()
    observed = store.client.head_object(Bucket=store.bucket, Key=prefix.rstrip('/') + '/' + TREE_STATE)
    assert observed['ETag'] == result['stale']['external_metadata']['etag']
    await asyncio.to_thread(api.put, 'Ref/after-stale.txt', 'same owner can save after stale')
    result['after'] = inspect_source(store, key)
    assert result['after'] == result['before'], result['after']
    result['final_release'] = await await_released(api)
    url = (api.root / 'test.sqlite').resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(url, uri=True)) as db:
        exports = db.execute("SELECT phase,profile_json FROM native_requests WHERE kind='terminal.export'").fetchall()
    assert len(exports) == 2 and all(phase == 'succeeded' for phase, _ in exports), exports
    result['native_exports'] = len(exports)
    profile = next(json.loads(raw) for _, raw in exports if json.loads(raw)['session_id'] == result['stale']['session_id'])
    retained_root = Path(profile['root']).resolve()
    assert retained_root.is_relative_to(api.root.resolve())
    assert (retained_root / 'files' / UPDATED).read_bytes() == retained_text.encode()
    result['stale']['retained_copy'] = str(retained_root)


def run():
    api = IsolatedServer('sandbox-normal-close-resource', app='sandbox_normal_close_fixture:app')
    report = {'passed': False, 'real_model_calls': 0, 'mock_llm_calls': 0, 'api_port': api.port}
    print('SANDBOX_NORMAL_CLOSE_RESOURCE ' + str(api.root), flush=True)
    try:
        with ObjectStore(executable=RUNTIME, volume_max=16, authenticated=True) as store:
            settings = store.settings()
            scratch = api.root / 'native-temp'
            scratch.mkdir()
            settings.update(TMP=str(scratch), TEMP=str(scratch), TMPDIR=str(scratch))
            fixture = api.root / 'sandbox_normal_close_fixture.py'
            try:
                fixture.write_text('import os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE, encoding='utf-8')
                with api:
                    asyncio.run(exercise(api, store, report))
            finally:
                fixture.unlink(missing_ok=True)
            if api.proc is not None:
                report['api_wait_returncode'] = api.proc.poll()
        report['s3_receipt'] = str(store.root / 'store.json')
        assert report['api_wait_returncode'] is not None and store.proc.poll() is not None
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:1200]}
        raise
    finally:
        (api.root / 'sandbox-normal-close-resource.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('SANDBOX_NORMAL_CLOSE_RESOURCE_OK ' + str(api.root), flush=True)


if __name__ == '__main__':
    run()
