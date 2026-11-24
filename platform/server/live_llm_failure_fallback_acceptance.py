"""Actual CadQuery failures and owned API termination; real DeepSeek readback.

Boundary holds and missing-drain mutations are protocol fault fixtures. Native
processes, SQL/file transactions and model replies always execute for real.
"""
import asyncio,json,os,time,uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from casedict_fallback_capacity_acceptance import read_settled
from live_llm_job_repeat_acceptance import main,put,write
from testsupport import IsolatedServer

FIXTURE=r'''
import asyncio,hashlib,json,os,time,uuid
from pathlib import Path
from fastapi import Depends,HTTPException
from sqlalchemy import select
from app.db import SessionLocal,get_db
from app.deps import get_current_user
from app.models import Case,DurableEvent,Job,JobAttempt
from app.routers.workspaces import get_owned_case
from app.services import workflow_fallback as fallback,file_transactions,job_delivery,s3_storage_recovery
from app.services.case_write_capacity import committing
from app.services.casedict import CASE_DICT_PATH,get_workflow_state
from app import storage
fallback_root=Path(os.environ.get('VIBEFLOW_FALLBACK_SHARED_ROOT',str(Path(__file__).parent)))
fallback_role=os.environ.get('VIBEFLOW_FALLBACK_ROLE','main')
fallback_original_settle=fallback.settle_failure_fallback
fallback_original_applied=fallback._record_applied
fallback_original_recover=fallback.recover_once
fallback_original_finish=file_transactions.finish_committed
fallback_original_recover_storage=s3_storage_recovery.recover_reserved
fallback_original_pending=fallback.pending
fallback_original_delivery=job_delivery.deliver
fallback_faults={}

def fallback_trace(kind,case_id,**details):
 with (fallback_root/'fallback-boundaries.jsonl').open('a') as stream:
  stream.write(json.dumps({'kind':kind,'case_id':case_id,'role':fallback_role,'pid':os.getpid(),**details})+'\n')

def fallback_selection():
 path=fallback_root/'fallback-hold.json'
 return json.loads(path.read_text()) if path.exists() else None

async def fallback_hold(db,case,job,cut):
 selected=fallback_selection()
 if not selected or selected['case_id']!=case.id or selected['cut']!=cut or fallback_role!='crash':return
 async with SessionLocal() as observed:
  persisted=await observed.get(Case,case.id)
  original=await observed.get(Job,job.id)
  required=await observed.get(DurableEvent,fallback.event_id(job,fallback.REQUIRED))
  applied=await observed.get(DurableEvent,fallback.event_id(job,fallback.APPLIED))
  evidence={'job_id':job.id,'pid':os.getpid(),'cut':cut,'sql_stage':persisted.workflow_state,
   'job_status':original.status,'required_committed':required is not None,'applied_committed':applied is not None,
   'actual_file_stage':get_workflow_state(storage.get_bytes(case.s3_prefix,CASE_DICT_PATH).decode())}
 target=fallback_root/('fallback-'+cut+'-witness.json');temporary=target.with_name('.'+uuid.uuid4().hex)
 temporary.write_text(json.dumps(evidence));temporary.replace(target)
 deadline=time.monotonic()+90
 while not (fallback_root/('fallback-release-'+cut)).exists():
  if time.monotonic()>deadline:raise TimeoutError('Owned failure boundary was not released')
  await asyncio.sleep(.03)

async def fallback_apply(db,case,job):
 fallback_trace('apply_enter',case.id,job_id=job.id)
 await fallback_hold(db,case,job,'before-files')
 result=await fallback_original_settle(db,case,job)
 fallback_trace('apply_returned',case.id,job_id=job.id,result=result)
 if fallback_role=='recovery':
  with (fallback_root/'fallback-actual-recovery.jsonl').open('a') as stream:
   stream.write(json.dumps({'job_id':job.id,'pid':os.getpid(),'result':result})+'\n')
 return result
fallback.settle_failure_fallback=fallback_apply

async def fallback_applied(db,case,plan):
 job=await db.get(Job,plan.job_id)
 await fallback_hold(db,case,job,'after-files')
 return await fallback_original_applied(db,case,plan)
fallback._record_applied=fallback_applied

async def fallback_finish(db,case):
 fallback_trace('finish_committed_enter',case.id)
 selected=fallback_selection()
 if selected and selected['case_id']==case.id and selected.get('job_id'):
  job=await db.get(Job,selected['job_id'])
  if await db.get(DurableEvent,fallback.event_id(job,fallback.APPLIED)) is not None:
   await fallback_hold(db,case,job,'after-commit')
 result=await fallback_original_finish(db,case)
 fallback_trace('finish_committed_returned',case.id)
 return result
file_transactions.finish_committed=fallback_finish

async def fallback_recover_storage(lease, **kwargs):
 selected=fallback_selection()
 if (fallback_role=='crash' and selected and selected['cut']=='after-commit' and
     selected['case_id']==lease.case.id and selected.get('job_id')):
  async with SessionLocal() as db:
   case=await db.get(Case,lease.case.id);job=await db.get(Job,selected['job_id'])
   if await db.get(DurableEvent,fallback.event_id(job,fallback.APPLIED)) is not None:
    await fallback_hold(db,case,job,'after-commit')
 return await fallback_original_recover_storage(lease, **kwargs)
s3_storage_recovery.recover_reserved=fallback_recover_storage

async def fallback_recover(**kwargs):
 # Hold before entering the real scan, without supplying an invented outcome.
 while fallback_role!='recovery':
  selected=fallback_selection()
  if not selected or (fallback_root/('fallback-release-'+selected['cut'])).exists():break
  await asyncio.sleep(.03)
 return await fallback_original_recover(**kwargs)
fallback.recover_once=fallback_recover

async def fallback_pending(**kwargs):
 # Recheck after the actual SQL scan, before recovery takes a write lock. A
 # prior scan may overlap the next case's boundary hold.
 result=await fallback_original_pending(**kwargs)
 while fallback_role!='recovery':
  selected=fallback_selection()
  if not selected or (fallback_root/('fallback-release-'+selected['cut'])).exists():break
  await asyncio.sleep(.03)
 return result
fallback.pending=fallback_pending

async def fallback_delivery(job_id,**kwargs):
 # The requesting API need not win SQL claim. Delay other test APIs before
 # the real delivery so the process we deliberately terminate owns this Job.
 while fallback_role!='crash':
  selected=fallback_selection()
  if not selected or (fallback_root/('fallback-release-'+selected['cut'])).exists():break
  async with SessionLocal() as observing:
   job=await observing.get(Job,job_id)
   held=(job is not None and job.case_id==selected['case_id'] and job.status=='queued'
    and (not selected.get('job_id') or selected['job_id']==job_id))
  if not held:break
  await asyncio.sleep(.05)
 return await fallback_original_delivery(job_id,**kwargs)
job_delivery.deliver=fallback_delivery

@app.get('/api/v1/cases/{case_id}/testing/fallback')
async def fallback_observe(case_id:int,job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=False);job=await db.get(Job,job_id)
 assert job.case_id==case.id
 required=await db.get(DurableEvent,fallback.event_id(job,fallback.REQUIRED))
 applied=await db.get(DurableEvent,fallback.event_id(job,fallback.APPLIED))
 return {'sql_stage':case.workflow_state,'revision':case.dict_revision,'job_status':job.status,
  'required':json.loads(required.payload_json) if required else None,
  'applied':json.loads(applied.payload_json) if applied else None,'pending_ids':await fallback_original_pending()}

@app.post('/api/v1/jobs/{job_id}/testing/fallback')
async def fallback_repeat(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db,write=True)
 from app.services.workflow_state import ExecutionConflict
 try:
  result=await fallback.settle_failure_fallback(db,case,job)
 except ExecutionConflict as error:raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
 return result

@app.post('/api/v1/jobs/{job_id}/testing/fallback-drain-fault')
async def fallback_drain_fault(job_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db,write=True)
 async with committing(db,case,content_write=False):
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  if body.get('restore'):
   column,old,stamp=fallback_faults.pop(attempt.id);setattr(attempt,column,old);attempt.updated_at=stamp
  else:
   assert job.status==attempt.phase=='failed'
   column='claim_token' if body.get('mode')=='unclaimed' else 'write_checkpoint_json'
   fallback_faults[attempt.id]=(column,getattr(attempt,column),attempt.updated_at)
   setattr(attempt,column,None if column=='claim_token' else '{}')
 return {'owned_protocol_fault':True,'restored':bool(body.get('restore'))}
'''


def peer(api,store,role):
 value=IsolatedServer('fallback-'+role,app='fallback_peer_fixture:app')
 shared={key:value for key,value in api.env.items() if key.startswith('VIBEFLOW_')}
 if store:shared.update(store.settings())
 shared.update(VIBEFLOW_FALLBACK_SHARED_ROOT=str(api.root),VIBEFLOW_FALLBACK_ROLE=role)
 (value.root/'fallback_peer_fixture.py').write_text('import os\nos.environ.update('+repr(shared)+')\nfrom app.main import app\n'+FIXTURE,encoding='utf8')
 return value


def kill_owned_api(server,pid):
 """Verify ancestry and birth; terminate the same actual Windows process handle."""
 import ctypes,os,subprocess
 from ctypes import wintypes
 from app.services import process_identity_native as identity
 assert os.name=='nt' and type(pid) is int and pid>0 and server.proc.poll() is None
 expected=identity.observe(pid);launcher=server.proc.pid
 query=("$r=Get-CimInstance Win32_Process -Filter 'ProcessId = "+str(pid)+"'; "
  "$chain=@(); $p=$r; for($i=0;$i -lt 8 -and $null -ne $p;$i++){ $chain+=([int]$p.ProcessId); "
  "$next=[int]$p.ParentProcessId; $p=Get-CimInstance Win32_Process -Filter \"ProcessId = $next\" }; "
  "@{chain=$chain;command=$r.CommandLine}|ConvertTo-Json -Compress")
 result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',query],capture_output=True,text=True,timeout=10)
 assert result.returncode==0
 observed=json.loads(result.stdout);assert launcher in observed['chain']
 assert server.app in observed['command'] and str(server.port) in observed['command']
 kernel=ctypes.WinDLL('kernel32',use_last_error=True)
 kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
 kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT];kernel.TerminateProcess.restype=wintypes.BOOL
 kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.WaitForSingleObject.restype=wintypes.DWORD
 kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
 handle=kernel.OpenProcess(0x1000|0x100000|1,False,pid)
 if not handle:raise ctypes.WinError(ctypes.get_last_error())
 try:
  assert identity.windows_identity(handle)==expected
  if not kernel.TerminateProcess(handle,1):raise ctypes.WinError(ctypes.get_last_error())
  assert kernel.WaitForSingleObject(handle,10000)==0
 finally:kernel.CloseHandle(handle)
 server.proc.wait(timeout=10);assert identity.probe(expected)['state']=='exited'
 return launcher


def wait_for(function,description,seconds=60):
 deadline=time.monotonic()+seconds
 while True:
  value=function()
  if value:return value
  if time.monotonic()>deadline:raise TimeoutError(description)
  time.sleep(.1)


def scenario(api,*,store=None,**_):
 checks=[]
 cuts=['before-files','after-files','after-commit']
 if os.environ.get('VIBEFLOW_LIVE_FALLBACK_FOCUS'):
  assert os.environ['VIBEFLOW_LIVE_FALLBACK_FOCUS']=='after-commit'
  cuts=['after-commit']
 def save():
  (api.root/'failure-fallback-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
 for cut in cuts:
  api.create_case('Actual fallback API crash '+cut)
  fail=api.root/('fallback-native-fail-'+cut);release=api.root/('fallback-native-release-'+cut)
  started=api.root/('fallback-native-started-'+cut);executions=api.root/('fallback-native-count-'+cut)
  code=('import cadquery as cq\nfrom pathlib import Path\nimport time\nif Path('+repr(str(fail))+').exists():\n'
   ' with Path('+repr(str(executions))+').open("a") as stream:stream.write("once\\n")\n'
   ' Path('+repr(str(started))+').touch()\n'
   ' while not Path('+repr(str(release))+').exists():time.sleep(.03)\n'
   ' raise RuntimeError("ACTUAL_CADQUERY_FAILURE_FOR_RECOVERY")\n'
   'def build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n')
  seeded=put(api,'A-geo/geometry.py',code)
  if seeded.get('auto_preview_job'):api.wait_job(seeded['auto_preview_job'])
  else:api.wait_job(write(api,'POST',api.prefix+'/geometry:preview',{})['job_id'])
  api.advance('B');fail.touch()
  selection={'case_id':api.case_id,'cut':cut}
  hold=api.root/'fallback-hold.json';hold.write_text(json.dumps(selection))
  crash=peer(api,store,'crash');recovery=peer(api,store,'recovery')
  identifier=None;recovery_observation=None
  try:
   with crash:
    crash.token=api.token;crash.case_id=api.case_id;crash.prefix=api.prefix
    identifier=write(crash,'POST',api.prefix+'/geometry:preview',{})['job_id']
    selection['job_id']=identifier;hold.write_text(json.dumps(selection))
    wait_for(started.exists,'Actual CadQuery failure source did not start');release.touch()
    marker=api.root/('fallback-'+cut+'-witness.json')
    try:wait_for(marker.exists,'Actual failure did not reach '+cut)
    except BaseException:
     diagnostic={'job':api.call('GET','/jobs/'+str(identifier)),
      'fallback':api.call('GET',api.prefix+'/testing/fallback?job_id='+str(identifier))}
     (api.root/('fallback-'+cut+'-failure-diagnostic.json')).write_text(json.dumps(diagnostic,indent=2),encoding='utf8')
     raise
    witness=json.loads(marker.read_text())
    assert witness['job_id']==identifier and witness['job_status']=='failed' and witness['required_committed']
    assert witness['sql_stage']==('A' if cut=='after-commit' else 'B')
    assert witness['actual_file_stage']==('B' if cut=='before-files' else 'A')
    assert witness['applied_committed']==(cut=='after-commit')
    failed=api.wait_job(identifier,expected='failed')
    assert failed['attempt']['process_stop']['verified'] and failed['attempt']['handler']['key']=='geometry.preview'
    assert 'ACTUAL_CADQUERY_FAILURE_FOR_RECOVERY' in failed['log_backlog']
    launcher=kill_owned_api(crash,witness['pid'])
   if cut=='before-files':
    # Check the missing-proof guard before startup's independent, legitimate
    # drain-recovery service can reconstruct a certified v2 receipt.
    for fault_mode in ['missing-drain','unclaimed']:
     write(api,'POST','/jobs/'+str(identifier)+'/testing/fallback-drain-fault',{'mode':fault_mode})
     try:
      refused=api.call('POST','/jobs/'+str(identifier)+'/testing/fallback',{},expect=409)
      assert refused['detail']['code']=='recovery_required',refused
      observed=api.call('GET',api.prefix+'/testing/fallback?job_id='+str(identifier))
      assert observed['sql_stage']=='B' and observed['applied'] is None
     finally:write(api,'POST','/jobs/'+str(identifier)+'/testing/fallback-drain-fault',{'restore':True})
   with recovery:
    recovery.token=api.token;recovery.case_id=api.case_id;recovery.prefix=api.prefix
    def complete():
     value=api.call('GET',api.prefix+'/testing/fallback?job_id='+str(identifier))
     return value if value['applied'] is not None else None
    recovery_observation=wait_for(complete,'Restarted API did not recover the durable failure intent')
    assert recovery_observation['sql_stage']=='A' and identifier not in recovery_observation['pending_ids']
    raw=read_settled(api,api.prefix+'/raw?path=0-caseDict/case-events.jsonl',raw=True)
    events=[json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    assert len([event for event in events if event['type']=='workflow.fallback' and event.get('job_id')==identifier])==1
    before=read_settled(api,api.prefix+'/caseDict');assert before['workflow_state']=='A'
    with ThreadPoolExecutor(max_workers=2) as pool:
     results=list(pool.map(lambda server:write(server,'POST','/jobs/'+str(identifier)+'/testing/fallback',{}),[api,recovery]))
    assert all(result['replayed'] for result in results)
    assert api.call('GET',api.prefix+'/raw?path=0-caseDict/case-events.jsonl',raw=True)==raw
    assert api.call('GET',api.prefix+'/caseDict')['dict_revision']==before['dict_revision']
    # An old applied failure must not undo a later successful confirmation.
    fail.unlink();good=api.wait_job(write(api,'POST',api.prefix+'/geometry:preview',{})['job_id'])
    assert good['attempt']['outputs']['publication']['status']=='committed';api.advance('B')
    later=api.call('GET',api.prefix+'/caseDict')
    assert write(recovery,'POST','/jobs/'+str(identifier)+'/testing/fallback',{})['replayed']
    assert api.call('GET',api.prefix+'/caseDict')['workflow_state']=='B'
    assert api.call('GET',api.prefix+'/caseDict')['dict_revision']==later['dict_revision']
   assert executions.read_text().splitlines()==['once'] and crash.proc.poll() is not None and recovery.proc.poll() is not None
   checks.append({'cut':cut,'job_id':identifier,'actual_CadQuery_failure':True,'witness':witness,
    'actual_API_pid_killed':witness['pid'],'verified_launcher_pid':launcher,'required_intent_survived':True,
    'recovered_stage':'A','timeline_entries_for_failed_job':1,'two_API_duplicate_apply_replayed':True,
    'later_successful_B_not_regressed_by_old_failure':True,'actual_failed_native_executions':1,
    'missing_drain_refused':cut=='before-files','unclaimed_attempt_refused':cut=='before-files',
    'crash_and_recovery_API_exited':True});save()
  finally:
   release.touch();(api.root/('fallback-release-'+cut)).touch();hold.unlink(missing_ok=True)
 report={'passed':True,'backend':'s3' if store else 'fs','cuts':cuts,'checks':checks,'mock_llm_calls':0,
  'model_selects_operations':False,'scope':'N5-06 failed geometry stage/timeline recovery; Agent checkpoint/approval integration still pending'}
 (api.root/'failure-fallback.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent actual failure recovery model readback')
 return report


if __name__=='__main__':
 from s3testsupport import ObjectStore
 from testsupport import SERVER
 runtime=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
 asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,
  store_factory=lambda:ObjectStore(executable=runtime,volume_max=16,authenticated=True)))
