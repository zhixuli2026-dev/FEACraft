"""Actual Pandoc preparation races, requester API crash and durable native recovery."""
import asyncio,hashlib,json,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main,put,write,repeat
from live_llm_post_report_repeat_acceptance import FIXTURE as POST_FIXTURE,fail,seed_thermal
from live_llm_engineering_repeat_acceptance import case_root
from testsupport import IsolatedServer

DOC_RUN='C-run/case-1'
FIXTURE=POST_FIXTURE+r'''
import asyncio,os
from sqlalchemy import select
from fastapi import Depends,HTTPException
from app.db import get_db,SessionLocal
from app.models import Job,JobAttempt
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import job_delivery as g3_delivery,job_execution as g3_execution
from app.services.case_write_capacity import committing
g3_root=Path(os.environ.get('VIBEFLOW_REPEAT_SHARED_ROOT',str(Path(__file__).parent)))
g3_role=os.environ.get('VIBEFLOW_REPEAT_PEER_ROLE','main')
def g3_witness(name,value):
 target=g3_root/name;temporary=target.with_name('.'+target.name+'.'+uuid.uuid4().hex)
 temporary.write_text(json.dumps(value,indent=2));temporary.replace(target)
g3_complete=ReportHandler.complete_preparation
async def g3_preparation(self,prepared,case_id,user_id):
 result=await g3_complete(self,prepared,case_id,user_id)
 path=g3_root/'g3-hold-preparation.json'
 if path.exists():
  selected=json.loads(path.read_text())
  if selected['case_id']==case_id:
   tag=selected['tag'];assert tag in {'source-version','markdown'} and result.admission['ready']
   g3_witness('g3-prepared-'+tag+'.json',{'actual_complete_preparation_returned':True,
    'case_id':case_id,'pid':os.getpid(),'descriptor_sha256':g3_execution.request_hash(result.descriptor)})
   deadline=time.monotonic()+45
   while not (g3_root/('g3-release-'+tag)).exists():
    if time.monotonic()>deadline:raise TimeoutError('Owned native preparation hold not released')
    await asyncio.sleep(.02)
 return result
ReportHandler.complete_preparation=g3_preparation

@app.post('/api/v1/testing/g3-source-version/{job_id}')
async def g3_source_version(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id)
 if job is None:raise HTTPException(404)
 case=await get_owned_case(job.case_id,user,db,write=True)
 async with committing(db,case,content_write=False):
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
  assert job.status=='failed' and attempt.phase=='failed'
  old=attempt.version;attempt.version+=1
  await g3_execution.attempt_event(db,job,attempt,'testing.source_version_changed',{'previous':old,'version':attempt.version})
 return {'previous':old,'version':attempt.version,'owned_protocol_fault':True}

g3_original_deliver=g3_delivery.deliver
async def g3_deliver(job_id,**kwargs):
 path=g3_root/'g3-hold-committed-repeat.json'
 if path.exists():
  selected=json.loads(path.read_text())
  async with SessionLocal() as db:
   job=await db.get(Job,job_id)
   attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id).order_by(JobAttempt.number.desc()).limit(1))
   doc=json.loads(attempt.input_manifest_json) if attempt else {}
   match=doc.get('operation',{}).get('source_job_id')==selected['source_job_id']
   evidence={'job_id':job_id,'attempt_id':attempt.id if attempt else None,'pid':os.getpid(),
    'job_status':job.status if job else None,'attempt_phase':attempt.phase if attempt else None,
    'unclaimed':attempt.claim_token is None if attempt else False,
    'no_process_identity':attempt.process_identity_json=='{}' if attempt else False,
    'operation':doc.get('operation')}
  if match and evidence['job_status']=='queued':
   if g3_role=='main':return 'deferred'
   assert g3_role in {'crash-requester','recovery'}
   g3_witness('g3-'+g3_role+'-committed.json',evidence)
   deadline=time.monotonic()+45
   while not (g3_root/'g3-release-delivery').exists():
    if time.monotonic()>deadline:raise TimeoutError('Owned pre-delivery hold not released')
    await asyncio.sleep(.02)
 return await g3_original_deliver(job_id,**kwargs)
g3_delivery.deliver=g3_deliver
g3_release=g3_execution.release_process
async def g3_parent_release(owner,process):
 with (g3_root/'g3-actual-parent-releases.jsonl').open('a',encoding='utf8') as stream:
  stream.write(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id,'api_pid':os.getpid()})+'\n')
 return await g3_release(owner,process)
g3_execution.release_process=g3_parent_release
'''

def body_for(api,source):
 option=api.call('GET','/jobs/'+str(source)+'/operations')
 return {'action':'rerun','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version'],
  'expected_revision':option['expected_revision'],'expected_event_sequence':option['expected_event_sequence'],
  'parameters':{'run_dir':DOC_RUN,'mode':'markdown','allow_standalone':True}}

def guarded_request(api,source,record,marker):
 deadline=time.monotonic()+30;record.setdefault('pre_admission_rejections',[])
 while True:
  response=api.call('POST','/jobs/'+str(source)+':repeat',record['body'],expect=(202,409),extra_headers={'Idempotency-Key':record['key']})
  detail=response.get('detail',{});code=detail.get('code') if isinstance(detail,dict) else None
  if code not in {'storage_quota_busy','validation_busy','input_stale'} or marker.exists():return response
  record['pre_admission_rejections'].append(code)
  if time.monotonic()>deadline:raise TimeoutError('Owned admission did not settle before preparation')
  if code=='input_stale':record.update(body=body_for(api,source),key=uuid.uuid4().hex)
  time.sleep(.05)

def wait_file(path,future=None):
 deadline=time.monotonic()+25
 while not path.exists():
  if future is not None and future.done():raise AssertionError('Request ended before the owned hold: '+str(future.result()))
  if time.monotonic()>deadline:raise TimeoutError('Owned interleaving did not reach '+path.name)
  time.sleep(.03)
 return json.loads(path.read_text(encoding='utf8'))

def preparation_race(api,source,tag,mutation,expected):
 record={'body':body_for(api,source),'key':uuid.uuid4().hex};before={j['id'] for j in api.call('GET',api.prefix+'/jobs')}
 hold=api.root/'g3-hold-preparation.json';hold.write_text(json.dumps({'case_id':api.case_id,'tag':tag}))
 with ThreadPoolExecutor(max_workers=1) as pool:
  request=pool.submit(guarded_request,api,source,record,api.root/('g3-prepared-'+tag+'.json'))
  try:
   witness=wait_file(api.root/('g3-prepared-'+tag+'.json'),request)
   assert witness['actual_complete_preparation_returned']
   mutation()
  finally:
   (api.root/('g3-release-'+tag)).touch();hold.unlink(missing_ok=True)
  denied=request.result(timeout=30)
 assert denied['detail']['code']==expected,denied
 assert {j['id'] for j in api.call('GET',api.prefix+'/jobs')}==before
 return {'tag':tag,'actual_native_preparation_completed':True,'mutation_committed_while_preparation_paused':True,
  'code':expected,'no_new_job':True,'source_job':source,'pre_admission_rejections':record['pre_admission_rejections']}

def peer(api,store,role):
 value=IsolatedServer('repeat-interleaving-'+role,app='g3_peer_fixture:app')
 shared={key:value for key,value in api.env.items() if key.startswith('VIBEFLOW_')}
 if store:shared.update(store.settings())
 shared.update(VIBEFLOW_REPEAT_SHARED_ROOT=str(api.root),VIBEFLOW_REPEAT_PEER_ROLE=role)
 (value.root/'g3_peer_fixture.py').write_text('import os\nos.environ.update('+repr(shared)+')\nimport json\nfrom app.main import app\n'+FIXTURE,encoding='utf8')
 return value

def kill_owned_api(requester,pid):
 """Match the real Windows child and birth marker, terminate the same handle."""
 import ctypes,os,subprocess
 from ctypes import wintypes
 from app.services import process_identity_native as identity
 assert os.name=='nt' and type(pid) is int and pid>0 and requester.proc.poll() is None
 expected=identity.observe(pid);launcher=requester.proc.pid
 query=("$r=Get-CimInstance Win32_Process -Filter 'ProcessId = "+str(pid)+"'; "
  "$chain=@(); $p=$r; for($i=0;$i -lt 8 -and $null -ne $p;$i++){ $chain+=([int]$p.ProcessId); "
  "$next=[int]$p.ParentProcessId; $p=Get-CimInstance Win32_Process -Filter \"ProcessId = $next\" }; "
  "@{chain=$chain;command=$r.CommandLine}|ConvertTo-Json -Compress")
 result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',query],capture_output=True,text=True,timeout=10)
 assert result.returncode==0,'Owned API ancestry observation failed'
 process=json.loads(result.stdout);assert launcher in process['chain']
 assert 'g3_peer_fixture:app' in process['command'] and str(requester.port) in process['command']
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
  assert kernel.WaitForSingleObject(handle,10000)==0,'Owned API did not exit'
 finally:kernel.CloseHandle(handle)
 requester.proc.wait(timeout=10);assert identity.probe(expected)['state']=='exited'
 return launcher

def crash_recovery(api,store,source):
 (api.root/'g3-hold-committed-repeat.json').write_text(json.dumps({'source_job_id':source}))
 requester=peer(api,store,'crash-requester');recovery=peer(api,store,'recovery')
 recovered=None;failed_request=None
 try:
  with requester:
   requester.token=api.token
   record={'body':body_for(requester,source),'key':uuid.uuid4().hex}
   with ThreadPoolExecutor(max_workers=1) as pool:
    request=pool.submit(guarded_request,requester,source,record,api.root/'g3-crash-requester-committed.json')
    witness=wait_file(api.root/'g3-crash-requester-committed.json',request)
    body,key=record['body'],record['key']
    assert witness['job_status']=='queued' and witness['attempt_phase']=='queued' and witness['unclaimed'] and witness['no_process_identity']
    assert requester.proc.poll() is None
    persisted=api.call('GET','/jobs/'+str(witness['job_id']))
    assert persisted['status']=='queued' and persisted['attempt']['operation']['source_job_id']==source
    assert not [r for r in releases(api) if r['job_id']==witness['job_id']]
    # Windows venv redirectors can own a distinct real Python child. Verify its
    # parent chain, cwd and exact app/port, then kill that actual API process.
    launcher_pid=kill_owned_api(requester,witness['pid'])
    try:request.result(timeout=10)
    except Exception as error:failed_request=type(error).__name__
    else:raise AssertionError('Killed requester unexpectedly returned success')
  with recovery:
   recovery.token=api.token
   observed=wait_file(api.root/'g3-recovery-committed.json')
   assert observed['job_id']==witness['job_id'] and observed['attempt_id']==witness['attempt_id']
   assert observed['unclaimed'] and observed['no_process_identity']
   replay=api.call('POST','/jobs/'+str(source)+':repeat',body,expect=202,extra_headers={'Idempotency-Key':key})
   assert replay['id']==witness['job_id'] and not replay['created'] and replay['status']=='queued'
   (api.root/'g3-release-delivery').touch()
   recovered=api.wait_job(witness['job_id'],timeout=180)
   assert recovered['attempt']['outputs']['publication']['status']=='committed'
   assert recovered['attempt']['operation']['source_job_id']==source
   assert recovered['attempt']['number']==1 and recovered['attempt']['root_job_id']==recovered['id']
   actual=[r for r in releases(api) if r['job_id']==witness['job_id']];assert len(actual)==1
   children=recovered['attempt']['child_processes']['children']
   assert children and all(row['stop'] and row['stop']['verified'] for row in children.values())
   assert sum(row['purpose']=='pandoc_compile' and row['caller']=='worker' for row in children.values())==2
   again=repeat(api,source,body,key);assert again['id']==recovered['id'] and not again['created']
   assert len([r for r in releases(api) if r['job_id']==witness['job_id']])==1
 finally:
  (api.root/'g3-release-delivery').touch()
  (api.root/'g3-hold-committed-repeat.json').unlink(missing_ok=True)
 report={'source_job':source,'recovered_job':recovered['id'],'attempt_id':witness['attempt_id'],
  'actual_requester_api_killed_after_commit_before_launch':True,'failed_http_request':failed_request,
  'restarted_api_found_same_durable_job':True,'same_key_before_and_after_execution':True,'actual_parent_release_count':1,
  'actual_worker_pandoc_compile_children':2,'requester_pid':witness['pid'],'requester_launcher_pid':launcher_pid,
  'requester_exited':requester.proc.poll() is not None,'recovery_exited':recovery.proc.poll() is not None,
  'requester_root':str(requester.root),'recovery_root':str(recovery.root),'no_mock_model_or_native_result':True}
 (api.root/'g3-crash-recovery.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report

def releases(api):
 path=api.root/'g3-actual-parent-releases.jsonl'
 return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()] if path.exists() else []

def scenario(api,*,store=None,**_):
 api.create_case('Native document repeat preparation and crash windows')
 saved=api.call('GET',api.prefix+'/caseDict');text=set_block(saved['content'],'postProcessing',{'runDir':DOC_RUN})
 write(api,'PUT',api.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
 original='# Actual preparation and recovery\n\nORIGINAL_G3_DOCUMENT\n';put(api,'E-report/report.md',original)
 source,failed,_=fail(api,'report.build','/report:build',{})
 root=case_root(api,failed,store);old=root/failed['attempt']['workspace']['run_path']/'E-report/report.md'
 assert old.read_text(encoding='utf8')==original
 current=original.replace('ORIGINAL_G3_DOCUMENT','CURRENT_G3_DOCUMENT')
 races=[preparation_race(api,source,'source-version',lambda:write(api,'POST','/testing/g3-source-version/'+str(source),{}),'state_conflict'),
  preparation_race(api,source,'markdown',lambda:put(api,'E-report/report.md',current),'input_stale')]
 crash=crash_recovery(api,store,source)
 assert old.read_text(encoding='utf8')==original
 html=api.call('GET',api.prefix+'/raw?path=E-report/build/report.html',raw=True)
 assert b'CURRENT_G3_DOCUMENT' in html and b'ORIGINAL_G3_DOCUMENT' not in html
 assert api.call('GET','/jobs/'+str(source))['status']=='failed'
 active=[]
 def confirm_observer(api,flow,step):
  if step['key']!='confirm_B':return
  producer=next(row for row in flow['steps'] if row['key']=='geometry')
  identifier=producer['attempts'][-1]['job_id'];option=api.call('GET','/jobs/'+str(identifier)+'/operations')
  assert not option['rerun']['available'] and option['rerun']['reason']['code']=='state_conflict'
  body={'action':'rerun','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version'],
   'expected_revision':option['expected_revision'],'expected_event_sequence':option['expected_event_sequence'],'parameters':{}}
  before={row['id'] for row in api.call('GET',api.prefix+'/jobs')}
  denied=repeat(api,identifier,body,uuid.uuid4().hex,expect=409);assert denied['detail']['code']=='state_conflict'
  assert {row['id'] for row in api.call('GET',api.prefix+'/jobs')}==before
  active.append({'actual_workflow':flow['id'],'actual_waiting_confirmation':'confirm_B','finished_producer_job':identifier,
   'repeat_refused_while_original_workflow_active':True,'no_new_job':True})
 api.create_case('Actual active workflow repeat refusal');seed_thermal(api,confirm_observer=confirm_observer);assert len(active)==1
 report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','preparation_races':races,
  'actual_requester_crash_recovery':crash,'old_failed_document_and_frozen_input_preserved':True,
  'current_document_actual_pandoc_html':True,'active_workflow':active,'model_selects_operations':False,
  'fault_scope':'native prep is real; source-version mutation is owned SQL fault; requester kill and restart are actual processes'}
 (api.root/'repeat-interleavings.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent crash and preparation evidence model readback');return report

if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
