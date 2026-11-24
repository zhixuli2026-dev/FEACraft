"""Real case copy source hashing and concurrent files on FS and authenticated S3."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
import uuid

import httpx

from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER
from workspace_backup_lock_smoke import LARGE, seed, wait_json, writer_available


EDIT = 'Ref/ordinary-save.txt'
RUNTIME = SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed'
FIXTURE = r'''
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time

from fastapi import Depends
from app.deps import get_current_user
from app.main import app
from app.services import case_admin, case_provision, node_storage_budget
from app.services import owner_storage_budget, process_identity_native, s3_live_store


root = Path(__file__).parent
large = '/Ref/archive-large.bin'
request = ContextVar('clone_observation', default=None)
reading_source = ContextVar('clone_source_hash', default=None)
real_open = Path.open
real_get = s3_live_store.Client.get_object
real_manifest = case_admin.source_manifest
real_prepare = case_provision.prepare


def record(path, value):
    target = root / path
    temporary = target.with_suffix('.writing')
    temporary.write_text(json.dumps(value, sort_keys=True), encoding='utf8')
    temporary.replace(target)


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

    def read(self, size):
        block = self.body.read(size)
        if block:
            self.state['source_bytes'] += len(block)
            self.state['blocks'] += 1
            self.state['max_block'] = max(self.state['max_block'], len(block))
            if self.state['blocks'] == 1:
                record('first-file-block.json', self.state)
                until = time.monotonic() + 25
                while not (root / 'release-source-read').exists():
                    if time.monotonic() >= until:
                        raise TimeoutError('Isolated fixture did not release the actual source read')
                    time.sleep(.03)
        return block


def observe_local(self, *args, **kwargs):
    body = real_open(self, *args, **kwargs)
    state = reading_source.get()
    if state is not None and args == ('rb',) and self.as_posix().endswith(large):
        return ObservedBody(body, state)
    return body


def observe_remote(self, **kwargs):
    response = real_get(self, **kwargs)
    state = reading_source.get()
    if state is not None and kwargs['Key'].endswith(large):
        response['Body'] = ObservedBody(response['Body'], state)
    return response


def observe_manifest(*args, **kwargs):
    state = request.get()
    if state is not None:
        state['source_owner_grant'] = owner_storage_budget.current() is not None
    token = reading_source.set(state)
    try:
        return real_manifest(*args, **kwargs)
    finally:
        reading_source.reset(token)


def observe_prepare(*args, **kwargs):
    state = request.get()
    if state is not None:
        state['constructed_targets'] += 1
    return real_prepare(*args, **kwargs)


Path.open = observe_local
s3_live_store.Client.get_object = observe_remote
case_admin.source_manifest = observe_manifest
case_provision.prepare = observe_prepare


@app.get('/fixture/case-copy-owner')
def process(user=Depends(get_current_user)):
    return {'identity': process_identity_native.observe(os.getpid()),
            'reserved_bytes': node_storage_budget.observe()['reserved_bytes']}


class ObserveCopy:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get('headers', ()))
        if not scope.get('path', '').endswith(':copy') or b'x-fixture-observe-copy' not in headers:
            return await self.app(scope, receive, send)
        state = {'blocks': 0, 'source_bytes': 0, 'max_block': 0, 'constructed_targets': 0}
        token = request.set(state)

        async def observed_send(message):
            if message['type'] == 'http.response.start':
                state['status'] = message['status']
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            request.reset(token)
            record('copy-trace.json', state)


app = ObserveCopy(app)
'''


def digest_response(client, case, path):
    size, digest = 0, hashlib.sha256()
    with client.stream('GET', f'/cases/{case}/raw', params={'path': path}) as response:
        assert response.status_code == 200, (response.status_code, response.read()[:500])
        for block in response.iter_bytes(65536):
            size += len(block)
            digest.update(block)
    return {'size': size, 'sha256': digest.hexdigest()}


def read_observation(api, name):
    """Windows may briefly deny opening the just-installed trace file."""
    until = None
    while True:
        try:
            return wait_json(api, name)
        except PermissionError:
            if until is None:
                until = time.monotonic() + 4
            if time.monotonic() >= until:
                raise
            time.sleep(.03)


def exercise(api, store, report, baseline):
    source = api.create_case('Actual streamed clone source')
    workspace_id = api.call('GET', '/workspaces')[0]['id']
    owner = api.call('GET', '/fixture/case-copy-owner', api_prefix=False)
    report['api_process'] = owner['identity']
    _, expected = seed(api, store)
    report['large_source'] = expected
    original, updated = 'initial editable source', 'revised editable source'
    assert len(original) == len(updated)
    api.put(EDIT, original)
    headers = {'Authorization': 'Bearer ' + api.token}
    request = {'name': 'Case clone during real source hashing'}
    endpoint = api.prefix + ':copy'
    change = {'path': EDIT, 'content': updated, 'expected_sha256': hashlib.sha256(original.encode()).hexdigest()}
    with httpx.Client(base_url=api.base + '/api/v1', headers=headers, trust_env=False, timeout=150) as client:
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(client.post, endpoint, json=request, headers={'X-Fixture-Observe-Copy':'1'})
            try:
                observed = report['first_block'] = read_observation(api, 'first-file-block.json')
                assert 0 < observed['source_bytes'] <= 1024 * 1024 and not pending.done(), observed
                report['writer_available'] = writer_available(api)
                save = client.put(api.prefix + '/file', json=change, timeout=15)
                report['concurrent_save'] = {'status': save.status_code, 'document': save.json()}
            finally:
                (api.root / 'release-source-read').touch()
            result = pending.result(timeout=140)
        report['contested_copy'] = {'status': result.status_code, 'document': result.json()}
        report['trace'] = trace = read_observation(api, 'copy-trace.json')
        assert trace['source_bytes'] == expected['size'] and trace['max_block'] <= 1024 * 1024, trace
        assert report['writer_available'] is not baseline, report
        if baseline:
            assert trace['constructed_targets'] == 1, trace
            assert save.status_code == 409 and save.json()['detail']['code'] == 'storage_quota_busy', save.text
            assert result.status_code == 200, result.text
            clone = result.json()['id']
            copied = client.get(f'/cases/{clone}/raw', params={'path':EDIT})
            assert copied.status_code == 200 and copied.text == original
            retried_save = client.put(api.prefix + '/file', json=change)
            assert retried_save.status_code == 200, retried_save.text
        else:
            assert not observed['source_owner_grant'] and trace['constructed_targets'] == 0, trace
            assert save.status_code == 200 and result.status_code == 409, (save.text, result.text)
            assert isinstance(result.json()['detail'], str) and '源算例已变化' in result.json()['detail'], result.text
            listing = f'/workspaces/{workspace_id}/cases'
            assert [item['id'] for item in api.call('GET', listing)] == [source['id']]
            retry = client.post(endpoint, json=request)
            report['retry'] = {'status': retry.status_code, 'document': retry.json()}
            assert retry.status_code == 200, retry.text
            clone = retry.json()['id']
            rows = api.call('GET', listing)
            assert [item['id'] for item in rows if item['name'] == request['name']] == [clone], rows
            copied = client.get(f'/cases/{clone}/raw', params={'path':EDIT})
            assert copied.status_code == 200 and copied.text == updated
            outsider = api.call('POST', '/auth/register', {'email': uuid.uuid4().hex+'@example.com',
                'name': 'Different case owner', 'password': 'isolated-test-123'})
            foreign = client.post(endpoint, json={'name': 'Unauthorized clone'},
                                  headers={'Authorization':'Bearer ' + outsider['token']})
            report['foreign_copy_status'] = foreign.status_code
            assert foreign.status_code == 404, foreign.text
        report['clone_id'] = clone
        report['clone_large_readback'] = digest_response(client, clone, LARGE)
        assert report['clone_large_readback'] == expected
        current = client.get(api.prefix + '/raw', params={'path':EDIT})
        assert current.status_code == 200 and current.text == updated
    release = api.call('GET', '/fixture/case-copy-owner', api_prefix=False)
    report['node_reserved_after'] = release['reserved_bytes']
    assert report['node_reserved_after'] == 0


def run(backend, baseline=False):
    from app.services import process_identity_native

    api = IsolatedServer('case-copy-source-' + backend, app='clone_observation:app')
    name = api.root / 'case-copy-source-lock.json'
    report = {'passed': False, 'backend': backend, 'baseline': baseline, 'api_port': api.port,
              'real_model_calls': 0, 'mock_llm_calls': 0}
    context = ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) if backend == 's3' else nullcontext()
    print('CASE_COPY_SOURCE ' + str(api.root), flush=True)
    try:
        with context as store:
            configuration = store.settings() if store else {}
            if store is not None:
                report['s3_receipt'] = str(store.root / 'store.json')
            app_fixture = api.root / 'clone_observation.py'
            temp = api.root / 'native-temp'
            temp.mkdir()
            configuration.update(TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp))
            try:
                app_fixture.write_text('import os\nos.environ.update(' + repr(configuration) + ')\n' + FIXTURE, encoding='utf8')
                with api:
                    exercise(api, store, report, baseline)
            finally:
                app_fixture.unlink(missing_ok=True)
        if store is not None:
            report['s3_wait_returncode'] = store.proc.poll()
            assert report['s3_wait_returncode'] is not None
        report['api_wait_returncode'] = api.proc.poll()
        assert report['api_wait_returncode'] is not None
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
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:1400]}
        raise
    finally:
        name.write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    print('CASE_COPY_SOURCE_OK ' + str(name), flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    baseline = os.environ.get('VIBEFLOW_CASE_COPY_BASELINE') == '1'
    run('fs', baseline=baseline)
    if not baseline:
        run('s3')
