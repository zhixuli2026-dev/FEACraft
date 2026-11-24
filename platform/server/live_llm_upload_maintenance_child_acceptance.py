"""Real maintenance and DeepSeek evidence readback against isolated FS or SeaweedFS."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlencode
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'platform' / 'server'))
from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
from live_llm_job_repeat_acceptance import main, write
from live_llm_outbox_restart_acceptance import restart


FIXTURE = r'''
import os
import time
from fastapi import Depends
from app import storage
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import s3_uploads, storage_maintenance as maintenance, uploads
from app.services.case_write import CaseWriteConflict

maintenance.UPLOAD_PAGE_SIZE = 2
get_settings().storage_maintenance_enabled = (root/'run-upload-maintenance').exists()
get_settings().storage_maintenance_interval_seconds = 1
(root/'upload-maintenance-pid').write_text(str(os.getpid()))

actual_maintain_accounts = maintenance.maintain_accounts
async def observed_maintain_accounts(stop):
    if (root/'pause-scheduled-maintenance').exists():
        (root/'scheduled-maintenance-idle').touch()
        return
    await actual_maintain_accounts(stop)
maintenance.maintain_accounts = observed_maintain_accounts

def upload_trace(value):
    with (root/'upload-maintenance-trace.jsonl').open('a', encoding='utf8') as trace:
        trace.write(json.dumps({'pid':os.getpid(), **value})+'\n')

actual_page = maintenance.upload_page
def observed_page(prefix, after):
    pause = root/'pause-upload-enumeration'
    if pause.is_file() and pause.read_text() == prefix:
        (root/'upload-enumeration-paused').touch()
        deadline = time.monotonic() + 26
        while not (root/'allow-upload-enumeration').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Test did not release upload enumeration')
            time.sleep(.02)
    items, cursor = actual_page(prefix, after)
    upload_trace({'kind':'page','prefix':prefix,'after':after,'items':items,'cursor':cursor})
    return items, cursor
maintenance.upload_page = observed_page

actual_cleanup = uploads.cleanup_completed
def observed_cleanup(prefix, upload_id, receipt, *args):
    hold = root/'delay-completed-cleanup'
    if hold.is_file() and hold.read_text() == upload_id:
        upload_trace({'kind':'injected_pre_cleanup_fault','id':upload_id})
        return False
    result = actual_cleanup(prefix, upload_id, receipt, *args)
    upload_trace({'kind':'cleanup','prefix':prefix,'id':upload_id,'result':result})
    return result
uploads.cleanup_completed = observed_cleanup

actual_remote_cleanup = s3_uploads.cleanup_completed
def observed_remote_cleanup(prefix, upload_id, receipt):
    hold = root/'delay-completed-cleanup'
    if hold.is_file() and hold.read_text() == upload_id:
        upload_trace({'kind':'injected_pre_cleanup_fault','id':upload_id})
        return False
    return actual_remote_cleanup(prefix, upload_id, receipt)
s3_uploads.cleanup_completed = observed_remote_cleanup

actual_apply = s3_uploads.apply_completed
def observed_apply(prefix, upload_id, receipt, observation):
    result = actual_apply(prefix, upload_id, receipt, observation)
    upload_trace({'kind':'cleanup','prefix':prefix,'id':upload_id,'result':result})
    return result
s3_uploads.apply_completed = observed_apply

actual_conditional = s3_uploads._conditional
def observed_conditional(prefix, path, data, etag, metadata):
    if not (path.endswith('/data') and '/chunks/' in path and data == b''):
        return actual_conditional(prefix, path, data, etag, metadata)
    identifier = path.split('/')[2]
    pause = root/'pause-before-conditional'
    if pause.is_file() and pause.read_text() == identifier:
        stamp = root/'upload-conditional-path'
        stamp.write_text(path)
        stamp.replace(root/'upload-conditional-paused')
        deadline = time.monotonic() + 18
        while not (root/'allow-conditional').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Test did not release the conditional upload cleanup')
            time.sleep(.025)
    try:
        result = actual_conditional(prefix, path, data, etag, metadata)
    except CaseWriteConflict:
        upload_trace({'kind':'conditional_conflict','id':identifier,'path':path})
        raise
    exit_marker = root/'exit-after-conditional'
    if exit_marker.is_file() and exit_marker.read_text() == identifier:
        exit_marker.unlink()
        upload_trace({'kind':'exit_after_conditional','id':identifier,'path':path})
        os._exit(79)
    return result
s3_uploads._conditional = observed_conditional

actual_observe_part = uploads._observe_completed_part
def pause_part(upload_id):
    pause = root/'pause-upload-part'
    release = root/'allow-upload-part'
    if pause.is_file() and pause.read_text() == upload_id and not release.exists():
        (root/'upload-part-paused').touch()
        deadline = time.monotonic() + 18
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Test did not release upload part verification')
            time.sleep(.025)

def observed_part(prefix, upload_id, receipt):
    pause_part(upload_id)
    return actual_observe_part(prefix, upload_id, receipt)
uploads._observe_completed_part = observed_part

actual_snapshot = s3_uploads.snapshot
def observed_snapshot(prefix, path, **options):
    if '/chunks/' in path and path.endswith('/data') and options.get('limit', 0) > 1024 * 1024:
        pause_part(path.split('/')[2])
    return actual_snapshot(prefix, path, **options)
s3_uploads.snapshot = observed_snapshot

actual_unlink = Path.unlink
def interrupt_fs_cleanup(path, *args, **kwargs):
    marker = root/'interrupt-retired-cleanup'
    if marker.is_file() and path.name == 'data.part' and path.parent.name == marker.read_text()+'.completing':
        actual_unlink(path, *args, **kwargs)
        upload_trace({'kind':'injected_post_unlink_fault','path':path.parent.name})
        raise OSError('Controlled fault after actual retired data.part removal')
    return actual_unlink(path, *args, **kwargs)
Path.unlink = interrupt_fs_cleanup

@app.get('/api/v1/cases/{case_id}/testing/upload-directory')
async def upload_directory(case_id:int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    return {'prefix':case.s3_prefix, 'fs':str(storage._fs_path(case.s3_prefix,'.vibeflow/uploads'))}
'''


def scenario(api, *, store=None, **_):
    target = api.case_id
    target_endpoint = f'/cases/{target}'
    workspace = api.call('GET', '/workspaces')[0]['id']
    sibling = write(api, 'POST', f'/workspaces/{workspace}/cases', {'name':'Parallel case write'})['id']
    foreign_registration = api.call('POST', '/auth/register', {
        'email':uuid4().hex+'@example.com', 'password':'isolated-maintenance-proof-123', 'name':'Other owner',
    })
    foreign_headers = {'Authorization':'Bearer '+foreign_registration['token']}
    foreign_workspace = api.call('GET', '/workspaces', extra_headers=foreign_headers)[0]['id']
    foreign = api.call('POST', f'/workspaces/{foreign_workspace}/cases', {'name':'Foreign upload case'},
                       extra_headers=foreign_headers)['id']
    primary = api.call('GET', target_endpoint+'/testing/upload-directory')
    external = api.call('GET', f'/cases/{foreign}/testing/upload-directory', extra_headers=foreign_headers)
    data = b'true upload maintenance bounded page check\n' * (32768 if store else 12)
    sha = hashlib.sha256(data).hexdigest()
    trace_path = api.root/'upload-maintenance-trace.jsonl'
    scan_path = '/case-provisions:maintenance'
    allow = api.root/'allow-upload-enumeration'

    def trace():
        if not trace_path.is_file():
            return []
        return [json.loads(line) for line in trace_path.read_text(encoding='utf8').splitlines()]

    def append(case, item, offset, content, *, headers=None):
        auth = headers or {'Authorization':'Bearer '+api.token}
        result = httpx.put(api.base+f'/api/v1/cases/{case}/uploads/{item}?offset={offset}',
                           headers=auth, content=content, trust_env=False, timeout=40)
        result.raise_for_status()
        assert result.json()['offset'] == offset + len(content)

    candidates = {}
    for index in range(8 if store else 7):
        path = f'Ref/actual-child-page-{index}.bin'
        upload = write(api, 'POST', target_endpoint+'/uploads', {'path':path, 'size':len(data), 'sha256':sha})
        candidates[upload['id']] = path
        append(target, upload['id'], 0, data[:19])
        append(target, upload['id'], 19, data[19:37])

    foreign_item = api.call('POST', f'/cases/{foreign}/uploads', {'path':'Ref/foreign.bin','size':len(data)},
                            extra_headers=foreign_headers)
    append(foreign, foreign_item['id'], 0, data[:23], headers=foreign_headers)
    settled_ids = sorted(candidates)[-(4 if store else 3):]
    recovered = set(settled_ids[:2])
    corrupted = settled_ids[-1]
    drifted = settled_ids[2] if store else None
    active_ids = set(candidates) - set(settled_ids)

    def keys(directory, identifier):
        if store:
            base = directory['prefix'] + f'/.vibeflow/uploads/{identifier}/'
            pages = store.raw_client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket, Prefix=base)
            return sorted((item['Key'], item['Size'], item['ETag']) for page in pages for item in page.get('Contents', []))
        base = Path(directory['fs'])
        folders = [base / identifier, base / (identifier+'.completing')]
        return sorted((str(path.relative_to(base)), path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
                      for folder in folders if folder.is_dir() for path in folder.iterdir() if path.is_file())

    for position, identifier in enumerate(settled_ids):
        append(target, identifier, 37, data[37:])
        fault = api.root/('interrupt-retired-cleanup' if not store and position == 0 else 'delay-completed-cleanup')
        fault.write_text(identifier)
        try:
            completed = api.call('POST', f'{target_endpoint}/uploads/{identifier}:complete')
            assert completed['cleanup_pending'] and completed['sha256'] == sha
        finally:
            fault.unlink()
        assert keys(primary, identifier), 'Actual staging needed after durable completion'
    if not store:
        retired = Path(primary['fs']) / (settled_ids[0]+'.completing')
        assert retired.is_dir() and {path.name for path in retired.iterdir()} == {'meta.json'}
        unknown = Path(primary['fs']) / corrupted / 'unknown-sidecar.bin'
        unknown.write_bytes(b'must-not-delete-unknown-stage')
    elif store:
        # Change the actual original generation after its cleanup plan was committed.
        # The production receipt comparison must preserve the different stage.
        store.raw_client.put_object(Bucket=store.bucket,
            Key=primary['prefix']+f'/.vibeflow/uploads/{corrupted}/meta.json', Body=b'retained unknown metadata')

    active_before = {identifier:keys(primary,identifier) for identifier in active_ids | {corrupted}}
    foreign_before = keys(external, foreign_item['id'])
    assert all(active_before.values()) and foreign_before
    if store:
        client = store.raw_client
        listing = {'Bucket':store.bucket, 'Prefix':primary['prefix']+'/.vibeflow/uploads/', 'Delimiter':'/', 'MaxKeys':2}
        first = client.list_objects_v2(**listing)
        second = client.list_objects_v2(**listing, ContinuationToken=first['NextContinuationToken'])
        a = {item['Prefix'] for item in first['CommonPrefixes']}
        b = {item['Prefix'] for item in second['CommonPrefixes']}
        assert len(a) == len(b) == 2 and a.isdisjoint(b) and first['IsTruncated']

    phase_start = len(trace())
    (api.root/'pause-upload-enumeration').write_text(primary['prefix'])
    with ThreadPoolExecutor(max_workers=3) as pool:
        request = pool.submit(api.call,'POST',scan_path)
        try:
            wait_for(lambda:(api.root/'upload-enumeration-paused').exists(), 'Production upload discovery did not pause', 25)
            first_write = pool.submit(write, api, 'PUT', target_endpoint+'/file',
                                      {'path':'Ref/same-case-during-enumeration.txt','content':'during page'})
            second_write = pool.submit(write, api, 'PUT', f'/cases/{sibling}/file',
                                       {'path':'Ref/same-owner-during-enumeration.txt','content':'parallel with page'})
            assert first_write.result(timeout=20)['ok'] and second_write.result(timeout=20)['ok']
            assert not request.done(), 'Upload page must still be paused when both actual writes are durable'
        finally:
            allow.touch()
        initial = request.result(timeout=45)

    initial_pages = [row for row in trace()[phase_start:] if row['kind']=='page' and row['prefix']==primary['prefix']]
    assert len(initial_pages) == 1 and len(initial_pages[0]['items']) <= 2 and initial_pages[0]['cursor']
    assert len([row for row in initial['uploads'] if row['case_id']==target]) <= 2

    # Observe the same account after the actual API has died, with normal
    # lifespan scheduling enabled and no client response advancing its cursor.
    old_pid = int((api.root/'upload-maintenance-pid').read_text())
    before_restart_count = len(trace())
    (api.root/'run-upload-maintenance').touch()
    (api.root/'pause-upload-part').write_text(settled_ids[1])
    if store:
        (api.root/'exit-after-conditional').write_text(settled_ids[0])
        (api.root/'pause-before-conditional').write_text(drifted)
    kill_owned_api(api,old_pid)
    restart(api)
    new_pid = int((api.root/'upload-maintenance-pid').read_text())
    assert new_pid != old_pid
    writes_during_verification = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            wait_for(lambda:(api.root/'upload-part-paused').exists(), 'Actual upload part verification did not pause', 45)
            same = pool.submit(write,api,'PUT',target_endpoint+'/file',
                               {'path':'Ref/same-case-during-part-check.txt','content':'part verification'})
            other = pool.submit(write,api,'PUT',f'/cases/{sibling}/file',
                                {'path':'Ref/same-owner-during-part-check.txt','content':'part verification'})
            assert same.result(timeout=9)['ok'] and other.result(timeout=9)['ok']
            writes_during_verification = 2
        finally:
            (api.root/'allow-upload-part').touch()

    if store:
        wait_for(lambda:api.proc.poll() is not None, 'API did not exit after a real conditional replacement', 45)
        assert api.proc.returncode == 79
        completed_steps = trace()[before_restart_count:]
        crashed = next(row for row in completed_steps if row['kind']=='exit_after_conditional')
        crashed_page = next(row for row in reversed(completed_steps)
                            if row['kind']=='page' and row['prefix']==primary['prefix'])
        key = primary['prefix'] + '/' + crashed['path']
        assert client.head_object(Bucket=store.bucket, Key=key)['ContentLength'] == 0
        assert any('/chunks/' in name and size > 0 for name, size, _ in keys(primary, crashed['id']))
        restart(api)
        recovered_pid = int((api.root/'upload-maintenance-pid').read_text())
        assert recovered_pid not in (old_pid, new_pid)
        try:
            paused = api.root/'upload-conditional-paused'
            wait_for(paused.is_file, 'Recovery never reached the conditionally replaced object', 60)
            path = paused.read_text()
            key = primary['prefix'] + '/' + path
            prior = client.head_object(Bucket=store.bucket, Key=key)
            replacement = b'Z' * prior['ContentLength']
            client.put_object(Bucket=store.bucket, Key=key, Body=replacement, IfMatch=prior['ETag'])
        finally:
            (api.root/'allow-conditional').touch()
        wait_for(lambda:any(row['kind']=='conditional_conflict' and row['id']==drifted and row['path']==path
                            for row in trace()), 'Changed object did not reject the old ETag', 35)
        current = client.get_object(Bucket=store.bucket, Key=key)['Body']
        try:
            assert current.read() == replacement
        finally:
            current.close()
        resumed = next(row for row in trace() if row['kind']=='page' and row['pid']==recovered_pid
                       and row['prefix']==primary['prefix'])
        assert resumed['after'] == crashed_page['after'] and resumed['items'] == crashed_page['items']

    def target_pages():
        return [row for row in trace()[before_restart_count:] if row['kind']=='page' and row['prefix']==primary['prefix']]

    def completed_recovery():
        observed = [row for row in trace()[phase_start:]
                    if row['kind']=='cleanup' and row['prefix']==primary['prefix']]
        return (
            recovered <= {row['id'] for row in observed if row['result']}
            and any(row['id']==corrupted and row['result'] is False for row in observed)
        )

    wait_for(lambda:len(target_pages())>=3 and completed_recovery(), 'Later completed uploads never reached normalized cleanup', 75)
    (api.root/'pause-scheduled-maintenance').touch()
    wait_for(lambda:(api.root/'scheduled-maintenance-idle').exists(), 'Scheduled worker did not drain', 45)
    after = target_pages()
    assert all(0 <= len(row['items']) <= 2 for row in after)
    assert after[0]['after'] == initial_pages[0]['cursor'], (initial_pages, after[0])
    assert set(candidates) <= {identifier for row in initial_pages+after for identifier in row['items']}
    assert all(keys(primary, identifier) == inventory for identifier, inventory in active_before.items())
    assert keys(external, foreign_item['id']) == foreign_before
    assert all(api.call('GET', f'{target_endpoint}/uploads/{identifier}')['offset']==37 for identifier in active_ids)
    for identifier in settled_ids:
        published = api.call('GET',target_endpoint+'/raw?'+urlencode({'path':candidates[identifier]}),raw=True)
        assert published == data
    if not store:
        assert all(not keys(primary,identifier) for identifier in recovered)
    else:
        for identifier in recovered:
            inventory = keys(primary, identifier)
            assert all(size == 0 for key,size,_ in inventory if '/chunks/' in key)
            assert all(size == 0 for key,size,_ in inventory if key.endswith('/meta.json'))
    assert api.call('GET', target_endpoint+'/raw?path=Ref/same-case-during-enumeration.txt',raw=True) == b'during page'
    assert api.call('GET', f'/cases/{sibling}/raw?path=Ref/same-owner-during-enumeration.txt',raw=True) == b'parallel with page'
    assert api.call('GET', target_endpoint+'/raw?path=Ref/same-case-during-part-check.txt',raw=True) == b'part verification'
    assert api.call('GET', f'/cases/{sibling}/raw?path=Ref/same-owner-during-part-check.txt',raw=True) == b'part verification'
    report = {
        'passed':True,'page_size':2,'real_uploads':len(candidates),'completed_recovered':sorted(recovered),
        'incomplete_byte_identity_count':len(active_ids),'corrupted_stage_retained':True,'foreign_stage_unchanged':True,
        'real_parallel_writes_before_page_release':2,'real_api_restart':True,'resumed_saved_cursor':True,
        'real_parallel_writes_during_part_verification':writes_during_verification,
        'real_exit_after_conditional_write_resumed':bool(store),'changed_object_cas_rejected':bool(store),
        's3_delimiter_pagination_observed':bool(store),'fs_retired_directory_recovery_observed':not store,
        'pages':initial_pages+after,'published_payload_sha256':sha,
    }
    (api.root/'upload-child-maintenance.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


if __name__ == '__main__':
    if sys.argv[1:] == ['--infrastructure-only']:
        from testsupport import IsolatedServer, SERVER

        source_path = SERVER / 'app' / 'services' / 'storage_maintenance.py'
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        runner = IsolatedServer('upload-maintenance-infrastructure', app='live_fixture:app')
        (runner.root/'live_fixture.py').write_text(
            'from pathlib import Path\nimport json\nroot=Path(__file__).parent\nfrom app.main import app\n' + FIXTURE,
            encoding='utf8',
        )
        print('UPLOAD_MAINTENANCE_INFRA ' + str(runner.root), flush=True)
        summary = {'passed':False,'production_sha256':source_hash,'real_model_requests':0,'mock_llm_calls':0}
        try:
            with runner:
                runner.create_case('Actual upload child paging')
                scenario(runner)
            assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash
            summary['passed'] = True
        finally:
            summary['api_exited'] = runner.proc is not None and runner.proc.poll() is not None
            (runner.root/'infrastructure-result.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
    else:
        from s3testsupport import ObjectStore, SERVER

        runtime = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
        asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE,
                         store_factory=lambda:ObjectStore(executable=runtime, volume_max=8, authenticated=True)))
