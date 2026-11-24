"""Seal and retry a real native Job with a 40 MiB member on actual SeaweedFS."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shlex
import time
from urllib.parse import quote

from case_read_view_streaming_acceptance import ASSET, RUNTIME, SIZE, install, source_file, stop_api
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


SCRIPT = 'Ref/sealed-input-proof.sh'
PROOF = 'Ref/sealed-input-proof.txt'
ENTRY = b'''#!/bin/sh
set -eu
test -x Ref/sealed-input-proof.sh
sha256sum Ref/large-view-input.bin > Ref/sealed-input-proof.txt
cat Ref/sealed-input-proof.txt
[ -f "$1" ] || exit 17
'''

FIXTURE = r'''
import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path
import threading
import tracemalloc

from botocore.exceptions import ClientError
from fastapi import Depends
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.models import JobAttempt
from app.routers.workspaces import get_owned_case
from app.services import attempt_objects, node_storage_budget, process_identity_native, s3_live_store

root = Path(__file__).parent
phase = ContextVar('input_seal_phase', default=None)
receipt_lock = threading.Lock()
materialize = attempt_objects.Snapshot.materialize
put_members = attempt_objects.put_input_members
get_object = s3_live_store.Client.get_object
put_object = s3_live_store.Client.put_object

def receipt(item):
    with receipt_lock, (root/'input-events.jsonl').open('a',encoding='utf-8') as log:
        log.write(json.dumps(item)+'\n')

def measured(name, call, *args):
    token = phase.set(name)
    tracemalloc.start()
    try:
        return call(*args)
    finally:
        receipt({'kind':'memory','phase':name,'peak':tracemalloc.get_traced_memory()[1]})
        tracemalloc.stop()
        phase.reset(token)

def measured_source(self):
    return measured('source',materialize,self)

def measured_upload(*args):
    return measured('upload',put_members,*args)

class MeasuredBody:
    def __init__(self, body, entry):
        self.body, self.entry = body,entry

    def read(self, count=-1):
        self.entry['max_requested'] = max(count,self.entry['max_requested'])
        self.entry['unbounded_request'] |= count<=0
        data = self.body.read(count)
        self.entry['received'] += len(data)
        return data

    def close(self):
        try:
            self.body.close()
        finally:
            receipt({**self.entry,'closed':True})

def witnessed_get(self, **args):
    response = get_object(self, **args)
    name = phase.get()
    if name and args['Key'].endswith('/large-view-input.bin'):
        response['Body'] = MeasuredBody(response['Body'], {'kind':'get','phase':name,'key':args['Key'],
            'if_match':args.get('IfMatch'),'status':response['ResponseMetadata']['HTTPStatusCode'],
            'max_requested':0,'unbounded_request':False,'received':0})
    return response

def witnessed_put(self, **args):
    name = phase.get()
    if not name or not args['Key'].endswith('/large-view-input.bin'):
        return put_object(self, **args)
    body = args['Body']
    entry = {'kind':'put','phase':name,'key':args['Key'],'condition':args.get('IfNoneMatch'),
             'seekable_stream':not isinstance(body,bytes) and body.seekable()}
    try:
        result = put_object(self, **args)
    except ClientError as error:
        receipt({**entry,'status':error.response['ResponseMetadata']['HTTPStatusCode']})
        raise
    receipt({**entry,'status':result['ResponseMetadata']['HTTPStatusCode']})
    return result

attempt_objects.Snapshot.materialize = measured_source
attempt_objects.put_input_members = measured_upload
s3_live_store.Client.get_object = witnessed_get
s3_live_store.Client.put_object = witnessed_put

@app.get('/fixture/input/{case_id}/binding')
async def binding(case_id: int,user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(case_id,user,db,write=False)
    return {'prefix':case.s3_prefix,'process':process_identity_native.observe(os.getpid())}

@app.post('/fixture/input/{case_id}/same-upload')
async def same_upload(case_id: int, body: dict, user=Depends(get_current_user), db=Depends(get_db)):
    case=await get_owned_case(case_id,user,db,write=False)
    prefix=case.s3_prefix
    attempt=await db.get(JobAttempt,body['attempt_id'])
    manifest=json.loads(attempt.input_manifest_json)['workspace']
    assert manifest['storage']['prefix']==prefix and manifest['attempt_id']==body['attempt_id']
    root=storage._fs_path(prefix,manifest['inputs_path'])
    await db.rollback()
    await asyncio.to_thread(attempt_objects.put_input_members,prefix,manifest['inputs_path'],manifest['files'],root)
    return {'ok':True,'reserved_bytes':(await asyncio.to_thread(node_storage_budget.observe))['reserved_bytes']}

@app.post('/fixture/view:stop')
async def stop(body: dict,user=Depends(get_current_user)):
    assert body['pid']==os.getpid()
    os._exit(79)
'''


def events(root):
    return [json.loads(line) for line in (root/'input-events.jsonl').read_text(encoding='utf-8').splitlines()]


def settled(api, job_id, expected):
    job = api.wait_job(job_id,timeout=150,expected=expected)
    deadline = time.monotonic()+35
    while job.get('completion_pending'):
        if time.monotonic()>=deadline:
            raise TimeoutError('Actual Job did not complete output settlement')
        time.sleep(.1)
        job = api.call('GET',f'/jobs/{job_id}')
    assert job['attempt']['phase']==expected, job
    return job


def actual_output(api, first, sha256):
    path = first['attempt']['workspace']['run_path']+'/'+PROOF
    data = api.call('GET',api.prefix+'/raw?path='+quote(path,safe=''),raw=True)
    assert data.decode().split()[0]==sha256, data
    assert sha256 in first['log_backlog']


def exercise(api,store,report):
    identifier = api.create_case('Native long S3 seal')['id']
    route = f'/fixture/input/{identifier}'
    binding = api.call('GET',route+'/binding',api_prefix=False)
    prefix = binding['prefix']
    report['api_process'] = binding['process']
    payload,sha256 = source_file(api.root)
    try:
        install(api,store,prefix,ASSET,payload)
        install(api,store,prefix,SCRIPT,ENTRY)
        from app.services import tree_metadata_native as tree
        portable={'schema':tree.SCHEMA,'materialize_directories':True,'entries':{
            'Ref':{'kind':'directory','mode':0o755},
            SCRIPT:{'kind':'file','mode':0o755,'size':len(ENTRY),'sha256':hashlib.sha256(ENTRY).hexdigest()}}}
        install(api,store,prefix,tree.PATH,json.dumps(portable).encode())
        permit = api.root/'allow-actual-retry'
        linux_permit = '/mnt/'+permit.drive[0].lower()+permit.as_posix()[2:]
        job = api.call('POST',api.prefix+'/jobs',{'type':'shell','runtime':'wsl',
            'input_paths':[ASSET,SCRIPT],'command':'bash '+SCRIPT+' '+shlex.quote(linux_permit),
            'timeout_seconds':45},extra_headers={'Idempotency-Key':'native-long-input'})
        first = settled(api,job['id'],'failed')
        assert first['exit_code']==17, first['log_backlog']
        actual_output(api,first,sha256)
        manifest = first['attempt']['workspace']
        assert manifest['storage']['backend']=='s3' and manifest['source']['job_id']==job['id']
        assert manifest['files'][ASSET]['size']==SIZE and manifest['files'][ASSET]['sha256']==sha256
        args={'Bucket':store.bucket,'Key':prefix+'/'+manifest['inputs_path']+'/'+ASSET}
        mode_args={**args,'Key':prefix+'/'+manifest['inputs_path']+'/'+tree.PATH}
        with closing(store.client.get_object(**mode_args)['Body']) as body:
            archived_mode = tree.decode(body.read(tree.MAX_BYTES+1))['entries'][SCRIPT]
        assert archived_mode==portable['entries'][SCRIPT]
        expected_etag = store.client.head_object(**args)['ETag']
        same = api.call('POST',route+'/same-upload',{'attempt_id':manifest['attempt_id']},api_prefix=False)
        assert same['ok'] and same['reserved_bytes']==0
        assert store.client.head_object(**args)['ETag']==expected_etag
        traced = events(api.root)
        explicit = [entry for entry in traced if entry['kind']=='put']
        assert [entry['status'] for entry in explicit]==[200,412],explicit
        assert all(entry['condition']=='*' and entry['seekable_stream'] for entry in explicit)
        reads = [entry for entry in traced if entry['kind']=='get']
        assert [entry['phase'] for entry in reads]==['source','upload','upload'],reads
        assert all(entry['received']==SIZE and entry['closed'] and not entry['unbounded_request']
                   and entry['max_requested']<=1024*1024 for entry in reads), reads
        assert reads[0]['if_match'] and reads[0]['max_requested']<=64*1024
        peaks={phase:[entry['peak'] for entry in traced if entry['kind']=='memory' and entry['phase']==phase]
               for phase in ('source','upload')}
        assert len(peaks['source'])==1 and len(peaks['upload'])==2 and max(sum(peaks.values(),[]))<SIZE//4, peaks
        report.update({'initial_job':job['id'],'initial_exit_code':17,'archived_script_mode':archived_mode['mode'],
            'first_seal_and_same_upload':traced,'initial_peaks':peaks,'member_sha256':sha256})

        command = f'/jobs/{job["id"]}'
        options = api.call('GET',command+'/operations')
        assert options['retry']['available'], options
        request = {'action':'retry','source_attempt_id':options['source_attempt_id'],
                   'source_version':options['source_version']}
        retry_headers={'Idempotency-Key':'native-original-archive-retry'}
        store.client.delete_object(**args,IfMatch=expected_etag)
        absent = api.call('POST',command+':repeat',request,expect=409,extra_headers=retry_headers)
        assert absent['detail']['code'] in {'input_stale','recovery_required'},absent
        with payload.open('r+b') as scratch:
            original = scratch.read(1)
            scratch.seek(0)
            scratch.write(bytes([original[0]^0xff]))
        try:
            with payload.open('rb') as body:
                store.client.put_object(**args,Body=body,IfNoneMatch='*')
            tampered = store.client.head_object(**args)['ETag']
            stale = api.call('POST',command+':repeat',request,expect=409,extra_headers=retry_headers)
            assert stale['detail']['code'] in {'input_stale','recovery_required'},stale
            assert store.client.head_object(**args)['ETag']==tampered
            assert [row['id'] for row in api.call('GET',api.prefix+'/jobs')]==[job['id']]
        finally:
            with payload.open('r+b') as scratch:
                scratch.write(original)
        with payload.open('rb') as body:
            store.client.put_object(**args,Body=body,IfMatch=tampered)
        permit.touch()
        retry = api.call('POST',command+':repeat',request,expect=202,extra_headers=retry_headers)
        assert retry['created'] and retry['id']!=job['id']
        done = settled(api,retry['id'],'succeeded')
        actual_output(api,done,sha256)
        assert done['attempt']['workspace']['files']==manifest['files']
        again = api.call('POST',command+':repeat',request,expect=202,extra_headers=retry_headers)
        assert again['id']==retry['id'] and not again['created']
        assert not list((api.root/'files/.attempt-sources').glob('owned-*'))
        report.update({'absent_refusal':absent['detail']['code'],'stale_refusal':stale['detail']['code'],
                       'retry_job':retry['id'],'retry_idempotent':True,'retry_output_sha256':sha256})
    finally:
        payload.unlink(missing_ok=True)


def run():
    from app.services import process_identity_native
    api = IsolatedServer('attempt-input-streaming',app='attempt_input_fixture:app')
    report={'passed':False,'real_model_requests':0,'mock_llm_calls':0}
    print('ATTEMPT_INPUT_STREAMING '+str(api.root),flush=True)
    try:
        with ObjectStore(executable=RUNTIME,volume_max=16,authenticated=True) as store:
            module=api.root/'attempt_input_fixture.py'
            try:
                module.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE,encoding='utf-8')
                with api:
                    try:
                        exercise(api,store,report)
                    finally:
                        if 'api_process' in report:
                            stop_api(api,report['api_process'])
            finally:
                module.unlink(missing_ok=True)
        assert store.proc.poll() is not None
        report.update({'s3_stopped':True,'s3_receipt':str(store.root/'store.json')})
        report['api_exit']=process_identity_native.probe(report['api_process'])
        assert report['api_exit']['state']=='exited'
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1400]}
        raise
    finally:
        paths=[Path(__file__).resolve(),*(SERVER/'app/services'/name for name in
            ('attempt_objects.py','attempt_workspace.py','private_cache.py','tree_metadata_native.py'))]
        report['source_sha256']={path.relative_to(SERVER).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in paths}
        (api.root/'attempt-input-streaming.json').write_text(
            json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print('ATTEMPT_INPUT_STREAMING_OK '+str(api.root),flush=True)


if __name__=='__main__':
    run()
