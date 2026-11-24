"""Actual 40 MiB Windows stdout and independent authenticated SeaweedFS logs."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from app.services.process_identity_native import probe
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


BLOCK = b'B'*65535+b'\n'
SIZE = 640*len(BLOCK)
SOURCE = ('import sys\nblock=b"B"*65535+b"\\n"\n'
          'for _ in range(640):\n    sys.stdout.buffer.write(block)\n'
          'sys.stdout.buffer.flush()\n')

FIXTURE = r'''
import asyncio
import json
import os
from pathlib import Path
import time
import tracemalloc

from botocore.exceptions import ClientError
from fastapi import Depends
from app.db import SessionLocal
from app.deps import get_current_user
from app.main import app
from app.models import Job, JobLogReservation
from app.routers.workspaces import get_owned_case
from app.services import attempt_objects, job_log_storage as logs, jobrunner
from app.services import process_identity_native, s3_live_store

root = Path(__file__).parent
trace = root/'late-log-trace.jsonl'

def record(name, **fields):
    with trace.open('a', encoding='utf8') as stream:
        stream.write(json.dumps({'name':name, **fields})+'\n')

def canonical(key):
    return '/.vibeflow/job-logs/job-' in key and key.endswith('.log')

put_object = s3_live_store.Client.put_object
get_object = s3_live_store.Client.get_object
upload = logs.upload

def put(self, **kwargs):
    key = kwargs['Key']
    if not canonical(key):
        return put_object(self, **kwargs)
    node = root/'files'/'.node-storage-budget'
    claims = [json.loads(path.read_text(encoding='utf8')) for path in node.glob('*.json')]
    body = kwargs['Body']
    details = {'key':key, 'condition':kwargs.get('IfNoneMatch'),
               'seekable':hasattr(body, 'seek') and body.seekable(),
               'claims':[item['bytes'] for item in claims]}
    try:
        result = put_object(self, **kwargs)
    except ClientError as error:
        record('put', status=error.response['ResponseMetadata']['HTTPStatusCode'], **details)
        raise
    record('put', status=result['ResponseMetadata']['HTTPStatusCode'], **details)
    signal = root/'put-acknowledged'
    if (root/'hold-response').exists() and not signal.exists():
        signal.touch()
        end = time.monotonic()+35
        while not (root/'lose-response').exists():
            if time.monotonic()>=end:
                raise TimeoutError('Test driver did not release the observed PUT')
            time.sleep(.03)
        raise TimeoutError('Test fixture discarded a successful actual PUT response')
    return result

class ObservedBody:
    def __init__(self, body, key):
        self.body = body
        self.key = key
        self.largest = self.length = 0

    def read(self, size=None):
        block = self.body.read(size)
        self.largest = max(self.largest, size or len(block))
        self.length += len(block)
        return block

    def close(self):
        self.body.close()
        record('get', key=self.key, largest_read=self.largest, bytes=self.length)

def get(self, **kwargs):
    result = get_object(self, **kwargs)
    if canonical(kwargs['Key']) and 'Range' not in kwargs:
        result['Body'] = ObservedBody(result['Body'], kwargs['Key'])
    return result

async def measure(label, operation, *args):
    tracemalloc.start()
    try:
        return await operation(*args)
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        record(label, peak_bytes=peak)

async def measured_upload(owner, prefix, path):
    marker = root/'job-log-sources.jsonl'
    with marker.open('a', encoding='utf8') as stream:
        stream.write(json.dumps({'job_id':owner.job_id, 'path':str(path)})+'\n')
    return await measure('upload', upload, owner, prefix, path)

s3_live_store.Client.put_object = put
s3_live_store.Client.get_object = get
logs.upload = measured_upload

@app.get('/fixture/identity')
async def identity(user=Depends(get_current_user)):
    return process_identity_native.observe(os.getpid())

async def load_ticket(job_id, user):
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        case = await get_owned_case(job.case_id, user, db, write=False)
        row = await db.get(JobLogReservation, job_id)
        ticket = logs.Ticket(**{field:getattr(row,field) for field in
            ('job_id','attempt_id','case_id','user_id','binding_json','sha256','size','token')})
        return case.s3_prefix, ticket

@app.post('/fixture/replay/{job_id}')
async def replay(job_id:int, user=Depends(get_current_user)):
    prefix, ticket = await load_ticket(job_id, user)
    sources = [json.loads(line) for line in (root/'job-log-sources.jsonl').read_text().splitlines()]
    source = Path(next(item['path'] for item in sources if item['job_id']==job_id))
    def retry():
        expected = {'size':ticket.size, 'sha256':ticket.sha256}
        with attempt_objects._frozen_member_body(source, logs.log_key(job_id), expected, kind='log') as body:
            logs.put_new(prefix, ticket, body)
    await measure('replay', asyncio.to_thread, retry)
    return {'verified':True}

@app.post('/fixture/reconcile/{job_id}')
async def reconcile(job_id:int, user=Depends(get_current_user)):
    await load_ticket(job_id, user)
    return {'result':await measure('reconcile', logs.reconcile, job_id)}

@app.post('/fixture/legacy/{case_id}')
async def legacy(case_id:int, user=Depends(get_current_user)):
    import sys
    from app import storage
    async with SessionLocal() as db:
        case = await get_owned_case(case_id, user, db)
        job = Job(case_id=case.id, type='shell', command='legacy staging fixture')
        db.add(job)
        await db.flush()
        old = f'.vibeflow/jobs/job-{job.id}.log'
        job.log_s3_key = old
        await db.commit()
    await asyncio.to_thread(storage._s3_put,case.s3_prefix,old,b'previous historical log\n')
    argv = [sys.executable, '-I', '-c', 'import sys; from pathlib import Path; sys.stdout.buffer.write(b"legacy stdout\\n"); Path("output.txt").write_text("legacy output")']
    proc = await jobrunner.start_job(job.id, job.command, case.s3_prefix, argv=argv)
    return {'job_id':job.id, 'staging':bool(proc.staging_prefix), 'isolated':proc.isolated_workspace}
'''


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()]


def wait_for(fetch, check, timeout=120):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        value = fetch()
        if check(value):
            return value
        time.sleep(.08)
    raise TimeoutError(f'Late log evidence did not reach its boundary: {value}')


def state(api, job_id):
    uri = (api.root/'test.sqlite').resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT j.status,j.log_s3_key,j.exit_code,c.s3_prefix,a.id AS attempt_id,'
                         'a.phase,a.output_manifest_json,l.state,l.size,l.sha256,l.remaining_bytes '
                         'FROM jobs j JOIN cases c ON c.id=j.case_id JOIN job_attempts a ON a.job_id=j.id '
                         'LEFT JOIN job_log_reservations l ON l.job_id=j.id WHERE j.id=?',(job_id,)).fetchone()
        value = dict(row)
        value['output_manifest_json'] = json.loads(value['output_manifest_json'])
        return value


def submit(api, *, empty=False):
    api.put('Ref/native_stdout.py','pass\n' if empty else SOURCE)
    command = subprocess.list2cmdline([sys.executable,'-I','-B','Ref/native_stdout.py'])
    return api.call('POST',api.prefix+'/jobs',{'type':'shell','runtime':'windows','command':command,
        'input_paths':['Ref/native_stdout.py'],'max_log_bytes':48*1024*1024,'timeout_seconds':240})['id']


def arguments(store, row):
    return {'Bucket':store.bucket,'Key':row['s3_prefix'].rstrip('/')+'/'+row['log_s3_key']}


def verified_bytes(store, args):
    response = store.raw_client.get_object(**args)
    digest = hashlib.sha256()
    size = 0
    with closing(response['Body']) as stream:
        for block in iter(lambda:stream.read(65536),b''):
            digest.update(block)
            size += len(block)
    assert size==response['ContentLength']
    return {'size':size,'sha256':digest.hexdigest(),'etag':response['ETag'],
            'version':response.get('VersionId')}


def verify_row(row, expected, *, state_name):
    assert {field:row[field] for field in expected}==expected, row
    assert row['state']==state_name, row
    assert row['remaining_bytes']==(expected['size'] if state_name=='active' else 0), row
    assert row['output_manifest_json']['process_stop']['verified'], row['phase']


def compatibility(api, store, report):
    api.create_case('Empty native stdout compatibility')
    empty_id = submit(api,empty=True)
    api.wait_job(empty_id,timeout=85)
    empty = state(api,empty_id)
    assert empty['state']=='materialized' and empty['size']==empty['remaining_bytes']==0
    assert verified_bytes(store,arguments(store,empty))['sha256']==hashlib.sha256(b'').hexdigest()
    empty_log = api.call('GET',f'/jobs/{empty_id}/log?limit=8')
    assert empty_log['chunk']=='' and empty_log['offset']==0, empty_log

    api.create_case('Legacy staging log compatibility')
    legacy = api.call('POST',f'/fixture/legacy/{api.case_id}',api_prefix=False)
    assert legacy['staging'] and not legacy['isolated'], legacy
    legacy_id = legacy['job_id']
    api.wait_job(legacy_id,timeout=90)
    old_path = f'.vibeflow/jobs/job-{legacy_id}.log'
    old = b'previous historical log\n'
    assert api.call('GET',api.prefix+'/raw?path='+old_path,raw=True)==old
    assert api.call('GET',api.prefix+'/raw?path=output.txt',raw=True)==b'legacy output'
    # The old staging runner appends stdout after the historical prefix.
    expected_legacy = old+b'legacy stdout\n'
    public = api.call('GET',f'/jobs/{legacy_id}/log')
    assert public['chunk'].encode()==expected_legacy and public['offset']==len(expected_legacy), public
    staged = state(api,legacy_id)
    selected = verified_bytes(store,arguments(store,staged))
    assert selected['sha256']==hashlib.sha256(expected_legacy).hexdigest()
    assert staged['state']=='materialized' and staged['size']==len(expected_legacy)
    report['compatibility'] = {'empty':empty_id,'legacy_staging':legacy_id,'legacy_object_preserved':True,
                               'legacy_log':selected}


def main():
    digest = hashlib.sha256()
    for _ in range(640):
        digest.update(BLOCK)
    expected = {'size':SIZE,'sha256':digest.hexdigest()}
    api = IsolatedServer('job-log-streaming',app='log_streaming_fixture:app',access_log=True)
    compat_only = os.environ.get('VIBEFLOW_JOB_LOG_COMPAT_ONLY')=='1'
    report = {'passed':False,'api':str(api.root),'expected':expected,'model_calls':0,'mock_llm_calls':0,
              'compat_only':compat_only}
    store = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                        volume_max=24,authenticated=True)
    report['object_store'] = str(store.root)
    try:
        with store:
            store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
            scratch = api.root/'native-temp'
            scratch.mkdir()
            env = {**store.settings(),'TEMP':str(scratch),'TMP':str(scratch),'TMPDIR':str(scratch)}
            (api.root/'log_streaming_fixture.py').write_text('import os\nos.environ.update('+repr(env)+')\n'+FIXTURE,
                                                             encoding='utf8')
            with api:
                if compat_only:
                    compatibility(api,store,report)
                else:
                    api.create_case('Dedicated remote log streaming')
                    job_id = submit(api)
                    job = api.wait_job(job_id,timeout=120)
                    assert job['exit_code']==0
                    row = state(api,job_id)
                    verify_row(row,expected,state_name='materialized')
                    assert row['status']==row['phase']=='succeeded'
                    args = arguments(store,row)
                    original = verified_bytes(store,args)
                    assert {field:original[field] for field in expected}==expected
                    assert api.call('POST',f'/fixture/replay/{job_id}',api_prefix=False)=={'verified':True}
                    assert verified_bytes(store,args)==original
                    version_list = store.raw_client.list_object_versions(Bucket=store.bucket,Prefix=args['Key'])
                    assert len([v for v in version_list.get('Versions',[]) if v['Key']==args['Key']])==1
                    public = api.call('GET',f'/jobs/{job_id}/log?offset={SIZE-8}&limit=8')
                    assert public['offset']==SIZE and public['chunk']=='B'*7+'\n', public
                    report['normal'] = {'job_id':job_id,'attempt_id':row['attempt_id'],'original':original,
                                        'version_count_after_replay':1,'public_tail':public}

                    (api.root/'hold-response').touch()
                    fault_id = submit(api)
                    wait_for((api.root/'put-acknowledged').exists,bool)
                    pending = state(api,fault_id)
                    assert pending['state']=='active' and pending['remaining_bytes']==SIZE, pending
                    fault_args = arguments(store,pending)
                    assert verified_bytes(store,fault_args)['sha256']==expected['sha256']
                    sources = lines(api.root/'job-log-sources.jsonl')
                    source = Path(next(item['path'] for item in sources if item['job_id']==fault_id))
                    original_copy = scratch/'original-stdout.bin'
                    corrupted = scratch/'bad-stdout.bin'
                    with original_copy.open('xb') as good, corrupted.open('xb') as bad, source.open('rb') as log:
                        first = log.read(len(BLOCK))
                        assert first==BLOCK
                        good.write(first)
                        bad.write(b'C'+first[1:])
                        for block in iter(lambda:log.read(len(BLOCK)),b''):
                            good.write(block)
                            bad.write(block)
                    with corrupted.open('rb') as stream:
                        store.raw_client.put_object(**fault_args,Body=stream)
                    (api.root/'lose-response').touch()
                    api.wait_job(fault_id,timeout=120,expected='failed')
                    pending = state(api,fault_id)
                    verify_row(pending,expected,state_name='active')
                    wrong = verified_bytes(store,fault_args)
                    assert wrong['size']==SIZE and wrong['sha256']!=expected['sha256']
                    withheld = api.call('POST',f'/fixture/reconcile/{fault_id}',api_prefix=False)
                    assert withheld=={'result':'log_unverified'} and state(api,fault_id)==pending
                    with original_copy.open('rb') as stream:
                        store.raw_client.put_object(**fault_args,Body=stream)
                    restored = api.call('POST',f'/fixture/reconcile/{fault_id}',api_prefix=False)
                    assert restored=={'result':None}
                    settled = state(api,fault_id)
                    verify_row(settled,expected,state_name='materialized')
                    assert settled['status']==settled['phase']=='failed' and settled['attempt_id']==pending['attempt_id']
                    report['lost_response'] = {'job_id':fault_id,'attempt_id':settled['attempt_id'],
                        'before_reconcile_bytes':pending['remaining_bytes'],'wrong_bytes':wrong,
                        'rejected':withheld,'after_reconcile_bytes':settled['remaining_bytes'],'job_status':settled['status']}

                    trace = lines(api.root/'late-log-trace.jsonl')
                    puts = [item for item in trace if item['name']=='put' and item['key']==args['Key']]
                    assert [item['status'] for item in puts]==[200,412], puts
                    assert all(item['seekable'] and item['condition']=='*' and SIZE in item['claims'] for item in puts), puts
                    reads = [item for item in trace if item['name']=='get' and item['bytes']==SIZE]
                    assert len(reads)>=4 and all(item['largest_read']<=len(BLOCK) for item in reads), reads
                    report.update(puts=puts,reads=reads)
                    compatibility(api,store,report)
                trace = lines(api.root/'late-log-trace.jsonl')
                measurements = [item for item in trace if 'peak_bytes' in item]
                assert all(item['peak_bytes']<12*1024*1024 for item in measurements), measurements
                assert not list((api.root/'files'/'.node-storage-budget').glob('*.json'))
                report.update(measurements=measurements,node_claims_released=True)
                birth = api.call('GET','/fixture/identity',api_prefix=False)
            report['api_exit'] = wait_for(lambda:probe(birth),lambda selected:selected['state']=='exited',12)
        assert store.proc.poll() is not None
        report['object_store_launcher_exited'] = True
        report['passed'] = True
    except BaseException as error:
        report.update(error=type(error).__name__,detail=str(error)[:4000]);raise
    finally:
        path = api.root/'job-log-streaming.json'
        path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
        print(f'JOB_LOG_STREAMING_RESULT {path}',flush=True)


if __name__=='__main__':
    main()
