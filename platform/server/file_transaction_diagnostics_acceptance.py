"""Crash real file transactions and inspect their large retained copies."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import httpx

from s3testsupport import ObjectStore
from sandbox_normal_close_resource_smoke import RUNTIME, case_prefix, writer_available
from testsupport import IsolatedServer


SIZE = 40 * 1024 * 1024
TARGET = 'Ref/diagnostic-large.bin'
DICT_HISTORY = '0-caseDict/case-events.jsonl'
ORIGINAL = hashlib.sha256(bytes(SIZE)).hexdigest()
FIXTURE = r'''
import json
from contextlib import contextmanager
from pathlib import Path
import time

from app.main import app
from app import storage
from app.services import (case_read_capacity, file_transactions as local, local_file_edits,
                          node_storage_budget, s3_file_transactions as remote, s3_live_store,
                          s3_staging_transactions)
from app.services.request_deadline import current
from app.services.file_read import FileRead

root = Path(__file__).parent
local_apply = local._apply
publish_edit = local_file_edits.PreparedEdit.publish_files
remote_apply = remote._apply
capture = local._capture
finish_local = local._finish_local
get = s3_live_store.Client.get_object
deadline_scope = case_read_capacity.file_read_deadline
drain = s3_staging_transactions.drain
local_get = storage._fs_get
file_open = FileRead._open


def pause(size, name='read', copied=0):
    flag = root/('arm-'+name)
    if size != 40*1024*1024 or not flag.exists():
        return
    flag.unlink()
    pending = root/(name+'-observed.tmp')
    evidence = {'body_size': size, 'already_copied': copied}
    if (root/'short-recovery').exists() or (root/'short-write').exists():
        evidence['deadline_remaining'] = current().remaining()
    if name == 'remote-write':
        evidence['reserved_local_bytes'] = node_storage_budget.observe()['reserved_bytes']
    pending.write_text(json.dumps(evidence), encoding='utf8')
    pending.replace(root/(name+'-observed.json'))
    until = time.monotonic()+35
    while not (root/('release-'+name)).exists():
        if time.monotonic() >= until:
            raise TimeoutError('Read observation was not released')
        time.sleep(.025)


def capture_observed(path, destination=None, **kwargs):
    name = None
    if path.parts[-2:] == ('Ref', 'diagnostic-large.bin') and destination is not None:
        if '.file-write-staging' in destination.parts:
            if (root/'arm-live-write').exists() or (root/'exit-staged-copy').exists():
                name = 'live-write'
            elif (root/'arm-staged-write').exists():
                result = capture(path, destination, **kwargs)
                pause(result['size'], 'staged-write', destination.stat().st_size)
                return result
    if path.parts[-3:] == ('pending', 'before', '0'):
        if destination is not None and '.file-recovery-staging' in destination.parts and (root/'arm-stage').exists():
            name = 'stage'
        elif destination is None:
            pause(path.stat().st_size)
    if name is not None:
        size = path.stat().st_size
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as target:
            class ObservedCopy:
                def write(self, block):
                    count = target.write(block)
                    if name == 'live-write' and (root/'exit-staged-copy').exists():
                        (root/'exit-staged-copy').unlink()
                        os._exit(86)
                    pause(size, name, count)
                    return count
            observed = capture(path, stream=ObservedCopy(), **kwargs)
            target.flush()
            os.fsync(target.fileno())
            return observed
    return capture(path, destination, **kwargs)


def finish_observed(*args):
    if (root/'arm-recovery-exit').exists():
        (root/'arm-recovery-exit').unlink()
        os._exit(85)
    return finish_local(*args)


class Body:
    def __init__(self, body, size, name='read'):
        self.body, self.size, self.name = body, size, name

    def read(self, amount=None):
        if self.name == 'dict-history' and amount is None and (root/'arm-dict-history').exists():
            first = self.body.read(1024*1024)
            pause(self.size, self.name, len(first))
            return first + self.body.read()
        data = self.body.read(amount)
        if data:
            if self.name == 'remote-write' and (root/'exit-remote-stage').exists():
                (root/'exit-remote-stage').unlink()
                os._exit(86)
            pause(self.size, self.name, len(data))
        return data

    def close(self):
        self.body.close()


def get_observed(self, **args):
    result = get(self, **args)
    if '/file-transactions/objects/' in args['Key'] and args['Key'].endswith('/before/0'):
        result['Body'] = Body(result['Body'], result['ContentLength'])
    elif args['Key'].endswith('/Ref/diagnostic-large.bin') and (
            (root/'arm-remote-write').exists() or (root/'exit-remote-stage').exists()):
        result['Body'] = Body(result['Body'], result['ContentLength'], 'remote-write')
    elif args['Key'].endswith('/0-caseDict/case-events.jsonl') and (root/'arm-dict-history').exists():
        result['Body'] = Body(result['Body'], result['ContentLength'], 'dict-history')
    return result


def crash_at_apply(apply):
    def observed(case, directory, index, entry, side, expected):
        result = apply(case, directory, index, entry, side, expected)
        if side == 'after' and entry['path'] == 'Ref/diagnostic-large.bin' and (root/'arm-exit').exists():
            (root/'arm-exit').unlink()
            os._exit(84)
        return result
    return observed


def crash_at_publish(edit, case, pending):
    result = publish_edit(edit, case, pending)
    if (root/'arm-exit').exists() and any(r['path'] == 'Ref/diagnostic-large.bin'
                                       for r in result.document['plan']['files']):
        (root/'arm-exit').unlink()
        os._exit(84)
    return result


def deadline(code, label, **options):
    if code == 'file_recovery' and (root/'short-recovery').exists():
        options['seconds'] = 14
    if code == 'file_write' and (root/'short-write').exists():
        options['seconds'] = 8
    return deadline_scope(code, label, **options)


async def drain_observed(case, document):
    if (root/'exit-remote-committed').exists() and any(r['path'] == 'Ref/diagnostic-large.bin'
                                                     for r in document['plan']['files']):
        (root/'exit-remote-committed').unlink()
        os._exit(87)
    await drain(case, document)


def before_backup_send(request, **kwargs):
    if '/file-transactions/objects/' in request.url and request.url.endswith('/before/0'):
        pause(int(request.headers['Content-Length']), 'remote-backup')


def get_local_observed(prefix, path):
    if path == '0-caseDict/case-events.jsonl' and (root/'arm-dict-history').exists():
        selected = storage._fs_path(prefix, path)
        with selected.open('rb') as stream:
            return Body(stream, selected.stat().st_size, 'dict-history').read()
    return local_get(prefix, path)


@contextmanager
def open_observed(reader, start=None, end=None):
    with file_open(reader, start, end) as (stream, size):
        if not reader.remote and reader.path.parts[-2:] == ('0-caseDict', 'case-events.jsonl'):
            stream = Body(stream, size, 'dict-history')
        yield stream, size


local._capture = capture_observed
local._apply = crash_at_apply(local_apply)
local_file_edits.PreparedEdit.publish_files = crash_at_publish
local._finish_local = finish_observed
remote._apply = crash_at_apply(remote_apply)
s3_live_store.Client.get_object = get_observed
s3_staging_transactions.drain = drain_observed
case_read_capacity.file_read_deadline = deadline
storage._fs_get = get_local_observed
FileRead._open = open_observed
if storage._use_s3():
    storage.s3().meta.events.register('before-send.s3.PutObject', before_backup_send)
'''


def arm(api, name='read'):
    for path in (name+'-observed.json', 'release-'+name):
        (api.root/path).unlink(missing_ok=True)
    (api.root/('arm-'+name)).touch()


def observe(api, pending, lock_available, name='read'):
    until = time.monotonic()+30
    marker = api.root/(name+'-observed.json')
    while not marker.exists():
        if pending.done():
            response = pending.result()
            raise AssertionError((response.status_code, response.text))
        if time.monotonic() >= until:
            raise TimeoutError('Large journal image was not read')
        time.sleep(.025)
    observed = json.loads(marker.read_text(encoding='utf8'))
    observed['sqlite_writer_available'] = writer_available(api)
    assert observed['body_size'] == SIZE and observed['sqlite_writer_available'] == lock_available, observed
    return observed


def restart(api, code):
    assert api.proc.wait(timeout=10) == code
    original = api.proc.pid
    api.proc = subprocess.Popen(api.proc.args, cwd=api.root, env=api.env, stdout=api.log,
                                stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    until = time.monotonic()+40
    while time.monotonic() < until:
        try:
            if api.call('GET', '/api/health', api_prefix=False)['ok']:
                return {'exited_pid': original, 'fresh_pid': api.proc.pid, 'crash_code': code}
        except OSError:
            time.sleep(.1)
    raise TimeoutError('Cold API did not become ready')


def seed(api, store):
    prefix = case_prefix(api)
    path = api.root/'files'/prefix/TARGET if store is None else api.root/'diagnostic-source.bin'
    with path.open('wb') as stream:
        stream.truncate(SIZE)
    if store is not None:
        try:
            with path.open('rb') as stream:
                store.client.put_object(Bucket=store.bucket, Key=prefix+'/'+TARGET, Body=stream, IfNoneMatch='*')
        finally:
            path.unlink()


def expire_recovery(api, client, pool, *, local=False):
    flag = api.root/'short-recovery'
    flag.touch()
    name = 'stage' if local else 'read'
    arm(api, name)
    pending = pool.submit(client.get, api.prefix+'/file:version', params={'path': TARGET})
    try:
        try:
            evidence = observe(api, pending, lock_available=True, name=name)
            time.sleep(evidence['deadline_remaining']+.15)
            assert not pending.done()
        finally:
            (api.root/('release-'+name)).touch()
        timed_out = pending.result(timeout=50)
    finally:
        flag.unlink()
    assert timed_out.status_code == 504 and timed_out.json()['detail']['code'] == 'file_recovery_timeout', timed_out.text
    status = client.get(api.prefix+'/file-transactions').json()
    if local:
        assert status['status'] == 'pending' and status['database_committed'] is False, status
        assert evidence['already_copied'] > 0 and not list((api.root/'files/.file-recovery-staging').glob('*/owner.json'))
    else:
        assert status['status'] == 'recovery_required' and any(
            row['purpose'] == 'recovery' and row['state'] == 'drained' for row in status['operations']), status
    return {**evidence, 'http': 504, 'pending_journal_preserved': True}


def local_interleavings(api, client, pool, other):
    root = api.root/'files'/case_prefix(api)
    target = root/TARGET
    pending_journal = root/'.vibeflow/file-transactions/pending'
    staged_root = api.root/'files/.file-recovery-staging'
    counts = {}
    original = target.read_bytes()
    assert original == b'staged but uncommitted'
    for name in ('changed_live', 'second_exit'):
        arm(api, 'stage')
        if name == 'second_exit':
            request = pool.submit(client.post, api.prefix+'/file-transactions:recover')
        else:
            request = pool.submit(client.get, api.prefix+'/file:version', params={'path': TARGET})
        try:
            evidence = observe(api, request, lock_available=True, name='stage')
            assert 0 < evidence['already_copied'] <= 1024*1024
            assert list(staged_root.glob('*/owner.json'))
            assert list((api.root/'files/.node-storage-budget').glob('*.json'))
            write = client.put(f'/cases/{other}/file', json={'path': f'Ref/{name}.txt', 'content': 'save during local stage'})
            assert write.status_code == 200, write.text
            if name == 'changed_live':
                target.write_bytes(b'external change during preparation')
            else:
                (api.root/'arm-recovery-exit').touch()
        finally:
            (api.root/'release-stage').touch()
        if name == 'changed_live':
            result = request.result(timeout=60)
            assert result.status_code == 409 and result.json()['detail']['code'] == 'file_recovery_required', result.text
            assert target.read_bytes() == b'external change during preparation' and pending_journal.exists()
            target.write_bytes(original)
            evidence['unrelated_live_edit_preserved'] = True
        else:
            try:
                request.result(timeout=60)
            except httpx.TransportError:
                pass
            else:
                raise AssertionError('Expected actual process exit after replacement')
            evidence.update(restart(api, 85))
            assert target.stat().st_size == SIZE and pending_journal.exists()
            assert list(staged_root.glob('*/owner.json'))
        counts[name] = {**evidence, 'other_case_save': 200}
    return counts


def verify(store=None):
    backend = 's3' if store is not None else 'fs'
    api = IsolatedServer('file-transaction-diagnostics-'+backend, app='diagnostic_fixture:app')
    fixture = api.root/'diagnostic_fixture.py'
    if store is not None:
        store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status': 'Enabled'})
    fixture.write_text('import os\nos.environ.update('+repr(store.settings() if store else {})+')\n'+FIXTURE,
                       encoding='utf8')
    evidence = {'root': str(api.root), 'backend': backend, 'passed': False}
    try:
        with api:
            api.create_case('Cold file transaction')
            workspaces = api.call('GET', '/workspaces')
            other = api.call('POST', f"/workspaces/{workspaces[0]['id']}/cases", {'name': 'Sibling case'})['id']
            seed(api, store)
            endpoint = api.prefix
            with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization': 'Bearer '+api.token},
                              trust_env=False, timeout=100) as client, ThreadPoolExecutor(max_workers=1) as pool:
                (api.root/'arm-exit').touch()
                try:
                    client.put(endpoint+'/file', json={'path': TARGET, 'content': 'staged but uncommitted'})
                except httpx.TransportError:
                    pass
                else:
                    raise AssertionError('Expected actual API process exit during the write')
                evidence['restart'] = restart(api, 84)
                arm(api)
                pending = pool.submit(client.get, endpoint+'/file-transactions?include_preparations=true&preparation_limit=2')
                try:
                    evidence['diagnosis'] = observe(api, pending, lock_available=True)
                    write = client.put(f'/cases/{other}/file', json={'path': 'Ref/save.txt', 'content': 'parallel save'})
                    assert write.status_code == 200, write.text
                finally:
                    (api.root/'release-read').touch()
                diagnosed = pending.result(timeout=60)
                assert diagnosed.status_code == 200, diagnosed.text
                status = diagnosed.json()
                assert status['backend'] == backend and status['status'] == 'pending', status
                assert status['database_committed'] is False and status['preparations']['read_only'], status
                assert status['files'] == [{'path': TARGET, 'state': 'after', 'conflict': False}], status
                assert client.get(endpoint+'/file-transactions').json()['transaction_id'] == status['transaction_id']
                evidence['diagnosis'].update(read_only=True, other_case_save=200, pending=status['transaction_id'])

                evidence['timeout'] = expire_recovery(api, client, pool, local=store is None)
                if store is None:
                    evidence['local_stage'] = local_interleavings(api, client, pool, other)
                arm(api)
                recovered = pool.submit(client.get, endpoint+'/file:version', params={'path': TARGET})
                try:
                    evidence['automatic_recovery'] = observe(api, recovered, lock_available=True)
                    write = client.put(f'/cases/{other}/file', json={'path': 'Ref/recovery-save.txt', 'content': 'save during rollback'})
                    assert write.status_code == 200, write.text
                    evidence['automatic_recovery']['other_case_save'] = 200
                finally:
                    (api.root/'release-read').touch()
                result = recovered.result(timeout=60)
                assert result.status_code == 200, result.text
                assert result.json()['content_sha256'] == ORIGINAL and result.json()['size'] == SIZE
                assert client.get(endpoint+'/file-transactions').json()['status'] == 'idle'
                evidence['automatic_recovery']['restored_sha256'] = ORIGINAL
                if store is None:
                    assert not list((api.root/'files/.file-recovery-staging').glob('*/owner.json'))
                    assert not list((api.root/'files/.node-storage-budget').glob('*.json'))
                    evidence['private_staging_and_claims_cleared'] = True
        evidence['passed'] = True
    finally:
        fixture.unlink(missing_ok=True)
        (api.root/'file-transaction-diagnostics.json').write_text(json.dumps(evidence, indent=2), encoding='utf8')
    print('FILE_DIAGNOSTICS_PASS', backend, api.root, flush=True)
    return evidence


def measure_ordinary_write():
    api = IsolatedServer('live-file-write-lock', app='diagnostic_fixture:app')
    fixture = api.root/'diagnostic_fixture.py'
    fixture.write_text('import os\n'+FIXTURE, encoding='utf8')
    evidence = {'root': str(api.root), 'passed': False, 'backend': 'fs'}
    try:
        with api:
            api.create_case('Save an ordinary file with an existing 40 MiB file')
            seed(api, None)
            workspace = api.call('GET', '/workspaces')[0]['id']
            other = api.call('POST', f'/workspaces/{workspace}/cases', {'name': 'Parallel save'})['id']
            arm(api, 'live-write')
            body = {'path': TARGET, 'content': 'ordinary save', 'expected_sha256': ORIGINAL}
            with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization': 'Bearer '+api.token},
                              trust_env=False, timeout=45) as client, ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(client.put, api.prefix+'/file', json=body)
                try:
                    evidence['before_image_read'] = observe(api, pending, lock_available=True, name='live-write')
                    assert evidence['before_image_read']['already_copied'] == 1024*1024
                    scratch = api.root/'files/.file-write-staging'
                    assert list(scratch.glob('*/owner.json'))
                    assert list((api.root/'files/.node-storage-budget').glob('*.json'))
                    for label, prefix in (('other_case', f'/cases/{other}'), ('same_case', api.prefix)):
                        saved = client.put(prefix+'/file', json={'path': f'Ref/{label}.txt', 'content': 'parallel'})
                        assert saved.status_code == 200, saved.text
                        evidence[label+'_http'] = saved.status_code
                finally:
                    (api.root/'release-live-write').touch()
                saved = pending.result(timeout=35)
                assert saved.status_code == 200, saved.text
                version = client.get(api.prefix+'/file:version', params={'path': TARGET}).json()
                assert version['content_sha256'] == hashlib.sha256(b'ordinary save').hexdigest()
                original = api.root/'files'/case_prefix(api)/'.vibeflow/file-transactions/history'
                backups = list(original.glob('*/before/0'))
                assert len(backups) == 1 and backups[0].stat().st_size == SIZE
                with backups[0].open('rb') as stream:
                    assert hashlib.file_digest(stream, 'sha256').hexdigest() == ORIGINAL
                seed(api, None)
                arm(api, 'staged-write')
                stale = pool.submit(client.put, api.prefix+'/file', json={**body, 'content': 'stale edit'})
                try:
                    evidence['stale_copy'] = observe(api, stale, lock_available=True, name='staged-write')
                    assert evidence['stale_copy']['already_copied'] == SIZE
                    winner = client.put(api.prefix+'/file', json={**body, 'content': 'newer edit'})
                    assert winner.status_code == 200, winner.text
                finally:
                    (api.root/'release-staged-write').touch()
                rejected = stale.result(timeout=35)
                assert rejected.status_code == 409 and rejected.json()['detail']['code'] == 'file_conflict', rejected.text
                assert (api.root/'files'/case_prefix(api)/TARGET).read_bytes() == b'newer edit'
                assert not list(scratch.glob('*/owner.json'))
                assert not list((api.root/'files/.node-storage-budget').glob('*.json'))
                evidence.update(stale_http=409, same_target_winner_http=200, newer_edit_preserved=True)
                seed(api, None)
                target = api.root/'files'/case_prefix(api)/TARGET
                arm(api, 'staged-write')
                stale = pool.submit(client.put, api.prefix+'/file', json=body)
                try:
                    observe(api, stale, lock_available=True, name='staged-write')
                    previous = target.stat()
                    with target.open('r+b') as stream:
                        stream.write(b'x')
                    os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
                    current = target.stat()
                    assert (previous.st_ino, previous.st_size, previous.st_mtime_ns) == (
                        current.st_ino, current.st_size, current.st_mtime_ns)
                finally:
                    (api.root/'release-staged-write').touch()
                conflict = stale.result(timeout=35)
                assert conflict.status_code == 409 and conflict.json()['detail']['code'] == 'file_conflict', conflict.text
                with target.open('rb') as stream:
                    assert stream.read(1) == b'x'
                evidence['same_inode_size_and_mtime_conflict_http'] = conflict.status_code
                seed(api, None)
                (api.root/'exit-staged-copy').touch()
                try:
                    client.put(api.prefix+'/file', json=body)
                except httpx.TransportError:
                    pass
                else:
                    raise AssertionError('Expected actual process exit during staging')
                evidence['stage_crash'] = restart(api, 86)
                assert list(scratch.glob('*/owner.json'))
                assert not (original.parent/'pending').exists()
                version = client.get(api.prefix+'/file:version', params={'path': TARGET}).json()
                assert version['content_sha256'] == ORIGINAL
                retried = client.put(api.prefix+'/file', json=body)
                assert retried.status_code == 200, retried.text
                assert not list(scratch.glob('*/owner.json'))
                assert not list((api.root/'files/.node-storage-budget').glob('*.json'))
                evidence['stage_crash'].update(original_sha256=ORIGINAL, retry_http=200, staging_and_claims_cleared=True)
                evidence.update(passed=True, write_http=200, archived_before_image_bytes=SIZE)
    finally:
        fixture.unlink(missing_ok=True)
        (api.root/'live-file-write.json').write_text(json.dumps(evidence, indent=2), encoding='utf8')
    print('LIVE_FILE_WRITE_STAGING', api.root, flush=True)
    return evidence


def measure_remote_write(store, *, baseline):
    api = IsolatedServer('remote-file-write-lock', app='diagnostic_fixture:app')
    fixture = api.root/'diagnostic_fixture.py'
    store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status': 'Enabled'})
    fixture.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE, encoding='utf8')
    evidence = {'root': str(api.root), 'passed': False, 'backend': 's3', 'baseline': baseline}
    try:
        with api:
            api.create_case('Save an ordinary S3 file with a 40 MiB previous version')
            seed(api, store)
            workspace = api.call('GET', '/workspaces')[0]['id']
            other = api.call('POST', f'/workspaces/{workspace}/cases', {'name': 'Parallel save'})['id']
            arm(api, 'remote-write')
            if not baseline:
                arm(api, 'remote-backup')
            with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization': 'Bearer '+api.token},
                              trust_env=False, timeout=60) as client, ThreadPoolExecutor(max_workers=1) as pool:
                body = {'path': TARGET, 'content': 'ordinary S3 save'}
                saving = pool.submit(client.put, api.prefix+'/file', json=body)
                try:
                    evidence['before_image_read'] = observe(api, saving, lock_available=not baseline, name='remote-write')
                    assert evidence['before_image_read']['already_copied'] == 1024*1024
                    if not baseline:
                        assert evidence['before_image_read']['reserved_local_bytes'] >= SIZE
                        assert active_remote_rows(api)[0] == 1
                        parallel = client.put(f'/cases/{other}/file', json={'path': 'Ref/parallel.txt', 'content': 'parallel'})
                        assert parallel.status_code == 200, parallel.text
                        competing = client.put(api.prefix+'/file', json={'path': 'Ref/conflict.txt', 'content': 'busy'})
                        assert competing.status_code == 409 and competing.json()['detail']['code'] == 'file_sync_busy', competing.text
                        evidence.update(other_case_http=parallel.status_code, same_case_http=competing.status_code)
                finally:
                    (api.root/'release-remote-write').touch()
                if not baseline:
                    try:
                        evidence['backup_put'] = observe(api, saving, lock_available=True, name='remote-backup')
                        assert active_remote_rows(api) == (1, SIZE)
                        evidence['backup_put']['durable_claim_bytes'] = SIZE
                    finally:
                        (api.root/'release-remote-backup').touch()
                saved = saving.result(timeout=60)
                assert saved.status_code == 200, saved.text
                version = client.get(api.prefix+'/file:version', params={'path': TARGET}).json()
                assert version['content_sha256'] == hashlib.sha256(b'ordinary S3 save').hexdigest()
                if not baseline:
                    assert active_remote_rows(api) == (0, 0)
                    evidence['compatibility'] = remote_edit_conditions(api, client, body, version)
                    api.case_id = api.call('POST', f'/workspaces/{workspace}/cases', {'name': 'Expired save'})['id']
                    api.prefix = f'/cases/{api.case_id}'
                    seed(api, store)
                    evidence['timeout'] = expire_remote_write(api, client, pool, body)
                    for mode, code, final in (
                            ('exit-remote-stage', 86, ORIGINAL),
                            ('arm-exit', 84, ORIGINAL),
                            ('exit-remote-committed', 87, hashlib.sha256(b'ordinary S3 save').hexdigest())):
                        api.case_id = api.call('POST', f'/workspaces/{workspace}/cases', {'name': mode})['id']
                        api.prefix = f'/cases/{api.case_id}'
                        seed(api, store)
                        evidence[mode] = crash_remote_write(api, client, body, mode, code, final)
                evidence.update(passed=True, write_http=200)
    finally:
        fixture.unlink(missing_ok=True)
        (api.root/'remote-file-write.json').write_text(json.dumps(evidence, indent=2), encoding='utf8')
    print('LIVE_REMOTE_FILE_WRITE', api.root, flush=True)
    return evidence


def active_remote_rows(api):
    with closing(sqlite3.connect(api.root/'test.sqlite', timeout=2)) as db:
        operations = db.execute("SELECT count(*) FROM case_storage_operations WHERE case_id=? AND state!='settled'",
                                (api.case_id,)).fetchone()[0]
        retained = db.execute("SELECT coalesce(sum(remaining_bytes),0) FROM storage_write_reservations "
                              "WHERE case_id=? AND state='active'", (api.case_id,)).fetchone()[0]
        return operations, retained


def remote_edit_conditions(api, client, body, version):
    url = api.prefix+'/file'
    revision = version['dict_revision']
    assert client.put(url, json={**body, 'expected_sha256': 'invalid'}).status_code == 422
    for conditions, expected in (({'expected_sha256': ORIGINAL}, 'file_conflict'),
                                 ({'expected_sha256': None}, 'file_conflict'),
                                 ({'expected_revision': revision+1}, 'revision_conflict')):
        response = client.put(url, json={**body, **conditions})
        assert response.status_code == 409, response.text
        detail = response.json()['detail']
        assert detail['code'] == expected and detail['current_revision'] == revision, detail
    path = 'Ref/binary.bin'
    upload = client.post(api.prefix+'/upload', params={'path': path, 'expected_absent': True},
                         files={'file': ('binary.bin', b'\x00\xff', 'application/octet-stream')})
    assert upload.status_code == 200, upload.text
    readback = client.get(api.prefix+'/raw', params={'path': path})
    assert readback.content == b'\x00\xff' and readback.headers['Content-Type'] == 'application/octet-stream'
    deleted = client.delete(url, params={'path': path, 'expected_sha256': upload.json()['content_sha256']})
    assert deleted.status_code == 200, deleted.text
    assert client.get(api.prefix+'/raw', params={'path': path}).status_code == 404
    assert active_remote_rows(api) == (0, 0)
    return {'invalid_hash_http': 422, 'stale_hash_http': 409, 'stale_revision_http': 409,
            'requires_absence_http': 409, 'upload_http': 200, 'delete_http': 200}


def expire_remote_write(api, client, pool, body):
    arm(api, 'remote-write')
    flag = api.root/'short-write'
    flag.touch()
    saving = pool.submit(client.put, api.prefix+'/file', json=body)
    try:
        proof = observe(api, saving, lock_available=True, name='remote-write')
        time.sleep(proof['deadline_remaining']+.4)
        assert not saving.done(), 'Copy slot was released while the actual S3 body was still open'
        assert active_remote_rows(api)[0] == 1
    finally:
        (api.root/'release-remote-write').touch()
    expired = saving.result(timeout=60)
    flag.unlink()
    assert expired.status_code == 504 and expired.json()['detail']['code'] == 'file_write_timeout', expired.text
    intact = client.get(api.prefix+'/file:version', params={'path': TARGET})
    assert intact.status_code == 200 and intact.json()['content_sha256'] == ORIGINAL, intact.text
    assert active_remote_rows(api) == (0, 0)
    retry = client.put(api.prefix+'/file', json={**body, 'expected_sha256': ORIGINAL})
    assert retry.status_code == 200, retry.text
    return {**proof, 'http': 504, 'original_preserved': True, 'retry': 200, 'claims_settled': True}


def crash_remote_write(api, client, body, mode, code, final):
    (api.root/mode).touch()
    try:
        client.put(api.prefix+'/file', json=body)
    except httpx.TransportError:
        pass
    else:
        raise AssertionError('Expected actual remote write process exit: '+mode)
    proof = restart(api, code)
    assert active_remote_rows(api)[0] == 1
    restored = client.get(api.prefix+'/file:version', params={'path': TARGET})
    assert restored.status_code == 200 and restored.json()['content_sha256'] == final, restored.text
    assert api.call('GET', api.prefix+'/file-transactions')['status'] == 'idle'
    assert active_remote_rows(api) == (0, 0)
    return {**proof, 'recovery_http': 200, 'live_sha256': final, 'claims_settled': True}


def seed_dictionary_history(api, store):
    prefix = case_prefix(api)
    padding = b'{"type":"fixture.retained","note":"' + b'x'*(SIZE-512) + b'"}\n'
    if store is None:
        target = api.root/'files'/prefix/DICT_HISTORY
        original = target.read_bytes()
        contents = original + padding + b' '*(SIZE-len(original)-len(padding))
        target.write_bytes(contents)
    else:
        args = {'Bucket':store.bucket, 'Key':prefix+'/'+DICT_HISTORY}
        response = store.client.get_object(**args)
        with closing(response['Body']) as stream:
            original = stream.read()
        contents = original + padding + b' '*(SIZE-len(original)-len(padding))
        store.client.put_object(**args, IfMatch=response['ETag'], Body=contents)
    assert len(contents) == SIZE
    return hashlib.sha256(contents).hexdigest()


def history_end(api, store, digest):
    prefix = case_prefix(api)
    if store is None:
        stream = (api.root/'files'/prefix/DICT_HISTORY).open('rb')
    else:
        stream = store.client.get_object(Bucket=store.bucket,Key=prefix+'/'+DICT_HISTORY)['Body']
    measured = hashlib.sha256()
    with closing(stream):
        remaining = SIZE
        while remaining:
            block = stream.read(min(1024*1024,remaining))
            assert block, 'Old dictionary history length changed'
            measured.update(block)
            remaining -= len(block)
        appended = stream.read(4096)
        assert not stream.read(1)
    assert measured.hexdigest() == digest
    return json.loads(appended)


def dictionary_endpoints(api, client, case_id):
    prefix = f'/cases/{case_id}'
    initial = api.call('GET', prefix+'/caseDict')
    full = initial['content'].replace('StandardCase','Shared editor case')
    revision = initial['dict_revision']
    checks = [
        ('PUT','',{'content':full}),
        ('PUT','/block',{'block':'intent','entries':{'userIntent':'coordinated edits'}}),
        ('PATCH','/meta',{'changes':{'caseName':'dictionary-meta'}}),
        ('PUT','/extensions/journal',{'version':'1','enabled':True,'settings':{}}),
        ('DELETE','/extensions/journal',None),
        ('POST','/errors',{'id':'E1','title':'test','stage':'mesh','category':'meshQuality',
                           'severity':'error','message':'test'}),
        ('PATCH','/errors/E1',{'status':'verified','resolution':'checked','verification':'done'}),
    ]
    for method,suffix,body in checks:
        options = {'params':{'expected_revision':revision}} if body is None else {
            'json':{**body,'expected_revision':revision}}
        result = client.request(method,prefix+'/caseDict'+suffix,**options)
        assert result.status_code == 200, (suffix,result.text)
        revision += 1
        assert result.json()['dict_revision'] == revision, result.text
    no_op = client.patch(prefix+'/caseDict/meta',json={'changes':{},'expected_revision':revision})
    assert no_op.status_code == 200 and no_op.json()['dict_revision'] == revision, no_op.text
    stale = client.put(prefix+'/caseDict/block',json={'block':'intent','entries':{},'expected_revision':revision-1})
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'revision_conflict', stale.text
    assert api.call('GET',prefix+'/provenance')['chain_valid']
    return {'successful_edits':len(checks),'no_op_revision':revision,'stale_revision_http':409}


def measure_dictionary_write(store, *, baseline):
    backend = 'fs' if store is None else 's3'
    api = IsolatedServer('case-dictionary-write-'+backend, app='diagnostic_fixture:app')
    fixture = api.root/'diagnostic_fixture.py'
    fixture.write_text('import os\nos.environ.update('+repr(store.settings() if store else {})+')\n'+FIXTURE,
                       encoding='utf8')
    evidence = {'root':str(api.root), 'passed':False, 'backend':backend, 'baseline':baseline}
    try:
        with api:
            api.create_case('Edit caseDict with a 40 MiB timeline')
            workspace = api.call('GET','/workspaces')[0]['id']
            other = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Parallel dictionary edit'})['id']
            old = api.call('GET',api.prefix+'/caseDict')
            old_history_sha = seed_dictionary_history(api,store)
            arm(api,'dict-history')
            body = {'block':'intent','entries':{'userIntent':'timeline edit proof'},'expected_revision':old['dict_revision'],
                    'expected_sha256':old['content_sha256']}
            with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization':'Bearer '+api.token},
                              trust_env=False,timeout=90) as client, ThreadPoolExecutor(max_workers=1) as pool:
                editing = pool.submit(client.put,api.prefix+'/caseDict/block',json=body)
                try:
                    evidence['history_read'] = observe(api,editing,lock_available=not baseline,name='dict-history')
                    if not baseline:
                        other_edit = client.put(f'/cases/{other}/file',json={'path':'Ref/parallel.txt','content':'parallel'})
                        assert other_edit.status_code == 200, other_edit.text
                        evidence['other_case_http'] = other_edit.status_code
                finally:
                    (api.root/'release-dict-history').touch()
                saved = editing.result(timeout=90)
                assert saved.status_code == 200, saved.text
                receipt = saved.json()
                assert receipt['dict_revision'] == old['dict_revision']+1
                edited = api.call('GET',api.prefix+'/caseDict')
                assert edited['dict_revision'] == receipt['dict_revision']
                assert edited['content_sha256'] == receipt['content_sha256']
                assert edited['parsed']['intent']['userIntent'] == 'timeline edit proof'
                event = history_end(api,store,old_history_sha)
                assert event['revision'] == receipt['dict_revision'] and event['type'] == 'caseDict.updated'
                provenance = api.call('GET',api.prefix+'/provenance')
                assert provenance['chain_valid'] and provenance['events'][0]['casedict_sha256'] == receipt['content_sha256']
                if not baseline:
                    evidence['edit_endpoints'] = dictionary_endpoints(api,client,other)
                evidence.update(passed=True,write_http=200,revision=receipt['dict_revision'],
                                content_sha256=receipt['content_sha256'],old_history_sha256=old_history_sha)
    finally:
        fixture.unlink(missing_ok=True)
        (api.root/'case-dictionary-write.json').write_text(json.dumps(evidence,indent=2),encoding='utf8')
    print('CASE_DICTIONARY_WRITE',backend,api.root,flush=True)
    return evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--storage', choices=('fs', 's3', 'both'), default='both')
    parser.add_argument('--observe-write-lock', action='store_true')
    parser.add_argument('--observe-remote-write', choices=('baseline', 'reserved'))
    parser.add_argument('--observe-dictionary-write', choices=('baseline','staged'))
    args = parser.parse_args()
    report = {'passed': False, 'actual_model_calls': 0, 'mock_llm_calls': 0, 'runs': []}
    try:
        if args.observe_dictionary_write:
            baseline = args.observe_dictionary_write == 'baseline'
            if args.storage in {'fs','both'}:
                report['runs'].append(measure_dictionary_write(None,baseline=baseline))
            if args.storage in {'s3','both'}:
                with ObjectStore(executable=RUNTIME,volume_max=24,authenticated=True) as store:
                    report['runs'].append(measure_dictionary_write(store,baseline=baseline))
        elif args.observe_remote_write:
            with ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) as store:
                report['runs'].append(measure_remote_write(store, baseline=args.observe_remote_write == 'baseline'))
        elif args.observe_write_lock:
            report['runs'].append(measure_ordinary_write())
        elif args.storage in {'fs', 'both'}:
            report['runs'].append(verify())
        if not args.observe_dictionary_write and not args.observe_remote_write and not args.observe_write_lock and args.storage in {'s3', 'both'}:
            with ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) as store:
                report['runs'].append(verify(store))
        report['passed'] = True
    finally:
        args.report.write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
