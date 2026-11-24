"""Actual Redis acknowledgement waits for startup handoff and PDF finalization."""
import asyncio,hashlib,io,json,shlex,time
from pathlib import Path
import asyncpg
from redis.asyncio import Redis
from arq.constants import in_progress_key_prefix,job_key_prefix
from pypdf import PdfReader,PdfWriter
from app.services.owned_process import wsl_path
from live_llm_remote_documents_acceptance import upload
import live_llm_remote_worker_acceptance as baseline

OBSERVE='''
from app.services import job_launch_registration,job_execution
from app.services.distributed_worker import PlatformWorker
boundary_root=Path(BOUNDARY_ROOT)
async def boundary_wait(stage,owner):
 mode=boundary_root/'mode'
 if not mode.exists() or mode.read_text()!=stage:return
 mode.unlink()
 target=boundary_root/'boundary.json';staged=target.with_suffix('.staged')
 staged.write_text(json.dumps({'stage':stage,'job_id':owner.job_id,'attempt_id':owner.attempt_id}))
 staged.replace(target)
 while not (boundary_root/'release').exists():await asyncio.sleep(.02)
original_prepared=job_launch_registration.prepared
async def observed_prepared(owner,process):
 await original_prepared(owner,process)
 await boundary_wait('startup',owner)
job_launch_registration.prepared=observed_prepared
original_finish=job_execution._finish_owned
async def observed_finish(owner,*args,**kwargs):
 await boundary_wait('finalizing',owner)
 return await original_finish(owner,*args,**kwargs)
job_execution._finish_owned=observed_finish
original_close=PlatformWorker._close_drained
async def observed_close(self,grace_seconds):
 (boundary_root/'close-started').touch()
 return await original_close(self,grace_seconds)
PlatformWorker._close_drained=observed_close
'''


class ObservedWorker(baseline.LinuxWorker):
    def fixture_code(self,root):return 'BOUNDARY_ROOT='+repr(wsl_path(root))+'\n'+OBSERVE


async def scenario(api,worker,selected):
    result={'passed':False,'mock_llm_calls':0,'jobs':[],'boundaries':[]}
    def save():(api.root/'shutdown-boundaries.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
    async def call(method,path,**kwargs):
        deadline=time.monotonic()+45
        while True:
            try:return await asyncio.to_thread(api.call,method,path,**kwargs)
            except AssertionError as error:
                if not any(c in str(error) for c in ('storage_quota_busy','validation_busy')) or time.monotonic()>deadline:raise
                await asyncio.sleep(.2)
    async def file(path):
        deadline=time.monotonic()+120
        while not path.exists():
            assert worker.process.poll() is None,'Worker exited before observed boundary'
            assert time.monotonic()<deadline,'Actual worker boundary was not reached'
            await asyncio.sleep(.05)
    async def wait(identifier,status):
        deadline=time.monotonic()+120
        while True:
            job=await call('GET','/jobs/'+str(identifier))
            if job['status']==status and job['attempt'].get('process_stop',{}).get('verified'):
                if status!='succeeded' or job['attempt']['outputs'].get('completion',{}).get('state')=='settled':return job
            assert time.monotonic()<deadline,'Actual boundary job did not finish: '+job['status']
            await asyncio.sleep(.2)
    connection=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,
        password=selected.password,database=selected.database,timeout=10)
    redis=Redis(host='127.0.0.1',port=worker.broker.port,socket_timeout=5)
    async def snapshot(identifier):
        row=await connection.fetchrow('''SELECT j.status,a.id,a.phase,a.input_manifest_json,
          a.process_identity_json,a.output_manifest_json,a.write_checkpoint_json
          FROM jobs j JOIN job_attempts a ON a.job_id=j.id WHERE j.id=$1''',identifier)
        return dict(row)
    closing=None
    try:
        source=PdfWriter();source.add_blank_page(width=300,height=400)
        data=io.BytesIO();source.write(data)
        await asyncio.to_thread(upload,api,'Ref/shutdown-source.pdf',data.getvalue(),'application/pdf')
        for stage in ('startup','finalizing'):
            root=worker.root;(root/'mode').write_text(stage,'utf8')
            if stage=='startup':
                created=await call('POST',api.prefix+'/jobs',body={'type':'shell','runtime':'windows','timeout_seconds':180,
                    'command':'python3 -c '+shlex.quote('import time;time.sleep(120)')})
                identifier=created['id']
            else:
                created=await call('POST',api.prefix+'/pdf:number',body={'input':'Ref/shutdown-source.pdf','output':'E-report/build/shutdown-numbered.pdf'})
                identifier=created['job_id']
            await file(root/'boundary.json');observed=json.loads((root/'boundary.json').read_text('utf8'))
            before=await snapshot(identifier);assert observed['job_id']==identifier and observed['attempt_id']==before['id']
            assert before['phase']==('starting' if stage=='startup' else 'finalizing')
            if stage=='startup':assert before['process_identity_json']=='{}' and before['write_checkpoint_json']=='{}'
            else:
                assert json.loads(before['write_checkpoint_json'])['stop']['verified']
                assert 'finalization_intent' in json.loads(before['output_manifest_json'])
            delivery='vf-delivery-'+before['id'];key=in_progress_key_prefix+delivery
            assert await redis.exists(key)==1
            closing=asyncio.create_task(worker.stop())
            await file(root/'close-started');await asyncio.sleep(.3)
            during=await snapshot(identifier)
            assert not closing.done() and worker.process.poll() is None
            assert await redis.exists(key)==1,'Redis delivery acknowledged before boundary drained'
            assert during['phase']==before['phase'] and during['write_checkpoint_json']==before['write_checkpoint_json']
            (root/'release').touch();await closing;closing=None
            job=await wait(identifier,'interrupted' if stage=='startup' else 'succeeded')
            after=await snapshot(identifier)
            assert before['input_manifest_json']==after['input_manifest_json']
            assert await connection.fetchval('SELECT count(*) FROM job_attempts WHERE job_id=$1',identifier)==1
            assert await redis.exists(key)==0
            result['jobs'].append(job)
            result['boundaries'].append({'stage':stage,'job_id':identifier,'before':before,'during':during,'after':after,
                'delivery_in_progress_while_stalled':True,'worker_waited_for_boundary':True,'exit':worker.receipts[-1]})
            save();print('SHUTDOWN_BOUNDARY_PASS '+stage,flush=True)
            await asyncio.to_thread(worker.start)
        pdf=await call('GET',api.prefix+'/raw?path=E-report/build/shutdown-numbered.pdf',raw=True)
        pages=PdfReader(io.BytesIO(pdf)).pages;assert len(pages)==1 and '1 / 1' in pages[0].extract_text()
        (api.root/'shutdown-numbered.pdf').write_bytes(pdf)
        result['artifact']={'file':'shutdown-numbered.pdf','sha256':hashlib.sha256(pdf).hexdigest()}
        code="from pathlib import Path;import hashlib;data=Path('E-report/build/shutdown-numbered.pdf').read_bytes();Path('Ref/proof.txt').write_bytes(('RETIREMENT_'+hashlib.sha256(data).hexdigest()+'\\n').encode())"
        job=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'windows','command':'python3 -c '+shlex.quote(code)})
        result['jobs'].append(await wait(job['id'],'succeeded'))
        result['passed']=True;save()
    except BaseException as error:result['failure_type']=type(error).__name__;save();raise
    finally:
        (worker.root/'release').touch()
        try:
            if closing is not None:await closing
        finally:await connection.close();await redis.aclose();save()


async def verify(configuration,selected,admin,observed):
    return await baseline.verify(configuration,selected,admin,observed,worker_factory=ObservedWorker,after_publish=scenario)
if __name__=='__main__':asyncio.run(baseline.main(entry='platform/server/live_llm_remote_shutdown_boundaries_acceptance.py',verifier=verify))
