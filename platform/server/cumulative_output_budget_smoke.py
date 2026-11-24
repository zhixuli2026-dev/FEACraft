"""Real Pandoc calls share one output budget across worker and finalizer scopes."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from testsupport import IsolatedServer,SERVER
from app.services import process_identity_native as identity

CONTROLLED='''import asyncio,json,sys
from pathlib import Path
from app.services import native_execution,job_delivery
from app.services.report_handler import ReportHandler
root=Path(__file__).parent
counts=[]
async def paused(stop):await stop.wait()
job_delivery.recover_forever=paused
prepare=ReportHandler.prepare
async def preparing(self,*args,budget,**kwargs):
 limit=32768 if (root/'mode').read_text() in {'normal','diagnostic_normal'} else 4096
 return await prepare(self,*args,budget={**budget,'max_log_bytes':limit},**kwargs)
ReportHandler.prepare=preparing
execute=native_execution.NativeExecutor._execute
async def executing(self,entry,purpose,parameters,argv,timeout,limit,output):
 if not hasattr(self.owner,'job_id'):return await execute(self,entry,purpose,parameters,argv,timeout,limit,output)
 mode=(root/'mode').read_text()
 if self.kind=='report' and (mode=='normal' or mode==entry.caller+'_limit'):
  argv=[str(Path(getattr(sys,'_base_executable',sys.executable)).resolve()),'-I','-S',str(root/'padding.py'),json.dumps(argv)]
 if self.kind=='report' and (mode=='diagnostic_normal' or mode==entry.caller+'_diagnostic' or mode in {'diagnostic_live','diagnostic_partial'}):
  argv=[argv[0],'-I','-S',str(root/'diagnostic_helper.py'),json.dumps(argv),mode]
 size=0
 async def observed(data):
  nonlocal size
  size+=len(data)
  await output(data)
 try:return await execute(self,entry,purpose,parameters,argv,timeout,limit,observed)
 finally:
  diagnostic=Path(parameters['config']).with_name('stderr.bin') if 'config' in parameters else None
  counts.append({'job_id':self.owner.job_id,'caller':entry.caller,'purpose':purpose,'bytes':size,
   'diagnostic_bytes':diagnostic.stat().st_size if diagnostic is not None and diagnostic.exists() else 0})
  (root/'native-output-counts.json').write_text(json.dumps(counts))
native_execution.NativeExecutor._execute=executing
from app.main import app
from fastapi import Depends
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Job,JobAttempt,DurableEvent,User
from app.routers.auth import get_current_user
from app.routers.workspaces import get_owned_case
@app.get('/api/v1/fixture/usage/{job_id}')
async def usage(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  return [json.loads(e.payload_json) for e in await db.scalars(select(DurableEvent).where(
   DurableEvent.attempt_id==attempt.id,DurableEvent.type=='attempt.resource_usage'))]
'''
PAD='''import json,subprocess,sys
# Leading JSON whitespace preserves the real helper receipt and document bytes.
sys.stdout.write(' '*1024);sys.stdout.flush()
raise SystemExit(subprocess.run(json.loads(sys.argv[1])).returncode)
'''
DIAGNOSTIC_HELPER='''import json,runpy,sys
from pathlib import Path
argv=json.loads(sys.argv[1]);mode=sys.argv[2]
module=runpy.run_path(argv[4]);execute=module['execute'];original=module['subprocess'].Popen
def noisy(command,*args,**kwargs):
 return original([sys.executable,'-I','-S',str(Path(__file__).with_name('diagnostic_child.py')),json.dumps(command),mode],*args,**kwargs)
module['subprocess'].Popen=noisy
execute(Path(argv[6]),argv[8])
'''
DIAGNOSTIC_CHILD='''import json,subprocess,sys,time
from pathlib import Path
sys.stderr.buffer.write(b'd'*(4097 if sys.argv[2]=='diagnostic_partial' else 8192 if sys.argv[2]=='diagnostic_live' else 2048));sys.stderr.buffer.flush()
if sys.argv[2] in {'diagnostic_live','diagnostic_partial'}:
 Path(__file__).with_name('diagnostic-live-started').touch()
 time.sleep(20)
 Path(__file__).with_name('diagnostic-live-finished').touch()
raise SystemExit(subprocess.run(json.loads(sys.argv[1])).returncode)
'''


def verify(modes,url=None):
    api=IsolatedServer('cumulative-output',app='controlled_output:app',database_url=url)
    (api.root/'controlled_output.py').write_text(CONTROLLED,encoding='utf8')
    (api.root/'padding.py').write_text(PAD,encoding='utf8')
    (api.root/'diagnostic_helper.py').write_text(DIAGNOSTIC_HELPER,encoding='utf8')
    (api.root/'diagnostic_child.py').write_text(DIAGNOSTIC_CHILD,encoding='utf8')
    checks=[];observations=[]
    with api:
        for mode in modes:
            (api.root/'mode').write_text(mode)
            api.create_case('Cumulative output '+mode)
            if mode.startswith('root_'):
                count=1024 if mode=='root_exact' else 1025
                payload=api.root/'payload.py';payload.write_text('import sys\nsys.stdout.buffer.write(b"z"*'+str(count)+')\n')
                row=api.call('POST',api.prefix+'/jobs',{'command':subprocess.list2cmdline([sys.executable,str(payload)]),'max_log_bytes':1024})
                job_id=row['id'];limit=1024
            else:
                api.put('E-report/report.md','# Real cumulative output report\n\nBudget verification.\n')
                row=api.call('POST',api.prefix+'/report:build',{});job_id=row['job_id']
                limit=32768 if mode in {'normal','diagnostic_normal'} else 4096
            deadline=time.monotonic()+120
            while True:
                final=api.call('GET',f'/jobs/{job_id}')
                if final['status'] not in {'queued','running'} and not final['completion_pending']:break
                if time.monotonic()>deadline:raise TimeoutError('Output budget Job did not settle')
                time.sleep(.1)
            path=api.root/'native-output-counts.json'
            counts=[c for c in json.loads(path.read_text()) if c['job_id']==job_id] if path.exists() else []
            usages=api.call('GET',f'/fixture/usage/{job_id}');assert len(usages)==1,usages
            usage=usages[0]
            assert usage['schema']=='vibeflow.execution-output-usage.v2' and usage['max_log_bytes']==limit
            assert usage['observed_output_bytes']==sum(usage['by_source'].values())
            assert usage['observed_output_bytes']==usage['observed_pipe_bytes']+usage['observed_diagnostic_bytes']
            expected='succeeded' if mode in {'normal','diagnostic_normal','root_exact'} else 'failed'
            observations.append({'mode':mode,'counts':counts,'usage':usage,'job':final})
            (api.root/'output-observations.json').write_text(json.dumps(observations,indent=2),encoding='utf8')
            assert final['status']==expected,final['status']
            if expected=='failed':
                assert final['attempt']['reason_code']=='resource_limit' and usage['violation']=='log_limit'
                assert usage['observed_output_bytes']>limit
            else:assert usage['violation'] is None and usage['observed_output_bytes']<=limit
            assert final['attempt']['process_stop']['verified']
            assert api.call('GET',f'/jobs/{job_id}/process')['state']=='exited'
            if mode.startswith('root_'):assert usage['observed_pipe_bytes']==count
            else:
                assert max(c['bytes'] for c in counts)<4096
                assert usage['observed_diagnostic_bytes']==sum(c['diagnostic_bytes'] for c in counts)
                if 'diagnostic' in mode:assert usage['observed_diagnostic_bytes']>0
                if mode in {'diagnostic_live','diagnostic_partial'}:
                    assert (api.root/'diagnostic-live-started').exists()
                    assert not (api.root/'diagnostic-live-finished').exists()
                if mode.endswith('_diagnostic'):
                    assert usage['violation_source']=='diagnostic.'+mode.split('_')[0]
                    assert max(c['diagnostic_bytes'] for c in counts)<=2048
                children=final['attempt']['child_processes']['children']
                for child in children.values():
                    assert child['stop']['verified'] and child['phase'] in {'stopped','failed'}
                    assert identity.probe(child['identity']['host'])['state']=='exited'
                if expected=='failed':assert final['attempt']['outputs']['publication']['status']=='failed'
                else:
                    publication=api.call('GET',api.prefix+'/report/results')['publication']
                    assert publication['id']==row['publication_id']
                if mode=='finalizer_limit':
                    assert any(c['caller']=='finalizer' for c in counts)
                    assert usage['by_source']['native.worker']<limit and usage['by_source']['native.finalizer']>0
            checks.append(mode+'_shares_owned_pipe_allowance_and_persists_exact_usage')
            print('PASS '+checks[-1],flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (api.root/'output-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--modes',default='normal,worker_limit,finalizer_limit,root_exact,root_over,diagnostic_normal,worker_diagnostic,finalizer_diagnostic,diagnostic_live,diagnostic_partial');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='cumulative-output-evidence-',dir=SERVER.parents[1]/'tmp'))
    modes=args.modes.split(',');results={'sqlite':await asyncio.to_thread(verify,modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_outputbudget_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_outputbudget_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,modes,target)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('CUMULATIVE_OUTPUT_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items()}}))


if __name__=='__main__':asyncio.run(main())
