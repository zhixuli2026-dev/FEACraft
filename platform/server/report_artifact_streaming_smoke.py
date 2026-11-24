"""Read actual native Pandoc report artifacts over real FS and authenticated S3."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import struct
import time
import uuid
import zlib

import httpx

from latex_artifact_streaming_smoke import save_archive
from professional_launch_smoke import settled
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


HTML = 'E-report/build/report.html'
SIZE = 40 * 1024 * 1024
RUNTIME = SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed'
FIXTURE = r'''
import asyncio
from contextlib import closing
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time
import tracemalloc

from fastapi import Depends
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.models import ArtifactPublication
from app.routers import reports
from app.routers.workspaces import get_owned_case
from app.services import artifact_publication, file_read, node_storage_budget, process_identity_native

root = Path(__file__).parent
html = 'E-report/build/report.html'
probe = ContextVar('report_download_observation', default=None)
whole = artifact_publication.read_file
stream = file_read.FileRead.chunks


def hold(state):
    if not state['pause'] or (root / 'first-file-block').exists():
        return
    (root / 'first-file-block').touch()
    until = time.monotonic() + 20
    while not (root / 'release-verification').exists():
        if time.monotonic() >= until:
            raise TimeoutError('Fixture did not release verification')
        time.sleep(.02)


def observed_whole(prefix, row, path):
    state = probe.get()
    if state is None or path != html:
        return whole(prefix, row, path)
    state['reader'] = 'whole'
    hold(state)
    tracemalloc.start()
    try:
        data = whole(prefix, row, path)
        state['bytes'] = len(data)
        return data
    finally:
        state['peak'] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()


def observed_chunks(self, *args, **kwargs):
    state = probe.get()
    key = self.location['Key'] if self.remote else str(self.path).replace('\\', '/')
    if state is None or not key.endswith('/files/' + html):
        yield from stream(self, *args, **kwargs)
        return
    state['reader'] = 'stream'
    state['streams'] += 1
    with closing(stream(self, *args, **kwargs)) as blocks:
        for block in blocks:
            hold(state)
            state['bytes'] += len(block)
            state['max_chunk'] = max(state['max_chunk'], len(block))
            yield block


artifact_publication.read_file = observed_whole
file_read.FileRead.chunks = observed_chunks
if hasattr(reports, '_report_artifact_response'):
    serve = reports._report_artifact_response

    def observed_response(prefix, row, path, download):
        state = probe.get()
        if state is None or path != html:
            return serve(prefix, row, path, download)
        tracemalloc.start()
        try:
            return serve(prefix, row, path, download)
        finally:
            state['peak'] = tracemalloc.get_traced_memory()[1]
            state['reserved'] = download._reserved
            tracemalloc.stop()

    reports._report_artifact_response = observed_response


@app.get('/fixture/report/{case_id}/binding')
async def binding(case_id: int, publication_id: str | None = None,
        user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    result = {'prefix': case.s3_prefix, 'process': process_identity_native.observe(os.getpid())}
    if publication_id is not None:
        row = await db.get(ArtifactPublication, publication_id)
        assert row.case_id == case.id and row.kind == 'report'
        result['archive'] = row.immutable_path
    return result


@app.get('/fixture/report:capacity')
async def capacity(user=Depends(get_current_user)):
    return {'reserved': (await asyncio.to_thread(node_storage_budget.observe))['reserved_bytes']}


class InspectDownload:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get('headers', []))
        if not scope.get('path', '').endswith('/report/artifact') or b'x-fixture-measure' not in headers:
            return await self.app(scope, receive, send)
        state = {'pause': b'x-fixture-pause-verification' in headers, 'reader':'none',
                 'streams': 0, 'bytes': 0, 'max_chunk': 0}
        token = probe.set(state)

        async def observed_send(message):
            if message['type'] == 'http.response.start':
                state['status'] = message['status']
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            probe.reset(token)
            with (root / 'report-download-trace.jsonl').open('a', encoding='utf-8') as log:
                log.write(json.dumps(state, sort_keys=True) + '\n')


app = InspectDownload(app)
'''


def make_png(target):
    """A real RGB PNG with incompressible pixels and bounded fixture generation."""
    width, height = 2048, 1800
    compressor = zlib.compressobj(level=0)
    with target.open('xb') as body:
        def write_block(kind, data):
            body.write(struct.pack('>I', len(data)))
            body.write(kind)
            body.write(data)
            body.write(struct.pack('>I', zlib.crc32(data, zlib.crc32(kind))))

        body.write(b'\x89PNG\r\n\x1a\n')
        write_block(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
        for _ in range(height):
            block = compressor.compress(b'\0' + os.urandom(width * 3))
            if block:
                write_block(b'IDAT', block)
        write_block(b'IDAT', compressor.flush())
        write_block(b'IEND', b'')
    assert target.stat().st_size < 16 * 1024 * 1024


def report_job(api):
    submitted = api.call('POST', api.prefix + '/report:build', {})
    job = settled(api, submitted['job_id'], timeout=240)
    assert job['status'] == 'succeeded', job
    publication = job['attempt']['outputs']['publication']
    assert publication['status'] == 'committed', publication
    return {'job_id': job['id'], 'publication_id': publication['id'],
            'process_stop': job['attempt']['process_stop']}


def get_html(client, endpoint, params, headers, retained=None):
    digest, size = hashlib.sha256(), 0
    with (retained.open('xb') if retained is not None else nullcontext()) as target:
        with client.stream('GET', endpoint, params=params, headers=headers) as response:
            if response.status_code != 200:
                raise AssertionError((response.status_code, response.read()[:800]))
            assert response.headers['Content-Type'] == 'text/html; charset=utf-8'
            assert response.headers['Content-Disposition'] == 'attachment; filename="report.html"'
            for block in response.iter_bytes(65536):
                if target is not None:
                    target.write(block)
                digest.update(block)
                size += len(block)
            assert int(response.headers['Content-Length']) == size
    return {'sha256': digest.hexdigest(), 'size': size}


def writer_available(api):
    with closing(sqlite3.connect(api.root / 'test.sqlite', timeout=.15)) as connection:
        try:
            connection.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as error:
            if 'locked' not in str(error):
                raise
            return False
        connection.rollback()
        return True


def wait_for(path):
    until = time.monotonic() + 22
    while not path.exists():
        if time.monotonic() >= until:
            raise TimeoutError('Missing real observation: ' + path.name)
        time.sleep(.03)


def exercise(api, store, report, baseline):
    case = api.create_case('Published Pandoc download')
    root = api.base + '/api/v1' + api.prefix
    endpoint = root + '/report/artifact'
    headers = {'Authorization': 'Bearer ' + api.token}
    measured = {**headers, 'X-Fixture-Measure': '1'}
    binding = f'/fixture/report/{case["id"]}/binding'
    report['api_process'] = api.call('GET', binding, api_prefix=False)['process']
    with httpx.Client(trust_env=False, timeout=120) as client:
        for index in range(3):
            target = api.root / f'owned-image-{index}.png'
            make_png(target)
            with target.open('rb') as source:
                response = client.post(root + '/upload', headers=headers, params={'path': 'Ref/' + target.name},
                                       files={'file': (target.name, source, 'image/png')})
            assert response.status_code == 200, (response.status_code, response.text[:300])
        api.put('E-report/report.md', '# Actual archived native report\n\n' + '\n\n'.join(
            f'![Actual fixture {number}](../Ref/owned-image-{number}.png)' for number in range(3)))
        old = report['previous'] = report_job(api)
        api.put('E-report/report.md', '# Latest native report\n\nA small replacement.\n')
        latest = report['current'] = report_job(api)
        assert old['publication_id'] != latest['publication_id']
        params = {'path': HTML, 'publication_id': old['publication_id']}

        retained = api.root / 'historical-native-report.html'
        paused = {**measured, 'X-Fixture-Pause-Verification': '1'}
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(get_html, client, endpoint, params, paused, retained)
            try:
                wait_for(api.root / 'first-file-block')
                report['writer_available_during_verification'] = writer_available(api)
                assert report['writer_available_during_verification'] is not baseline, report
                if not baseline:
                    api.put('Ref/parallel-file.txt', 'Real same-case edit during archive verification')
                    assert not pending.done()
            finally:
                (api.root / 'release-verification').touch()
            report['historical_html'] = pending.result(timeout=100)
        original = report['historical_html']
        assert original['size'] >= SIZE, original
        current = get_html(client, endpoint, {'path': HTML}, headers)
        assert current['size'] < 1024 * 1024 and current != original
        outsider = api.call('POST', '/auth/register', {'email': uuid.uuid4().hex+'@example.com',
            'name': 'Other report reader', 'password': 'isolated-test-123'})
        assert client.get(endpoint, params=params,
                          headers={'Authorization': 'Bearer ' + outsider['token']}).status_code == 404
        hidden = {**params, 'path': 'E-report/build/report-toolchain.json'}
        assert client.get(endpoint, params=hidden, headers=headers).status_code == 404

        archived = api.call('GET', binding + '?publication_id=' + old['publication_id'], api_prefix=False)
        corrupt = api.root / 'same-size-replacement.html'
        shutil.copyfile(retained, corrupt)
        with corrupt.open('r+b') as body:
            body.seek(-16, os.SEEK_END)
            body.write(bytes([body.read(1)[0] ^ 0xff]))
        relative = archived['archive'] + '/files/' + HTML
        try:
            save_archive(api, store, archived['prefix'], relative, corrupt)
            rejected = client.get(endpoint, params=params, headers=headers)
            assert rejected.status_code == 409 and rejected.json()['detail']['code'] == 'evidence_invalid'
            assert api.call('GET', '/fixture/report:capacity', api_prefix=False)['reserved'] == 0
        finally:
            save_archive(api, store, archived['prefix'], relative, retained)
            corrupt.unlink(missing_ok=True)
        assert get_html(client, endpoint, params, headers) == original
    trace_path = api.root / 'report-download-trace.jsonl'
    time_limit = time.monotonic() + 3
    while not trace_path.exists() and time.monotonic() < time_limit:
        time.sleep(.03)
    report['trace'] = json.loads(trace_path.read_text(encoding='utf-8').splitlines()[0])
    trace = report['trace']
    assert trace['bytes'] == original['size'] and trace['status'] == 200, trace
    if baseline:
        assert trace['reader'] == 'whole' and trace['peak'] >= original['size'], trace
    else:
        assert trace['reader'] == 'stream' and trace['streams'] == 1, trace
        assert trace['max_chunk'] <= 65536 and trace['peak'] < original['size'] // 4, trace
        assert trace['reserved'] >= original['size'], trace
    assert api.call('GET', '/fixture/report:capacity', api_prefix=False)['reserved'] == 0


def run(backend, baseline=False):
    from app.services import process_identity_native

    api = IsolatedServer('report-artifact-' + backend, app='report_download_fixture:app')
    name = 'report-artifact-streaming.json'
    report = {'passed': False, 'backend': backend, 'baseline': baseline,
              'real_model_calls': 0, 'mock_llm_calls': 0}
    context = ObjectStore(executable=RUNTIME, volume_max=16, authenticated=True) if backend == 's3' else nullcontext()
    print('REPORT_ARTIFACT_STREAMING ' + str(api.root), flush=True)
    try:
        with context as store:
            settings = store.settings() if store else {}
            scratch = api.root / 'native-temp'
            scratch.mkdir()
            settings.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
            module = api.root / 'report_download_fixture.py'
            try:
                module.write_text('import os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE, encoding='utf-8')
                with api:
                    exercise(api, store, report, baseline)
            finally:
                module.unlink(missing_ok=True)
        if store is not None:
            assert store.proc.poll() is not None
            report['s3_receipt'] = str(store.root / 'store.json')
        report['api_wait_returncode'] = api.proc.poll()
        assert report['api_wait_returncode'] is not None
        # Windows retains the process object until the last handle is closed.
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
            if report['api_exit']['state'] == 'exited' and report['api_port_closed']:
                break
            if time.monotonic() >= until:
                break
            time.sleep(.05)
        report['api_os_observation_pending'] = not (
            report['api_exit']['state'] == 'exited' and report['api_port_closed'])
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:1300]}
        raise
    finally:
        (api.root / name).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('REPORT_ARTIFACT_STREAMING_OK ' + str(api.root / name), flush=True)


if __name__ == '__main__':
    baseline = os.environ.get('VIBEFLOW_REPORT_ARTIFACT_BASELINE') == '1'
    run('fs', baseline=baseline)
    if not baseline:
        run('s3')
