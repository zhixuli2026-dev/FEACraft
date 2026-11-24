"""Actual v1 backup, SQLite writer contention and normal recovery of bytes/history."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
import zipfile

import httpx

from professional_launch_smoke import settled
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


SIZE = 40*1024*1024
LARGE = 'Ref/archive-large.bin'
EDIT = 'Ref/ordinary-save.txt'
ADDED = 'Ref/late-directory/new-entry.txt'
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
from app.services import backup, file_read, node_storage_budget, process_identity_native, s3_live_store

root = Path(__file__).parent
observed = ContextVar('observed_backup_request', default=None)
packing = ContextVar('observed_backup_pack', default=None)
read_range = file_read.FileRead.read
remote_get = s3_live_store.Client.get_object
pack = backup._pack_backup
source_versions = getattr(backup, '_source_versions', None)


def record(name, value):
    target = root / name
    scratch = target.with_suffix('.writing')
    scratch.write_text(json.dumps(value), encoding='utf-8')
    scratch.replace(target)


def hold(state, marker):
    record(state['mode'] + '-' + marker + '.json', state)
    until = time.monotonic() + 18
    while not (root / (state['mode'] + '-release')).exists():
        if time.monotonic() >= until:
            raise TimeoutError('Actual backup pack read was not released')
        time.sleep(.025)


def observe_body(state, block):
    state['source_reads'] += 1
    state['source_bytes'] += len(block)
    if state['source_reads'] == 1:
        hold(state, 'first')


def observed_read(self, *args, **kwargs):
    data = read_range(self, *args, **kwargs)
    state = packing.get()
    if state is not None and not self.remote and str(self.path).replace('\\','/').endswith('/Ref/archive-large.bin'):
        observe_body(state, data)
    return data


class ReadObservation:
    def __init__(self, body, state):
        self.body = body
        self.state = state

    def __getattr__(self, name):
        return getattr(self.body, name)

    def read(self, amount):
        data = self.body.read(amount)
        if data:
            observe_body(self.state, data)
        return data


def observed_get(self, **kwargs):
    response = remote_get(self, **kwargs)
    state = packing.get()
    if state is not None and kwargs['Key'].endswith('/Ref/archive-large.bin'):
        response['Body'] = ReadObservation(response['Body'], state)
    return response


def observed_pack(*args, **kwargs):
    state = observed.get()
    marker = packing.set(state if state is not None and state['mode'] != 'late' else None)
    try:
        return pack(*args, **kwargs)
    finally:
        packing.reset(marker)


def observed_versions(*args, **kwargs):
    state = observed.get()
    if state is not None and state['mode'] == 'late':
        hold(state, 'inventory')
    return source_versions(*args, **kwargs)


backup._pack_backup = observed_pack
file_read.FileRead.read = observed_read
s3_live_store.Client.get_object = observed_get
if source_versions is not None:
    backup._source_versions = observed_versions


@app.get('/fixture/workspace-backup-owner')
def owner(user=Depends(get_current_user)):
    return {'birth':process_identity_native.observe(os.getpid()),
            'reserved':node_storage_budget.observe()['reserved_bytes']}


class ObserveDownload:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        label = dict(scope.get('headers', ())).get(b'x-fixture-hold-backup', b'').decode('ascii')
        if label not in {'normal', 'changed', 'late'} or not scope.get('path', '').endswith('/backup'):
            return await self.app(scope, receive, send)
        state = {'mode':label, 'source_reads':0, 'source_bytes':0}
        token = observed.set(state)

        async def watch(message):
            if message['type'] == 'http.response.start':
                state['status'] = message['status']
            await send(message)

        try:
            await self.app(scope, receive, watch)
        finally:
            observed.reset(token)
            record(label + '-trace.json', state)


app = ObserveDownload(app)
'''


def seed(api, store, *, prefix=None):
    source = api.root / 'actual-backup-asset.bin'
    digest = hashlib.sha256()
    with source.open('xb') as stream:
        for _ in range(SIZE // 65536):
            data = os.urandom(65536)
            digest.update(data)
            stream.write(data)
    if prefix is None:
        uri = (api.root / 'test.sqlite').resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as db:
            prefix = db.execute('SELECT s3_prefix FROM cases WHERE id=?', (api.case_id,)).fetchone()[0]
    if store:
        with source.open('rb') as body:
            store.client.put_object(Bucket=store.bucket, Key=prefix.rstrip('/') + '/' + LARGE,
                                    Body=body, IfNoneMatch='*')
    else:
        root = (api.root / 'files' / prefix).resolve()
        assert root.is_relative_to(api.root.resolve())
        target = root / LARGE
        assert target.parent.is_dir() and not target.exists()
        shutil.copyfile(source, target)
    return prefix, {'size':SIZE, 'sha256':digest.hexdigest()}


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


def wait_json(api, name):
    path = api.root / name
    until = time.monotonic() + 75
    while not path.exists():
        if time.monotonic() >= until:
            raise TimeoutError('Missing actual backup trace: ' + name)
        time.sleep(.035)
    return json.loads(path.read_text(encoding='utf-8'))


def request_zip(client, endpoint, output, *, label=None):
    headers = {'X-Fixture-Hold-Backup':label} if label else {}
    with client.stream('GET', endpoint, headers=headers) as response:
        if response.status_code != 200:
            response.read()
            return {'status':response.status_code, 'detail':response.json()['detail'],
                    'retry_after':response.headers.get('Retry-After')}
        assert response.headers['Content-Type'] == 'application/zip'
        assert response.headers['Content-Disposition'].endswith('-backup.zip"')
        with output.open('xb') as file:
            for block in response.iter_bytes(65536):
                file.write(block)
        size = output.stat().st_size
        assert int(response.headers['Content-Length']) == size
        return {'status':200, 'zip_bytes':size}


def check_zip(path, expected, title, command, edit, *, added=False):
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == ['manifest.json']
        with archive.open('manifest.json') as source:
            document = json.load(source)
    assert document['schema'] == 'vibeflow.workspace-backup.v1'
    assert len(document['cases']) == 1 and any(job['command'] == command and job['status'] == 'succeeded'
                                             for job in document['jobs'])
    assert any(conversation['title'] == title for conversation in document['conversations'])
    rows = {row['path']:row for row in document['files']}
    data = base64.b64decode(rows[LARGE]['data'], validate=True)
    assert len(data) == expected['size'] and hashlib.sha256(data).hexdigest() == expected['sha256'] == rows[LARGE]['sha256']
    assert base64.b64decode(rows[EDIT]['data'], validate=True) == edit.encode()
    if added:
        assert base64.b64decode(rows[ADDED]['data'], validate=True) == b'actual new directory member'
    else:
        assert ADDED not in rows


def add_directory_member(api, store, prefix):
    if store:
        store.client.put_object(Bucket=store.bucket, Key=prefix.rstrip('/') + '/' + ADDED,
                                Body=b'actual new directory member', IfNoneMatch='*')
        return
    root = (api.root / 'files' / prefix).resolve()
    assert root.is_relative_to(api.root.resolve())
    member = root / ADDED
    member.parent.mkdir()
    with member.open('xb') as body:
        body.write(b'actual new directory member')


def check_restored(client, output, expected, title):
    with output.open('rb') as source:
        response = client.post('/workspaces:restore', files={'file':('backup.zip', source, 'application/zip')})
    assert response.status_code == 200, (response.status_code, response.text[:300])
    workspace = response.json()['workspace_id']
    cases = client.get(f'/workspaces/{workspace}/cases').json()
    assert len(cases) == 1
    case_id = cases[0]['id']
    digest, size = hashlib.sha256(), 0
    with client.stream('GET', f'/cases/{case_id}/raw', params={'path':LARGE}) as body:
        assert body.status_code == 200, body.status_code
        for block in body.iter_bytes(65536):
            size += len(block)
            digest.update(block)
    assert {'size':size, 'sha256':digest.hexdigest()} == expected
    assert any(row['title'] == title for row in client.get('/conversations', params={'caseId':case_id}).json())
    return {'workspace_id':workspace, 'case_id':case_id, 'large_file':expected}


def retry_backup_after_drain(api, client, endpoint, output, report):
    attempts = report['retry_attempts'] = []
    until = time.monotonic() + 15
    while True:
        result = request_zip(client, endpoint, output)
        attempts.append(result)
        if result['status'] == 200:
            return result
        assert (result['status'] == 429 and isinstance(result['detail'], dict)
                and result['detail'].get('code') == 'archive_busy'), result
        delay = int(result['retry_after'])
        if time.monotonic() + delay >= until:
            raise TimeoutError('The previous backup did not release its archive slot')
        time.sleep(delay)
        report['reserved_during_retry'] = api.call('GET', '/fixture/workspace-backup-owner', api_prefix=False)['reserved']


def exercise(api, store, report, baseline):
    api.create_case('Actual backup while editing')
    report['api_process'] = api.call('GET', '/fixture/workspace-backup-owner', api_prefix=False)['birth']
    prefix, expected = seed(api, store)
    report['source'] = expected
    title, command = 'Backup retained conversation', 'cmd /c echo BACKUP_RETAINED_HISTORY'
    api.call('POST', '/conversations', {'case_id':api.case_id, 'title':title})
    job = api.call('POST', api.prefix + '/jobs', {'command':command, 'runtime':'windows'})
    done = settled(api, job['id'], timeout=120)
    assert done['status'] == 'succeeded' and done['attempt']['process_stop']['verified'] is True
    report['job_stop'] = done['attempt']['process_stop']
    old = 'before backup'
    new = 'saved while backup was packing'
    api.put(EDIT, old)
    workspace = api.call('GET', '/workspaces')[0]['id']
    endpoint = f'/workspaces/{workspace}/backup'
    params = {'path':EDIT, 'content':new, 'expected_sha256':hashlib.sha256(old.encode()).hexdigest()}
    with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization':'Bearer ' + api.token},
                      trust_env=False, timeout=130) as client:
        normal = api.root / 'initial.zip'
        if not baseline:
            assert request_zip(client, endpoint, normal)['status'] == 200
            check_zip(normal, expected, title, command, old)

        label = 'normal' if baseline else 'changed'
        contested = api.root / 'contested.zip'
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(request_zip, client, endpoint, contested, label=label)
            try:
                block = wait_json(api, label + '-first.json')
                assert block['source_reads'] == 1 and block['source_bytes'] > 0, block
                report['writer_available'] = writer_available(api)
                save = client.put(api.prefix + '/file', json=params, timeout=10)
                report['save_during_pack'] = {'status':save.status_code, 'body':save.json()}
            finally:
                (api.root / (label + '-release')).touch()
            download = report['contested_get'] = pending.result(timeout=130)
        report['contested_trace'] = wait_json(api, label + '-trace.json')
        assert report['writer_available'] is not baseline
        if baseline:
            assert save.status_code == 409 and save.json()['detail']['code'] == 'storage_quota_busy', save.text
            assert download['status'] == 200
            check_zip(contested, expected, title, command, old)
            retry = client.put(api.prefix + '/file', json=params)
            assert retry.status_code == 200, retry.text
        else:
            assert save.status_code == 200 and download['status'] == 409, (save.text, download)
            assert not contested.exists()
            external = api.root / 'external.zip'
            with ThreadPoolExecutor(max_workers=1) as workers:
                validating = workers.submit(request_zip, client, endpoint, external, label='late')
                try:
                    wait_json(api, 'late-inventory.json')
                    add_directory_member(api, store, prefix)
                finally:
                    (api.root / 'late-release').touch()
                late = report['late_directory_change'] = validating.result(timeout=130)
            assert late['status'] == 409 and '源文件清单已变化' in late['detail'], late
            assert not external.exists()

        latest = api.root / 'latest.zip'
        retry_backup_after_drain(api, client, endpoint, latest, report)
        check_zip(latest, expected, title, command, new, added=not baseline)
        report['restored'] = check_restored(client, latest, expected, title)
        outsider = api.call('POST', '/auth/register', {'email':uuid.uuid4().hex+'@example.com',
            'name':'Outside backup owner','password':'isolated-test-123'})
        assert client.get(endpoint, headers={'Authorization':'Bearer ' + outsider['token']}).status_code == 404
    report['final_reserved'] = api.call('GET', '/fixture/workspace-backup-owner', api_prefix=False)['reserved']
    assert report['final_reserved'] == 0


def run(backend, baseline):
    api = IsolatedServer('workspace-backup-lock-'+backend, app='observed_workspace_backup:app')
    report = {'passed':False, 'baseline':baseline, 'backend':backend, 'api_port':api.port,
              'real_model_calls':0, 'mock_llm_calls':0}
    context = ObjectStore(executable=RUNTIME, volume_max=16, authenticated=True) if backend == 's3' else nullcontext()
    print('WORKSPACE_BACKUP_LOCK ' + str(api.root), flush=True)
    try:
        with context as store:
            settings = store.settings() if store else {}
            scratch = api.root / 'native-temp'
            scratch.mkdir()
            settings.update(TMP=str(scratch), TEMP=str(scratch), TMPDIR=str(scratch))
            module = api.root / 'observed_workspace_backup.py'
            try:
                module.write_text('import os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE, encoding='utf-8')
                with api:
                    exercise(api, store, report, baseline)
            finally:
                module.unlink(missing_ok=True)
            report['api_wait_returncode'] = api.proc.poll()
            assert report['api_wait_returncode'] is not None
        if store:
            assert store.proc.poll() is not None
            report['s3_receipt'] = str(store.root / 'store.json')
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__, 'message':str(error)[:1000]}
        raise
    finally:
        (api.root / 'workspace-backup-lock.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('WORKSPACE_BACKUP_LOCK_OK ' + str(api.root), flush=True)


if __name__ == '__main__':
    baseline = os.environ.get('VIBEFLOW_WORKSPACE_BACKUP_BASELINE') == '1'
    run('fs', baseline)
    if not baseline:
        run('s3', baseline)
