"""Actual numbered PDF history downloads over FS and authenticated SeaweedFS."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import os
import shutil
import socket
import time
import uuid

import httpx
from pypdf import PdfReader
from pptx import Presentation

from latex_artifact_streaming_smoke import expected_bytes, make_pdf, save_archive
from professional_launch_smoke import settled
from report_artifact_streaming_smoke import wait_for, writer_available
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


PDF = 'E-report/numbered.pdf'
SOURCE = 'Ref/large-source.pdf'
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
pdf = 'E-report/numbered.pdf'
observation = ContextVar('derivative_download_observation', default=None)
read_file = artifact_publication.read_file
chunks = file_read.FileRead.chunks


def wait_for_edit_window(state):
    if state['measure'] != 'full' or (root / 'first-file-block').exists():
        return
    (root / 'first-file-block').touch()
    until = time.monotonic() + 20
    while not (root / 'release-verification').exists():
        if time.monotonic() >= until:
            raise TimeoutError('The isolated fixture did not release the actual read')
        time.sleep(.02)


def observed_read_file(prefix, row, path):
    state = observation.get()
    if state is None or path != pdf:
        return read_file(prefix, row, path)
    state['whole_reads'] += 1
    wait_for_edit_window(state)
    tracemalloc.start()
    try:
        payload = read_file(prefix, row, path)
        state['source_bytes'] += len(payload)
        return payload
    finally:
        state['peak'] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()


def observed_chunks(self, *args, **kwargs):
    state = observation.get()
    key = self.location['Key'] if self.remote else str(self.path).replace('\\', '/')
    if state is None or not key.endswith('/files/' + pdf):
        yield from chunks(self, *args, **kwargs)
        return
    state['streams'] += 1
    state['remote'] = self.remote
    with closing(chunks(self, *args, **kwargs)) as blocks:
        for block in blocks:
            wait_for_edit_window(state)
            state['source_bytes'] += len(block)
            state['max_chunk'] = max(state['max_chunk'], len(block))
            yield block


artifact_publication.read_file = observed_read_file
file_read.FileRead.chunks = observed_chunks
if hasattr(reports, '_derivative_artifact_response'):
    serve = reports._derivative_artifact_response

    def observed_response(prefix, row, path, ranges, version, download):
        state = observation.get()
        if state is None or path != pdf:
            return serve(prefix, row, path, ranges, version, download)
        tracemalloc.start()
        try:
            return serve(prefix, row, path, ranges, version, download)
        finally:
            state['peak'] = tracemalloc.get_traced_memory()[1]
            state['reserved'] = download._reserved if download is not None else 0
            tracemalloc.stop()

    reports._derivative_artifact_response = observed_response


@app.get('/fixture/derivative/{case_id}/binding')
async def binding(case_id: int, publication_id: str | None = None,
                  user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    result = {'prefix': case.s3_prefix, 'process': process_identity_native.observe(os.getpid())}
    if publication_id is not None:
        row = await db.get(ArtifactPublication, publication_id)
        assert row.case_id == case.id and row.kind.startswith('pdf-number:')
        result['archive'] = row.immutable_path
    return result


@app.get('/fixture/derivative:capacity')
async def capacity(user=Depends(get_current_user)):
    return {'reserved': (await asyncio.to_thread(node_storage_budget.observe))['reserved_bytes']}


class InspectDownload:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get('headers', []))
        measured = headers.get(b'x-fixture-measure')
        if not scope.get('path', '').endswith('/report/derivative-artifact') or measured is None:
            return await self.app(scope, receive, send)
        state = {'measure': measured.decode('ascii'), 'whole_reads': 0, 'streams': 0,
                 'source_bytes': 0, 'max_chunk': 0, 'body_bytes': 0}
        token = observation.set(state)

        async def observed_send(message):
            if message['type'] == 'http.response.start':
                state['status'] = message['status']
            elif message['type'] == 'http.response.body':
                state['body_bytes'] += len(message.get('body', b''))
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            observation.reset(token)
            with (root / 'derivative-download-trace.jsonl').open('a', encoding='utf-8') as log:
                log.write(json.dumps(state, sort_keys=True) + '\n')


app = InspectDownload(app)
'''


def upload(api, client, root, headers, local, path):
    """Use the ordinary resumable upload for the producer's large real input."""
    with local.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
        source.seek(0)
        session = api.call('POST', api.prefix + '/uploads', {
            'path': path, 'size': local.stat().st_size, 'sha256': digest, 'overwrite': True,
        })
        offset = 0
        for block in iter(lambda: source.read(8 * 1024 * 1024), b''):
            result = client.put(root + '/uploads/' + session['id'], params={'offset': offset},
                content=block, headers={**headers, 'Content-Type': 'application/octet-stream'})
            assert result.status_code == 200, (result.status_code, result.text[:700])
            offset += len(block)
        api.call('POST', api.prefix + '/uploads/' + session['id'] + ':complete')


def build(api, action, body):
    created = api.call('POST', api.prefix + action, body)
    job = settled(api, created['job_id'], timeout=240)
    assert job['status'] == 'succeeded', job
    publication = job['attempt']['outputs']['publication']
    assert publication['status'] == 'committed', publication
    return {'publication_id': publication['id'], 'job_id': job['id'],
            'process_stop': job['attempt']['process_stop']}


def get_pdf(client, endpoint, params, headers, target):
    digest, size = hashlib.sha256(), 0
    with target.open('xb') as saved, client.stream('GET', endpoint, params=params, headers=headers) as response:
        if response.status_code != 200:
            raise AssertionError((response.status_code, response.read()[:800]))
        assert response.headers['Content-Type'] == 'application/pdf'
        assert response.headers['Content-Disposition'] == "attachment; filename*=UTF-8''numbered.pdf"
        for block in response.iter_bytes(65536):
            saved.write(block)
            digest.update(block)
            size += len(block)
        assert int(response.headers['Content-Length']) == size
        return {'size': size, 'sha256': digest.hexdigest(), 'version': response.headers['X-Vibeflow-File-Version']}


def compatibility(api, client, root, endpoint, headers):
    template = api.root / 'template.pptx'
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])
    deck.save(template)
    with template.open('rb') as content:
        result = client.post(root + '/upload', headers=headers, params={'path': 'Ref/template.pptx'},
                             files={'file': (template.name, content)})
    assert result.status_code == 200, (result.status_code, result.text[:500])
    ppt = build(api, '/ppt:build', {'title': 'Actual derivative history', 'template': 'Ref/template.pptx',
        'output': 'E-report/deck.pptx', 'slides': [{'title': 'Actual slide', 'text': 'Actual content'}]})
    ppt_headers = client.head(endpoint, params={'publication_id': ppt['publication_id'], 'path': 'E-report/deck.pptx'},
                             headers=headers)
    assert ppt_headers.status_code == 200 and int(ppt_headers.headers['Content-Length']) > 1000
    editable = 'E-report/report.generated.md'
    previous = '# Previous operator draft\n'
    api.put(editable, previous)
    draft = build(api, '/report:compose?wait=false', {'output': editable, 'overwrite': True})
    metadata = api.call('GET', api.prefix + '/report/derivative?publication_id=' + draft['publication_id'])
    assert [item['role'] for item in metadata['files']] == ['original', 'previous-draft'], metadata
    archived = [client.get(endpoint, params={'publication_id': draft['publication_id'], 'path': item['path']},
                           headers=headers) for item in metadata['files']]
    assert [(result.status_code, hashlib.sha256(result.content).hexdigest()) for result in archived] == [
        (200, item['sha256']) for item in metadata['files']]
    assert archived[1].content == previous.encode()
    hidden = client.get(endpoint, params={'publication_id': draft['publication_id'],
        'path': metadata['files'][0]['path'].rsplit('/', 1)[0] + '/result.json'}, headers=headers)
    assert hidden.status_code == 404, hidden.text
    return {'ppt': ppt, 'ppt_head_status': ppt_headers.status_code, 'draft': draft,
            'draft_original_and_previous': [result.status_code for result in archived], 'private_result': hidden.status_code}


def exercise(api, store, report, baseline):
    case = api.create_case('Published numbered PDF derivative')
    root = api.base + '/api/v1' + api.prefix
    endpoint = root + '/report/derivative-artifact'
    headers = {'Authorization': 'Bearer ' + api.token}
    binding = f'/fixture/derivative/{case["id"]}/binding'
    report['api_process'] = api.call('GET', binding, api_prefix=False)['process']
    with httpx.Client(trust_env=False, timeout=120) as client:
        source = make_pdf(api.root, 'valid-original', SIZE)
        upload(api, client, root, headers, source, SOURCE)
        historical = report['previous'] = build(api, '/pdf:number', {'input': SOURCE, 'output': PDF})
        api.call('DELETE', api.prefix + '/file?path=' + PDF)
        small = make_pdf(api.root, 'small-current', 4096)
        upload(api, client, root, headers, small, SOURCE)
        latest = report['current'] = build(api, '/pdf:number', {'input': SOURCE, 'output': PDF})
        assert historical['publication_id'] != latest['publication_id']
        params = {'path': PDF, 'publication_id': historical['publication_id']}
        retained = api.root / 'actual-historical-numbered.pdf'
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(get_pdf, client, endpoint, params, {**headers, 'X-Fixture-Measure': 'full'}, retained)
            try:
                wait_for(api.root / 'first-file-block')
                report['writer_available_during_verification'] = writer_available(api)
                assert report['writer_available_during_verification'] is not baseline, report
                if not baseline:
                    api.put('Ref/parallel-edit.txt', 'Same-case writer committed during the actual historical stream')
                    assert not pending.done()
            finally:
                (api.root / 'release-verification').touch()
            original = report['original'] = pending.result(timeout=100)
        assert original['size'] >= SIZE, original
        decoded = PdfReader(retained, strict=True)
        assert len(decoded.pages) == 1 and '1 / 1' in decoded.pages[0].extract_text()
        assert decoded.pages[0]['/Resources']['/XObject']['/Im0']['/Width'] == 4096

        head = client.head(endpoint, params=params, headers={**headers, 'X-Fixture-Measure': 'head'})
        assert head.status_code == 200 and head.content == b'' and int(head.headers['Content-Length']) == original['size']
        assert head.headers['X-Vibeflow-File-Version'] == original['version']
        suffix = client.get(endpoint, params=params, headers={**headers, 'X-Fixture-Measure': 'range',
            'Range': 'bytes=-97', 'X-Vibeflow-File-Version': original['version']})
        assert suffix.status_code == 206 and suffix.headers['Content-Range'] == (
            f"bytes {original['size'] - 97}-{original['size'] - 1}/{original['size']}")
        assert suffix.content == expected_bytes(retained, original['size'] - 97, original['size'])
        invalid = client.get(endpoint, params=params, headers={**headers, 'Range': 'bytes=999999999-'})
        assert invalid.status_code == 416 and invalid.headers['Content-Range'] == f"bytes */{original['size']}"
        changed = client.get(endpoint, params=params, headers={**headers, 'X-Vibeflow-File-Version': '0' * 64})
        assert changed.status_code == 409 and changed.json()['detail']['code'] == 'file_changed_during_read'
        report['transport'] = {'head': head.status_code, 'range': suffix.status_code, 'range_bytes': len(suffix.content),
                               'unsatisfied_range': invalid.status_code, 'stale_version': changed.status_code}
        if not baseline:
            owned_current = client.get(endpoint, params={'publication_id': latest['publication_id'], 'path': PDF}, headers=headers)
            assert owned_current.status_code == 200 and len(owned_current.content) < 1024 * 1024
            assert hashlib.sha256(owned_current.content).hexdigest() != original['sha256']
            outsider = api.call('POST', '/auth/register', {'email': uuid.uuid4().hex + '@example.com',
                'name': 'Other derivative reader', 'password': 'isolated-test-123'})
            assert client.head(endpoint, params=params).status_code == 401
            assert client.get(endpoint, params=params, headers={'Authorization': 'Bearer ' + outsider['token']}).status_code == 404
            hidden = client.get(endpoint, params={**params, 'path': 'Ref/large-source.pdf'}, headers=headers)
            assert hidden.status_code == 404
            archived = api.call('GET', binding + '?publication_id=' + historical['publication_id'], api_prefix=False)
            relative = archived['archive'] + '/files/' + PDF
            replacement = api.root / 'same-length-corruption.pdf'
            shutil.copyfile(retained, replacement)
            with replacement.open('r+b') as stream:
                stream.seek(5 * 1024 * 1024)
                deliberately_changed = bytes([stream.read(1)[0] ^ 0xff])
                stream.seek(-1, os.SEEK_CUR)
                stream.write(deliberately_changed)
            try:
                save_archive(api, store, archived['prefix'], relative, replacement)
                rejected = client.get(endpoint, params=params, headers={**headers, 'Range': 'bytes=-97'})
                assert rejected.status_code == 409 and rejected.json()['detail']['code'] == 'evidence_invalid'
                assert client.head(endpoint, params=params, headers=headers).status_code == 409
            finally:
                save_archive(api, store, archived['prefix'], relative, retained)
                replacement.unlink(missing_ok=True)
            restored = client.get(endpoint, params=params, headers={**headers, 'Range': 'bytes=-97'})
            report['corrupted_range_and_restored'] = [rejected.status_code, restored.status_code]
            assert restored.status_code == 206 and restored.content == suffix.content, (
                restored.status_code, restored.content[:700])
            report['compatibility'] = compatibility(api, client, root, endpoint, headers)

    trace = api.root / 'derivative-download-trace.jsonl'
    end = time.monotonic() + 4
    while time.monotonic() < end:
        state = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        if len(state) >= 3:
            break
        time.sleep(.03)
    report['trace'] = {item['measure']: item for item in state}
    for name, code, length in [('full', 200, original['size']), ('head', 200, 0), ('range', 206, 97)]:
        item = report['trace'][name]
        assert (item['status'], item['body_bytes'], item['source_bytes']) == (code, length, original['size']), item
        if baseline:
            assert item['whole_reads'] == 1 and item['peak'] >= original['size'] and item['streams'] == 0, item
        else:
            assert item['whole_reads'] == 0 and item['streams'] == 1 and item['remote'] is (store is not None), item
            assert item['max_chunk'] <= 65536 and item['peak'] < original['size'] // 4, item
            assert item['reserved'] == (original['size'] if name == 'full' else 0), item
    report['node_reserved_after'] = api.call('GET', '/fixture/derivative:capacity', api_prefix=False)['reserved']
    assert report['node_reserved_after'] == 0


def run(backend, baseline=False):
    from app.services import process_identity_native

    api = IsolatedServer('derivative-artifact-' + backend, app='derivative_fixture:app')
    target = api.root / 'report-derivative-artifact-streaming.json'
    report = {'passed': False, 'backend': backend, 'baseline': baseline, 'api_port': api.port,
              'real_model_calls': 0, 'mock_llm_calls': 0}
    context = ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) if backend == 's3' else nullcontext()
    print('REPORT_DERIVATIVE_ARTIFACT ' + str(api.root), flush=True)
    try:
        with context as store:
            settings = store.settings() if store else {}
            if store is not None:
                report['s3_receipt'] = str(store.root / 'store.json')
            scratch = api.root / 'native-temp'
            scratch.mkdir()
            settings.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
            module = api.root / 'derivative_fixture.py'
            try:
                module.write_text('import os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE, encoding='utf8')
                with api:
                    exercise(api, store, report, baseline)
            finally:
                module.unlink(missing_ok=True)
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
            if (report['api_exit']['state'] == 'exited' and report['api_port_closed']) or time.monotonic() >= until:
                break
            time.sleep(.05)
        report['api_os_observation_pending'] = not (report['api_exit']['state'] == 'exited' and report['api_port_closed'])
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:1300]}
        raise
    finally:
        target.write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    print('REPORT_DERIVATIVE_ARTIFACT_OK ' + str(target), flush=True)


if __name__ == '__main__':
    if os.environ.get('VIBEFLOW_REPORT_DERIVATIVE_BASELINE') == '1':
        run('fs', baseline=True)
    else:
        if os.environ.get('VIBEFLOW_REPORT_DERIVATIVE_S3_ONLY') != '1':
            run('fs')
        run('s3')
