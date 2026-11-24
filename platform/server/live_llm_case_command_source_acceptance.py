"""Measure the real typed-command S3 source scan and verify a published job."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from functools import partial
import hashlib
import json
import subprocess
import sys
import time

import httpx

from live_llm_job_repeat_acceptance import main, write
from s3testsupport import ObjectStore, SERVER


FIXTURE = '''
import asyncio
from contextvars import ContextVar
import os
import time
from botocore.client import BaseClient
from botocore.response import StreamingBody
from fastapi import Depends
from app.db import get_db
from app.deps import get_current_user
from app.models import CaseEventCounter, StorageWriteReservation
from app.routers.workspaces import get_owned_case
from app.services import attempt_seal_journal, attempt_workspace, s3_case_files
from app.services.remote_write_budget import Client as CapacityClient
from app import storage
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

scan = ContextVar('typed_command_input_scan', default=None)
observed_function = 'observe_source' if hasattr(s3_case_files, 'observe_source') else 'snapshot'
actual_snapshot = getattr(s3_case_files, observed_function)
actual_read = StreamingBody.read
actual_open = Path.open

def observe_read(body, amt=None):
    result = actual_read(body, amt)
    proof = scan.get()
    if proof is not None and int(body._content_length or 0) == 24*1024*1024:
        proof['bytes_read'] += len(result)
        proof['max_body_read'] = max(proof['max_body_read'], len(result))
        if result and (root/'pause-command-source').exists() and not (root/'release-command-source').exists():
            from app.services.request_deadline import current
            deadline = current()
            if deadline is not None:
                (root/'command-source-time-left').write_text(str(deadline.remaining()))
            (root/'command-source-paused').touch()
            wait_until = time.monotonic()+30
            while not (root/'release-command-source').exists():
                if time.monotonic() >= wait_until:
                    raise TimeoutError('Command source read was not released')
                time.sleep(.02)
    if (proof is not None and result and int(body._content_length or 0) == 4096
            and (root/'pause-legacy-read').exists() and not (root/'release-legacy-read').exists()):
        (root/'legacy-read-paused').touch()
        wait_until = time.monotonic()+20
        while not (root/'release-legacy-read').exists():
            if time.monotonic() >= wait_until:
                raise TimeoutError('Legacy test read was not released')
            time.sleep(.02)
    return result

StreamingBody.read = observe_read

def observe_open(path, mode='r', *args, **kwargs):
    proof = scan.get()
    if (proof is not None and path != root/'command-source-time-left'
            and any(flag in mode for flag in ('w','x','a','+'))):
        proof['local_files_opened_for_write'] += 1
    return actual_open(path, mode, *args, **kwargs)

Path.open = observe_open

if hasattr(s3_case_files, 'local_snapshot'):
    actual_local_snapshot = s3_case_files.local_snapshot
    def observe_local(source):
        proof = scan.get()
        if proof is not None:
            proof['local_snapshot_bytes'] = sum(path.stat().st_size for path in source.rglob('*') if path.is_file())
        return actual_local_snapshot(source)
    s3_case_files.local_snapshot = observe_local

def observe_snapshot(prefix, *args, **kwargs):
    proof = {'bytes_read':0,'max_body_read':0,'local_snapshot_bytes':0,'local_files_opened_for_write':0}
    token = scan.set(proof)
    try:
        return actual_snapshot(prefix, *args, **kwargs)
    finally:
        scan.reset(token)
        with (root/'typed-command-source.jsonl').open('a', encoding='utf-8') as trace:
            trace.write(json.dumps(proof)+'\\n')

setattr(s3_case_files, observed_function, observe_snapshot)

def observe_seal(actual):
    async def observed(*args, **kwargs):
        result = await actual(*args, **kwargs)
        if (root/'pause-after-command-seal').exists() and not (root/'release-command-seal').exists():
            (root/'command-seal-paused').touch()
            until = time.monotonic()+30
            while not (root/'release-command-seal').exists():
                if time.monotonic() >= until:
                    raise TimeoutError('Test did not release the completed command seal')
                await asyncio.sleep(.025)
        return result
    return observed

attempt_workspace.seal = observe_seal(attempt_workspace.seal)
try:
    from app.services import command_input_seal
except ImportError:
    pass
else:
    command_input_seal.bind_prepared = observe_seal(command_input_seal.bind_prepared)

actual_s3_call = BaseClient._make_api_call
def is_large_attempt_input(key):
    return '/.vibeflow/attempts/' in key and key.endswith('/inputs/Ref/large-command-input.bin')

def crash_on_input(marker, key):
    if (root/marker).exists() and is_large_attempt_input(key):
        (root/marker).unlink()
        (root/(marker+'-key')).write_text(key, encoding='utf-8')
        os._exit(89)

def observe_s3_call(client, operation, arguments):
    result = actual_s3_call(client, operation, arguments)
    key = arguments.get('Key', '')
    if operation == 'PutObject':
        crash_on_input('exit-before-capacity-ack', key)
    if (operation == 'PutObject' and is_large_attempt_input(key)
            and (root/'pause-command-upload').exists() and not (root/'release-command-upload').exists()):
        from app.services.request_deadline import current
        deadline = current()
        if deadline is not None:
            (root/'command-upload-time-left').write_text(str(deadline.remaining()), encoding='utf-8')
        (root/'command-upload-paused').write_text(key, encoding='utf-8')
        until = time.monotonic()+35
        while not (root/'release-command-upload').exists():
            if time.monotonic() >= until:
                raise TimeoutError('Test did not release the accepted command input upload')
            time.sleep(.025)
    return result

BaseClient._make_api_call = observe_s3_call

actual_capacity_put = CapacityClient.put_object
def observe_capacity_put(client, **arguments):
    result = actual_capacity_put(client, **arguments)
    crash_on_input('exit-after-capacity-ack', arguments.get('Key', ''))
    return result

CapacityClient.put_object = observe_capacity_put

@app.post('/api/v1/testing/short-command-source-deadline')
async def short_command_source_deadline(user=Depends(get_current_user)):
    from app.services import request_deadline

    request_deadline.STAGING_CLEANUP_SECONDS = 15
    return {'seconds':15}

@app.post('/api/v1/cases/{case_id}/testing/edit-after-command-seal')
async def edit_after_seal(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    case = await get_owned_case(case_id,user,db)
    # External storage edits do not share the application's SQL case lock.
    await asyncio.to_thread(storage.put_bytes,case.s3_prefix,'ExternalEdit/added.txt',b'real external input')
    return {'ok':True}

@app.get('/api/v1/cases/{case_id}/testing/command-seal-plans')
async def command_seal_plans(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    case = await get_owned_case(case_id,user,db)
    entries = await asyncio.to_thread(storage.list_tree,case.s3_prefix,attempt_seal_journal.PREFIX+'/pending')
    return [item['path'] for item in entries]

@app.get('/api/v1/cases/{case_id}/testing/command-seal-credit')
async def command_seal_credit(case_id:int,key:str,user=Depends(get_current_user),db=Depends(get_db)):
    case = await get_owned_case(case_id,user,db)
    rows = await db.scalars(select(StorageWriteReservation).where(StorageWriteReservation.case_id==case.id))
    return [{'state':row.state,'remaining':row.remaining_bytes} for row in rows
            if json.loads(row.request_json).get('key')==key]

@app.get('/api/v1/testing/raw-case-source')
async def raw_case_source(user=Depends(get_current_user)):
    return s3_case_files.snapshot(f'workspaces/{user.id}/cases/987100')

@app.get('/api/v1/testing/native-case-source/{case_id}')
async def native_case_source(case_id:int, user=Depends(get_current_user), db=Depends(get_db)):
    from app.services.case_query_source import _remote_snapshot

    case = await get_owned_case(case_id, user, db)
    return _remote_snapshot(case.s3_prefix)

@app.get('/api/v1/cases/{case_id}/testing/command-sql-lock')
async def command_sql_lock(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    from app.db import SessionLocal

    await get_owned_case(case_id, user, db)
    async with SessionLocal() as probe:
        if probe.get_bind().dialect.name == 'postgresql':
            try:
                row = await probe.scalar(select(CaseEventCounter.case_id).where(
                    CaseEventCounter.case_id == case_id).with_for_update(nowait=True))
                assert row == case_id
            except OperationalError as error:
                if getattr(error.orig,'sqlstate',None) != '55P03':
                    raise
                return {'acquired':False}
        else:
            connection = await probe.connection()
            previous = (await connection.exec_driver_sql('PRAGMA busy_timeout')).scalar_one()
            try:
                await connection.exec_driver_sql('PRAGMA busy_timeout=200')
                await connection.exec_driver_sql('BEGIN IMMEDIATE')
            except OperationalError as error:
                if getattr(error.orig,'sqlite_errorcode',None) != 5:
                    raise
                return {'acquired':False}
            finally:
                await connection.exec_driver_sql('PRAGMA busy_timeout='+str(previous))
        await probe.rollback()
    return {'acquired':True}
'''


def kill_during_upload(api, store, client, endpoint, request, key, marker):
    from live_llm_outbox_restart_acceptance import restart

    (api.root/marker).touch()
    try:
        response = client.post(endpoint, json=request, headers=key)
        raise AssertionError(f'Isolated API returned {response.status_code} before forced exit')
    except (httpx.RemoteProtocolError, httpx.ReadError):
        assert api.proc.wait(timeout=15)==89
    object_key = (api.root/(marker+'-key')).read_text(encoding='utf-8')
    head = store.raw_client.head_object(Bucket=store.bucket, Key=object_key)
    assert head['ContentLength']==24*1024*1024
    restart(api)
    return object_key


def scenario(api, *, store=None, baseline=False, lock_phase=None, **_):
    payload = bytes(range(256)) * (24*1024*1024//256)
    target = 'Ref/large-command-input.bin'
    source_hash = hashlib.sha256(payload).hexdigest()
    if store:
        upload = api.call('POST', api.prefix+'/uploads',
                          {'path':target, 'size':len(payload), 'sha256':source_hash})
        endpoint = api.prefix+'/uploads/'+upload['id']
        api.put('Ref/empty-source.txt','')
        api.put('Ref/example.CMD','echo Executable mode check')
    else:
        target = 'Ref/small-command-input.txt'
        payload = b'actual filesystem typed command input'
        source_hash = hashlib.sha256(payload).hexdigest()
        api.put(target, payload.decode())
    command = subprocess.list2cmdline([
        sys.executable, '-c',
        "import hashlib; from pathlib import Path; Path('D-post/source-sha.txt').write_text("
        f"hashlib.sha256(Path('{target}').read_bytes()).hexdigest())",
    ])
    auth = {'Authorization':'Bearer '+api.token}
    api_path = api.base+'/api/v1'+api.prefix
    with httpx.Client(headers=auth, trust_env=False, timeout=150) as client:
        if store:
            chunk = 8*1024*1024
            for offset in range(0,len(payload),chunk):
                part = client.put(api.base+'/api/v1'+endpoint, params={'offset':offset},
                                  content=payload[offset:offset+chunk])
                part.raise_for_status()
                assert part.json()['offset']==offset+chunk
            completion = client.post(api.base+'/api/v1'+endpoint+':complete')
            completion.raise_for_status()
            assert completion.json()['sha256']==source_hash
        request = {'type':'convert','runtime':'windows','command':command}
        key = {'Idempotency-Key':'real-source-snapshot-'+str(api.case_id)}
        lock_receipt = None
        if lock_phase=='seal-restart':
            failed_object = kill_during_upload(api, store, client, api_path+'/jobs', request, key, 'exit-after-capacity-ack')
            failed_attempt = failed_object.split('/.vibeflow/attempts/')[1].split('/')[0]
            assert not api.call('GET',api.prefix+'/jobs')
            failed_plans = api.call('GET',api.prefix+'/testing/command-seal-plans')
            assert len(failed_plans)==1 and failed_plans[0].endswith(failed_attempt+'.json'), failed_plans
            credits = client.get(api_path+'/testing/command-seal-credit', params={'key':failed_object})
            assert credits.status_code==200 and credits.json()==[{'state':'settled','remaining':0}], credits.text
            response = client.post(api_path+'/jobs', json=request, headers=key)
            assert response.status_code==200, response.text
            old_prefix = failed_object.split('/inputs/')[0]+'/'
            old_members = store.raw_client.list_objects_v2(Bucket=store.bucket, Prefix=old_prefix).get('Contents', [])
            assert not old_members
            assert not set(failed_plans).intersection(api.call('GET',api.prefix+'/testing/command-seal-plans'))
            lock_receipt = {'mode':lock_phase,'exited_code':89,'no_job_after_restart_before_retry':True,
                            'rolled_back_attempt':failed_attempt,'old_members_removed':True}
        elif lock_phase in ('seal-baseline','seal','seal-deadline'):
            workspace = api.call('GET','/workspaces')[0]['id']
            sibling = write(api,'POST',f'/workspaces/{workspace}/cases',{'name':'Save while sealed input uploads'})['id']
            if lock_phase=='seal-deadline':
                assert api.call('POST','/testing/short-command-source-deadline')['seconds']==15
            (api.root/'pause-command-upload').touch()
            with ThreadPoolExecutor(max_workers=2) as pool:
                submission = pool.submit(client.post, api_path+'/jobs', json=request, headers=key)
                try:
                    stop = time.monotonic()+70
                    while not (api.root/'command-upload-paused').exists():
                        if submission.done():
                            raise AssertionError(submission.result().text)
                        if time.monotonic() >= stop:
                            raise TimeoutError('Real attempt input upload did not pause')
                        time.sleep(.025)
                    sql_free = api.call('GET',api.prefix+'/testing/command-sql-lock')['acquired']
                    save = pool.submit(write,api,'PUT',f'/cases/{sibling}/file',
                                       {'path':'Ref/concurrent.txt','content':'concurrent durable save'})
                    try:
                        concurrent_save = save.result(timeout=8)['ok']
                    except FutureTimeout:
                        concurrent_save = False
                    if lock_phase=='seal-deadline':
                        remaining = float((api.root/'command-upload-time-left').read_text())
                        assert 0<remaining<=15
                        time.sleep(remaining+.25)
                    assert not submission.done()
                finally:
                    (api.root/'release-command-upload').touch()
                response = submission.result(timeout=150)
                assert save.result(timeout=45)['ok']
            expected = lock_phase!='seal-baseline'
            assert (sql_free, concurrent_save)==(expected,expected), (sql_free,concurrent_save)
            lock_receipt = {'mode':lock_phase,'sql_available':sql_free,'saved_during_upload':concurrent_save}
            if lock_phase=='seal-deadline':
                assert response.status_code==504 and response.json()['detail']['code']=='command_seal_timeout', response.text
                assert not api.call('GET',api.prefix+'/jobs')
                plans = set(api.call('GET',api.prefix+'/testing/command-seal-plans'))
                assert len(plans)==1, plans
                response = client.post(api_path+'/jobs',json=request,headers=key)
                assert response.status_code==200, response.text
                assert plans.isdisjoint(api.call('GET',api.prefix+'/testing/command-seal-plans'))
                lock_receipt['expired_upload_drained_and_retried'] = True
        elif lock_phase:
            workspace = api.call('GET','/workspaces')[0]['id']
            sibling = write(api,'POST',f'/workspaces/{workspace}/cases',{'name':'Save while typed input is read'})['id']
            if lock_phase=='deadline':
                assert api.call('POST','/testing/short-command-source-deadline')['seconds']==15
            (api.root/'pause-command-source').touch()
            with ThreadPoolExecutor(max_workers=2) as pool:
                submission = pool.submit(client.post, api_path+'/jobs', json=request, headers=key)
                save = None
                try:
                    stop = time.monotonic()+55
                    while not (api.root/'command-source-paused').exists():
                        if time.monotonic() >= stop:
                            raise TimeoutError('Formal job intake did not pause on real input')
                        time.sleep(.025)
                    sql_free = api.call('GET',api.prefix+'/testing/command-sql-lock')['acquired']
                    if lock_phase=='deadline':
                        seconds = float((api.root/'command-source-time-left').read_text())
                        assert 0<seconds<=15
                        time.sleep(seconds+.25)
                        concurrent_save = None
                    else:
                        destination = f'/cases/{sibling}/file' if lock_phase=='baseline' else api.prefix+'/file'
                        save = pool.submit(write,api,'PUT',destination,{'path':'Ref/intake-edit.txt','content':'durable edit'})
                        try:
                            concurrent_save = save.result(timeout=7 if lock_phase=='baseline' else 15)['ok']
                        except FutureTimeout:
                            concurrent_save = False
                    assert not submission.done()
                finally:
                    (api.root/'release-command-source').touch()
                response = submission.result(timeout=150)
                if save is not None:
                    assert save.result(timeout=45)['ok']
            stale_refused = False
            stale_seal_recovered = False
            if lock_phase=='baseline':
                assert not sql_free and not concurrent_save
            elif lock_phase=='deadline':
                assert sql_free and response.status_code==504, response.text
                assert response.json()['detail']['code']=='command_source_timeout'
                assert not api.call('GET',api.prefix+'/jobs')
                assert not api.call('GET',api.prefix+'/testing/command-seal-plans')
                response = client.post(api_path+'/jobs',json=request,headers=key)
            else:
                assert sql_free and concurrent_save
                assert response.status_code==409 and response.json()['detail']['code']=='input_stale', response.text
                assert not api.call('GET',api.prefix+'/jobs')
                stale_refused = True
                (api.root/'pause-after-command-seal').touch()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    sealed = pool.submit(client.post,api_path+'/jobs',json=request,headers=key)
                    try:
                        stop = time.monotonic()+70
                        while not (api.root/'command-seal-paused').exists():
                            if time.monotonic() >= stop:
                                raise TimeoutError('Real command attempt seal did not complete')
                            time.sleep(.025)
                        assert api.call('POST',api.prefix+'/testing/edit-after-command-seal')['ok']
                    finally:
                        (api.root/'release-command-seal').touch()
                    late = sealed.result(timeout=150)
                assert late.status_code==409 and late.json()['detail']['code']=='input_stale', late.text
                assert not api.call('GET',api.prefix+'/jobs')
                plans = set(api.call('GET',api.prefix+'/testing/command-seal-plans'))
                assert len(plans)==1, plans
                response = client.post(api_path+'/jobs',json=request,headers=key)
                assert response.status_code==200, response.text
                assert plans.isdisjoint(api.call('GET',api.prefix+'/testing/command-seal-plans'))
                stale_seal_recovered = True
            lock_receipt = {'mode':lock_phase,'sql_available':sql_free,'saved_during_read':concurrent_save,
                            'stale_refused':stale_refused, 'stale_seal_recovered':stale_seal_recovered}
        else:
            response = client.post(api_path+'/jobs', json=request, headers=key)
        assert response.status_code==200, response.text[:1300]
        job_id = response.json()['id']
        stop = time.monotonic()+160
        while True:
            result = client.get(api.base+f'/api/v1/jobs/{job_id}')
            result.raise_for_status()
            job = result.json()
            if job['status'] not in ('queued','pending','running','finalizing','cancelling'):
                break
            if time.monotonic() >= stop:
                raise TimeoutError('Actual typed command did not settle')
            time.sleep(.3)
        assert job['status']=='succeeded', job.get('log_backlog','')[-1200:]
        assert job['attempt']['outputs']['publication']['status']=='committed'
        frozen = job['attempt']['workspace']['files'][target]
        assert frozen == {'size':len(payload),'sha256':source_hash,'executable':False}
        if store:
            empty = job['attempt']['workspace']['files']['Ref/empty-source.txt']
            assert empty == {'size':0,'sha256':hashlib.sha256(b'').hexdigest(),'executable':False}
            command_input = job['attempt']['workspace']['files']['Ref/example.CMD']
            assert command_input['executable'] == (sys.platform=='win32')
        published = client.get(api_path+'/raw', params={'path':'D-post/source-sha.txt'})
        assert published.status_code==200 and published.text==source_hash
        repeat = client.post(api_path+'/jobs', json=request, headers=key)
        assert repeat.status_code == 200 and repeat.json()['id']==job_id
        if lock_phase=='seal-restart':
            unknown_key = {'Idempotency-Key':key['Idempotency-Key']+'-unknown'}
            retained = kill_during_upload(api, store, client, api_path+'/jobs', request, unknown_key, 'exit-before-capacity-ack')
            retry = client.post(api_path+'/jobs', json=request, headers=unknown_key)
            assert retry.status_code == 409 and retry.json()['detail']['code']=='file_recovery_required', retry.text
            assert {job['id'] for job in api.call('GET',api.prefix+'/jobs')}=={job_id}
            head = store.raw_client.head_object(Bucket=store.bucket, Key=retained)
            assert head['ContentLength']==len(payload)
            credits = client.get(api_path+'/testing/command-seal-credit', params={'key':retained})
            assert credits.status_code==200 and credits.json()==[{'state':'active','remaining':len(payload)}], credits.text
            lock_receipt['unknown_put_retained_without_job'] = True

    results = []
    raw_fallback = None
    source_version_change_rejected = False
    native_source_matches = False
    if store:
        trace = api.root/'typed-command-source.jsonl'
        results = [json.loads(line) for line in trace.read_text().splitlines()]
        scans = [item for item in results if item['bytes_read']>=len(payload)]
        assert len(scans)>=2, results
        if baseline:
            assert all(item['max_body_read']==len(payload) and item['local_snapshot_bytes']>=len(payload)
                       and item['local_files_opened_for_write']>0
                       for item in scans)
        else:
            assert all(0<item['max_body_read']<=1024*1024 and item['local_snapshot_bytes']==0
                       and item['local_files_opened_for_write']==0 for item in scans)
            native = api.call('GET',f'/testing/native-case-source/{api.case_id}')
            native_source_matches = (native['files'][target] == frozen
                                     and native['files']['Ref/empty-source.txt']==empty
                                     and native['files']['Ref/example.CMD']=={**command_input,'executable':False})
            assert native_source_matches
            root = 'workspaces/1/cases/987100/'
            for name, data in [('test/nested/data.bin',payload[:4096]),('test/empty.txt',b'')]:
                store.raw_client.put_object(Bucket=store.bucket,Key=root+name,Body=data,IfNoneMatch='*')
            raw_fallback = api.call('GET','/testing/raw-case-source')
            assert raw_fallback == {'files':{
                'test/empty.txt':{'size':0,'sha256':hashlib.sha256(b'').hexdigest(),'executable':False},
                'test/nested/data.bin':{'size':4096,'sha256':hashlib.sha256(payload[:4096]).hexdigest(),'executable':False},
            },'directories':['test','test/nested']}
            source = root+'test/nested/data.bin'
            version = store.raw_client.head_object(Bucket=store.bucket, Key=source)['ETag']
            changed = b'C'*4096
            (api.root/'pause-legacy-read').touch()
            with ThreadPoolExecutor(max_workers=1) as pool:
                reading = pool.submit(api.call,'GET','/testing/raw-case-source',expect=409)
                try:
                    stop = time.monotonic()+15
                    while not (api.root/'legacy-read-paused').exists():
                        if time.monotonic() >= stop:
                            raise TimeoutError('S3 legacy read did not pause')
                        time.sleep(.05)
                    store.raw_client.put_object(Bucket=store.bucket, Key=source, Body=changed, IfMatch=version)
                finally:
                    (api.root/'release-legacy-read').touch()
                assert reading.result(timeout=25)['detail']['code']=='input_stale'
            current = api.call('GET','/testing/raw-case-source')
            assert current['files']['test/nested/data.bin']['sha256']==hashlib.sha256(changed).hexdigest()
            source_version_change_rejected = True
    report = {'passed':True,'baseline':baseline,'real_job_id':job_id,'published':True,
              'source_bytes':len(payload),'source_sha256':source_hash,'snapshots':results,
              'raw_legacy_verified':raw_fallback is not None, 'native_source_matches':native_source_matches,
              'source_version_change_rejected':source_version_change_rejected, 'idempotent_job':True,
              'intake_lock':lock_receipt}
    (api.root/'typed-command-source-result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    baseline = '--source-baseline' in sys.argv
    if baseline:
        sys.argv.remove('--source-baseline')
    lock_phase = None
    for option in ('--source-lock-baseline','--source-lock','--source-deadline'):
        if option in sys.argv:
            sys.argv.remove(option)
            lock_phase = 'baseline' if option.endswith('baseline') else 'deadline' if option.endswith('deadline') else 'fixed'
    for option, phase in (('--seal-lock-baseline','seal-baseline'), ('--seal-lock','seal'),
                          ('--seal-restart','seal-restart'), ('--seal-deadline','seal-deadline')):
        if option in sys.argv:
            sys.argv.remove(option)
            lock_phase = phase
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline,lock_phase=lock_phase), fixture_override=FIXTURE,
                     store_factory=lambda:ObjectStore(executable=binary, volume_max=8, authenticated=True)))
