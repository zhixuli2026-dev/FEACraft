"""Original and resumed finalizing log writes from a real Windows native command."""

from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import httpx

from app.services.process_identity_native import probe
from s3testsupport import ObjectStore
from testsupport import SERVER, IsolatedServer


LOG = '.vibeflow/finalizing/root.log'
SIZE = 40 * 1024 * 1024
BLOCK = b'B' * 65535 + b'\n'
SOURCE = ('import sys\nblock=b"B"*65535+b"\\n"\n'
          'for _ in range(640):\n    sys.stdout.buffer.write(block)\n'
          'sys.stdout.buffer.flush()\n')

FIXTURE = r'''
import json
import os
from pathlib import Path
import threading
import tracemalloc

from fastapi import Depends
from app.deps import get_current_user
from app.main import app
from app.services import finalizing_stage as stages, finalizing_stage_recovery as recovery
from app.services import job_finalizing_source as source, process_identity_native

root = Path(__file__).parent

def event(name, **fields):
    with (root/'capture-trace.jsonl').open('a', encoding='utf8') as stream:
        stream.write(json.dumps({'name':name, **fields})+'\n')

async def measured(name, original, *args, **kwargs):
    tracemalloc.start()
    try:
        return await original(*args, **kwargs)
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        event(name, peak_bytes=peak)

original_capture = source.capture
original_recovery = recovery._capture_owned

async def capture(*args, **kwargs):
    return await measured('original_capture', original_capture, *args, **kwargs)

async def recover(*args, **kwargs):
    return await measured('recovered_capture', original_recovery, *args, **kwargs)

source.capture = capture
recovery._capture_owned = recover

original_append = stages.append_log

def append(stream, data, offset):
    if (root/'pause-after-prefix').exists() and not (root/'partial-log.json').exists():
        with data.path.open('rb') as real_log:
            block = real_log.read(stages.LOG_BLOCK_BYTES)
        assert offset == 0 and len(block) == stages.LOG_BLOCK_BYTES
        stream.write(block)
        stream.flush()
        os.fsync(stream.fileno())
        marker = root/'partial-log.pending'
        marker.write_text(json.dumps({'path':str(stream.name),'size':len(block),
                                      'record':data.record}), encoding='utf8')
        marker.replace(root/'partial-log.json')
        if not threading.Event().wait(120):
            raise TimeoutError('Fixture original owner was not terminated')
    return original_append(stream, data, offset)

stages.append_log = append

@app.get('/fixture/identity')
async def api_birth(user=Depends(get_current_user)):
    return process_identity_native.observe(os.getpid())

@app.post('/fixture/exit')
async def api_exit(user=Depends(get_current_user)):
    os._exit(79)
'''


def poll(fetch, predicate, seconds=90):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        value = fetch()
        if predicate(value):
            return value
        time.sleep(.04)
    raise TimeoutError(f'Finalizing log fixture did not reach its boundary: {value}')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(64*1024),b''):
            result.update(block)
    return {'size':path.stat().st_size,'sha256':result.hexdigest()}


def snapshot(api, job_id):
    uri = (api.root/'test.sqlite').resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        row = db.execute('SELECT a.id,a.phase,a.output_manifest_json,c.s3_prefix FROM job_attempts a '
                         'JOIN jobs j ON a.job_id=j.id JOIN cases c ON j.case_id=c.id WHERE j.id=?', (job_id,)).fetchone()
        events = db.execute('SELECT type,payload_json FROM durable_events WHERE attempt_id=? ORDER BY seq', (row[0],)).fetchall()
    return row[0], row[1], json.loads(row[2]), row[3], events


def submit(api):
    api.create_case('Actual large stdout finalization')
    api.put('Ref/native_stdout.py',SOURCE)
    command = subprocess.list2cmdline([sys.executable,'-I','-B','Ref/native_stdout.py'])
    return api.call('POST', api.prefix+'/jobs', {'type':'shell','runtime':'windows','command':command,
        'input_paths':['Ref/native_stdout.py'],'max_log_bytes':48*1024*1024,'timeout_seconds':240})['id']


def verify_log(api, job_id, expected, capture_name, store):
    attempt_id, phase, output, prefix, events = snapshot(api, job_id)
    assert phase=='succeeded' and output['process_stop']['verified'], (phase,output.keys())
    record = {key:output['files'][LOG][key] for key in expected}
    assert record==expected, record
    if store is not None or capture_name=='recovered_capture':
        assert output['finalizing_source']['log']==expected
    assert sum(kind=='attempt.launch_prepared' for kind,_ in events)==1
    plans = [json.loads(payload) for kind,payload in events if kind=='attempt.finalizing_file_planned']
    assert len([plan for plan in plans if plan['relative']==LOG and plan['record']==expected])==1
    path = api.root/'files'/prefix/output['run_path']/LOG
    assert digest(path)==expected
    if store is not None:
        from hashlib import sha256
        remote = store.client.get_object(Bucket=store.bucket,Key=prefix.rstrip('/')+'/'+output['run_path']+'/'+LOG)
        key_hash, size = sha256(), 0
        with closing(remote['Body']) as stream:
            for block in iter(lambda:stream.read(64*1024),b''):
                key_hash.update(block)
                size+=len(block)
        assert {'size':size,'sha256':key_hash.hexdigest()}==expected
    captures = [json.loads(line) for line in (api.root/'capture-trace.jsonl').read_text(encoding='utf8').splitlines()]
    capture = next(item for item in reversed(captures) if item['name']==capture_name)
    assert capture['peak_bytes']<12*1024*1024, capture
    return {'attempt_id':attempt_id,'log':record,'peak_bytes':capture['peak_bytes'],
            'actual_stops_verified':True,'native_launches':1,'actual_archive_verified':store is not None}


def run_case(expected, *, interrupted, store, reports):
    api = IsolatedServer('early-finalizing-log',app='finalizing_log_fixture:app')
    fixture = api.root/'finalizing_log_fixture.py'
    scratch = api.root/'native-temp'
    scratch.mkdir()
    environment = {'TEMP':str(scratch),'TMP':str(scratch),'TMPDIR':str(scratch)}
    if store is not None:
        environment.update(store.settings())
    fixture.write_text('import os\nos.environ.update('+repr(environment)+')\n'+FIXTURE,encoding='utf8')
    report = {'evidence':str(api.root),'interrupted':interrupted,'api_exits':[]}
    reports.append(report)
    with api:
        if interrupted:
            (api.root/'pause-after-prefix').touch()
        job_id = submit(api)
        birth = api.call('GET','/fixture/identity',api_prefix=False)
        if interrupted:
            user = api.call('GET','/auth/me')
            marker = api.root/'partial-log.json'
            poll(marker.exists,bool)
            partial = json.loads(marker.read_text(encoding='utf8'))
            target = Path(partial['path'])
            assert partial['record']==expected and target.read_bytes()==BLOCK
            original = api.call('GET',f'/jobs/{job_id}/preparation-recovery')
            assert original['state']=='preparation_pending', original
            body = {'attempt_id':original['attempt_id'],'stage_sha256':original['stage_sha256']}
            route = f'/jobs/{job_id}:recover-preparation'
            busy = api.call('POST',route,body,expect=409)
            assert busy['detail']['code']=='recovery_required', busy
            try:
                httpx.post(api.base+'/fixture/exit',headers={'Authorization':'Bearer '+api.token},timeout=15,trust_env=False)
            except httpx.TransportError:
                pass
            else:
                raise AssertionError('Actual owner exit unexpectedly returned a response')
            poll(lambda:probe(birth),lambda selected:selected['state']=='exited',12)
            report['api_exits'].append('original_owner_exit_after_partial')
            api.__exit__(RuntimeError,None,None)
            (api.root/'pause-after-prefix').unlink()
            api.__enter__()
            api.token = api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
            birth = api.call('GET','/fixture/identity',api_prefix=False)

            corrupted = b'C'+BLOCK[1:]
            target.write_bytes(corrupted)
            rejected = api.call('POST',route,body,expect=409)
            assert rejected['detail']['code']=='finalizing_file_conflict', rejected
            assert target.read_bytes()==corrupted
            target.write_bytes(BLOCK)
            recovered = api.call('POST',route,body)
            assert recovered['attempt_id']==original['attempt_id'] and recovered['state']=='captured', recovered
            assert not recovered['native_execution_started'] and not target.exists()
            assert api.call('POST',route,body)==recovered
            finalize = {'attempt_id':recovered['attempt_id'],'inventory_sha256':recovered['inventory_sha256']}
            repair = 'recover-outputs' if store is not None else 'restore-output-cache'
            api.call('POST',f'/jobs/{job_id}:{repair}',finalize)
            committed = api.call('POST',f'/jobs/{job_id}:finalize-outputs',finalize)
            assert committed['state']=='succeeded' and not committed['native_execution_started'], committed
            report['verified'] = verify_log(api,job_id,expected,'recovered_capture',store)
            report.update(physical_prefix_bytes=partial['size'],retained_bad_prefix=True,original_attempt_reused=True)
        else:
            job = api.wait_job(job_id,timeout=115)
            assert job['exit_code']==0, job
            report['verified'] = verify_log(api,job_id,expected,'original_capture',store)
    poll(lambda:probe(birth),lambda selected:selected['state']=='exited',12)
    report['api_exits'].append('terminal_api_exit')


def main():
    checksum = hashlib.sha256()
    for _ in range(SIZE//len(BLOCK)):
        checksum.update(BLOCK)
    expected = {'size':SIZE,'sha256':checksum.hexdigest()}
    reports=[]
    remote = os.environ.get('VIBEFLOW_FINALIZING_LOG_TEST_S3')=='1'
    result = {'passed':False,'backend':'s3' if remote else 'fs','expected':expected,
              'cases':reports,'model_calls':0,'mock_llm_calls':0}
    try:
        for interrupted in (False, True):
            store = (ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                                 volume_max=16, authenticated=True) if remote else None)
            with store if store is not None else nullcontext():
                run_case(expected,interrupted=interrupted,store=store,reports=reports)
            if store is not None:
                assert store.proc.poll() is not None
                reports[-1]['object_store'] = {'evidence':str(store.root),'isolated_launcher_exited':True}
        result['passed']=True
    except BaseException as error:
        result.update(error=type(error).__name__,detail=str(error)[:4000]);raise
    finally:
        if reports:
            path = Path(reports[0]['evidence'])/'early-finalizing-log-streaming.json'
            path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
            print(f'EARLY_FINALIZING_LOG_STREAMING_RESULT {path}',flush=True)


if __name__=='__main__':
    main()
