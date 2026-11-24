"""Actual MPI/UDF worker death, stopped native births and explicit recovery."""
import ast,asyncio,copy,ctypes,json,os,subprocess,sys,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from urllib.parse import quote
from cae_fixtures import cavity_2d
from live_llm_job_repeat_acceptance import main as live_main,put,write
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_professional_batch_cancel_acceptance import FIXTURE as BASE_FIXTURE,peer as base_peer
from live_llm_mpi_cancel_recovery_acceptance import actual_ranks
from live_llm_udf_cancel_recovery_acceptance import actual_phase,native_inspect
from workflow_batch_redis_support import Broker,Workers
from app.services import process_identity_native as identity
from app.services.owned_process import wsl_path
from app.services.casedict import set_block
from live_llm_mpi_workflow_acceptance import vectors
from testsupport import SERVER
import live_llm_workflow_batch_professional_acceptance as baseline
import workflow_batch_redis_support as worker_support

DELIVERY_OBSERVER=r'''
import json
from pathlib import Path
from app.services import job_delivery as observed_delivery
original_delivery=observed_delivery.deliver
async def recorded_real_delivery(job_id,**kwargs):
 result=await original_delivery(job_id,**kwargs)
 with (Path(READY).parent/'actual-deliveries.jsonl').open('a',encoding='utf8') as stream:
  stream.write(json.dumps({'job_id':job_id,'actual_result':result})+'\n')
 return result
observed_delivery.deliver=recorded_real_delivery
'''

GUARD_FIXTURE=r'''
from app.models import Job,JobAttempt,DurableEvent
from sqlalchemy import select
import copy as scope_copy,uuid
from app.services.job_scope_recovery import validate_certificate as validate_scope_certificate
@app.get('/api/v1/testing/professional-worker-guardians/{job_id}')
async def professional_worker_guardians(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id)
 if job is None:raise HTTPException(404)
 await get_owned_case(job.case_id,user,db,write=False)
 rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.case_id==job.case_id,
  DurableEvent.attempt_id.in_(select(JobAttempt.id).where(JobAttempt.job_id==job.id)),DurableEvent.type.in_([
   'attempt.scope_guardian_prepared','attempt.scope_guardian_observed',
   'attempt.native_scope_intent','attempt.native_scope_prepared','attempt.native_scope_allocated',
   'attempt.native_scope_admitted','attempt.native_scope_retired','attempt.native_scope_recovered'])).order_by(DurableEvent.seq)))
 return [{'type':r.type,'payload':json.loads(r.payload_json)} for r in rows]
@app.get('/api/v1/testing/professional-worker-certificate/{job_id}')
async def professional_worker_certificate(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id)
 if job is None:raise HTTPException(404)
 case=await get_owned_case(job.case_id,user,db,write=False)
 attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
 value=json.loads(attempt.write_checkpoint_json);validate_scope_certificate(case,job,attempt,value)
 mutations=[('claim',lambda x:x.update(claim_sha256='0'*64)),('process',lambda x:x.update(process_identity_json='{}')),
  ('children',lambda x:x.update(child_processes_json='{}')),('invented_stop',lambda x:x.update(stop={'verified':True})),
  ('state',lambda x:x.update(state='stopped')),('missing_guard',lambda x:x['recovery']['guardians'].pop()),
  ('membership',lambda x:x['recovery']['guardians'][0]['observed']['guardian_receipt']['membership'].update(pending=1)),
  ('owner_live',lambda x:x['recovery'].update(worker_exit={'state':'match','reason':'same_process'})),
  ('self_observer',lambda x:x['recovery'].update(observer=x['recovery']['worker']['process'])),
  ('invented_output',lambda x:x['recovery'].update(original_output_manifest_json='{"invented":true}')),
  ('missing_kernel',lambda x:x['recovery'].update(native_retirements=[])),
  ('wrong_kernel',lambda x:x['recovery']['native_retirements'][0]['retirement']['request']['arguments']['scope'].update(inode=1)),
  ('unverified_tree',lambda x:x['recovery']['native_retirements'][0].update(native_tree_verified=False)),
  ('wrong_order',lambda x:x['recovery']['native_history'].reverse()),
  ('wrong_root_guard',lambda x:x['recovery']['native_retirements'][0].update(root_guardian_sha256='0'*64))]
 refused=[]
 for name,mutate in mutations:
  changed=scope_copy.deepcopy(value);mutate(changed)
  try:validate_scope_certificate(case,job,attempt,changed)
  except (ValueError,KeyError,TypeError,AttributeError):refused.append(name)
  else:raise AssertionError('Corrupted real scope certificate accepted: '+name)
 return {'checkpoint':value,'actual_corruption_refusals':refused,'original_root_stop':json.loads(attempt.output_manifest_json).get('process_stop'),
  'original_child_stops':[c['stop'] for c in json.loads(attempt.child_processes_json)['children'].values()]}
@app.post('/api/v1/testing/professional-worker-redeliver/{job_id}')
async def professional_worker_redeliver(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id)
 if job is None:raise HTTPException(404)
 await get_owned_case(job.case_id,user,db,write=False)
 from arq import create_pool
 from app.services.distributed_queue import redis_settings,QUEUE_NAME
 pool=await create_pool(redis_settings)
 try:
  entry=await pool.enqueue_job('run_delivery',job.id,_job_id='actual-worker-death-redelivery-'+uuid.uuid4().hex,_queue_name=QUEUE_NAME)
  return {'enqueued':entry is not None}
 finally:await pool.close()
'''
FIXTURE=BASE_FIXTURE+GUARD_FIXTURE

def peer(api,store,broker,role):
 value=base_peer(api,store,broker,role);p=value.root/'redis_peer.py';code=p.read_text(encoding='utf8')+GUARD_FIXTURE
 ast.parse(code);p.write_text(code,encoding='utf8');return value


def kill_owned_worker(record):
 """Verify the ready birth and launcher ancestry, then kill that exact handle."""
 from ctypes import wintypes
 expected=record['identity'];pid=expected['pid'];launcher=record['process'].pid
 assert os.name=='nt' and record['process'].poll() is None and identity.probe(expected)['state']=='match'
 query=("$r=Get-CimInstance Win32_Process -Filter 'ProcessId = "+str(pid)+"'; "
  "$chain=@();$p=$r;for($i=0;$i -lt 8 -and $null -ne $p;$i++){ $chain+=([int]$p.ProcessId); "
  "$next=[int]$p.ParentProcessId;$p=Get-CimInstance Win32_Process -Filter \"ProcessId = $next\" }; "
  "@{chain=$chain;command=$r.CommandLine}|ConvertTo-Json -Compress")
 result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',query],capture_output=True,text=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
 assert result.returncode==0
 observed=json.loads(result.stdout);assert launcher in observed['chain'] and str(record['root']/'worker.py') in observed['command']
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
 record['process'].wait(timeout=10);probe=identity.probe(expected);assert probe['state']=='exited'
 record['log'].close()
 return {'identity':expected,'launcher_pid':launcher,'exit_code':record['process'].returncode,'actual_probe':probe,'intentional_actual_fault':True}


def stop_observed_native(distro,births):
 """Fault cleanup only: signal exact observed Linux births via opened pidfds."""
 program='''import importlib.util,json,os,signal,sys
s=importlib.util.spec_from_file_location('identity',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
out=[]
for expected in json.loads(sys.argv[2]):
 try:fd=os.pidfd_open(expected['pid'])
 except ProcessLookupError:out.append({'state':'exited'});continue
 try:
  try:actual=m.linux_identity(expected['pid'])
  except ProcessLookupError:out.append({'state':'exited'});continue
  if actual!=expected:out.append({'state':'mismatch'});continue
  signal.pidfd_send_signal(fd,signal.SIGKILL);out.append({'state':'signalled_exact_birth'})
 finally:os.close(fd)
print(json.dumps(out))'''
 result=subprocess.run(['wsl.exe','-d',distro,'--exec','python3','-I','-S','-c',program,
  wsl_path(Path(identity.__file__).resolve()),json.dumps(births)],capture_output=True,text=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
 assert result.returncode==0,'Exact observed native fault cleanup failed'
 value=json.loads(result.stdout);assert all(v['state'] in {'exited','signalled_exact_birth'} for v in value);return value


def exited_native(item):
 states=native_inspect(item['distro'],{'probe':[v['identity'] for v in item['members']]})
 return states if all(v['state']=='exited' for v in states) else None


def scenario(api,*,broker,loop,store=None,**_):
 (api.root/'professional-worker-death-entry-source.py').write_bytes(Path(__file__).read_bytes())
 owner=(api.token,api.case_id,api.prefix);original=baseline.scenario(api,broker=broker,loop=loop,store=store)
 api.token,api.case_id,api.prefix=owner
 first=original['checks'][0];delivery=first['all_jobs_actual_worker_bound']['jobs'];actors={}
 for mode,field in [('mpi','MPI_job'),('udf','UDF_solve_job')]:
  value=copy.copy(api);value.case_id=next(j['case_id'] for j in delivery if j['job_id']==first[field]);value.prefix='/cases/'+str(value.case_id);actors[mode]=value
 workers=Workers(api,broker,store);workers.sequence=len(list(api.root.glob('batch-worker-*')))
 source=peer(api,store,broker,'source');observer=peer(api,store,broker,'observer');recovery=peer(api,store,broker,'recovery')
 inputs=cavity_2d();checks=[];observations={};killed=[]
 def record(value):checks.append(value);(api.root/'professional-worker-death-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
 def waiting(server,rid,key):
  def observed():
   flow=server.call('GET','/workflow-runs/'+rid)
   assert flow['status'] not in {'failed','cancelled','interrupted'},flow['status']
   step=next(s for s in flow['steps'] if s['key']==key)
   return (flow,step) if step['status']=='awaiting_confirmation' else None
  return wait_for(observed,'Actual worker-death flow did not reach '+key,300)
 def confirm(server,actor,rid,key):
  flow,step=waiting(server,rid,key)
  for producer in flow['steps']:
   if producer['status']=='succeeded' and producer['attempts']:
    jid=producer['attempts'][-1]['job_id'];server.wait_job(jid,timeout=300)
    wait_for(lambda:server.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Actual producer did not drain',120)
    wait_for(lambda:not server.call('GET','/jobs/'+str(jid))['completion_pending'],'Actual producer did not settle',180)
  out=write(server,'POST',actor.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
   'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
  baseline.confirmed_dispatch_pending_kind(out)
 try:
  for mode,value in actors.items():
   dictionary=value.call('GET',value.prefix+'/caseDict')
   write(value,'POST',value.prefix+'/caseDict:advance',{'state':'A','confirmed':True,'expected_revision':dictionary['dict_revision']})
   control=inputs['C-run/case-1/system/controlDict'].replace('endTime         0.5;','endTime         1000000;').replace('deltaT          0.005;','deltaT          0.00001;').replace('writeInterval   20;','writeInterval   1000000000;')
   assert '1000000;' in control and '0.00001;' in control
   put(value,'C-run/case-1/system/controlDict',control)
   put(value,'C-run/case-1/system/blockMeshDict',inputs['C-run/case-1/system/blockMeshDict'].replace('(20 20 1)','(100 100 1)'))
  heads={mode:api.call('GET',v.prefix+'/testing/professional-cancel-heads') for mode,v in actors.items()}
  with source,observer:
   workers.start();body={'members':[],'max_concurrent':2}
   for mode,value in actors.items():
    parameters={'geometry_mode':'export','solve_mode':mode}
    parameters.update({'subdomains':2} if mode=='mpi' else {'udf_name':'myFunctionObject'})
    body['members'].append({'key':mode,'case_id':value.case_id,'target':'postprocess','run_dir':'C-run/case-1',
     'expected_revision':source.call('GET',value.prefix+'/caseDict')['dict_revision'],'parameters':parameters,
     'budget':{'timeout_seconds':180,'max_log_bytes':16777216}})
   key=uuid.uuid4().hex;batch=source.call('POST','/workflow-batches',body,expect=202,extra_headers={'Idempotency-Key':key});ids={v['key']:v['id'] for v in batch['members']}
   for stage in ['confirm_B','confirm_C']:
    for mode,value in actors.items():confirm(source,value,ids[mode],stage)
   flow=source.call('GET','/workflow-runs/'+ids['mpi']);jid=next(s for s in flow['steps'] if s['key']=='solve')['attempts'][0]['job_id']
   job,members,distro=actual_ranks(observer,jid);observations['mpi']={'job':job,'members':members,'distro':distro}
   _,job,members,distro=actual_phase(observer,api.root,ids['udf'],'solve');observations['udf']={'job':job,'members':members,'distro':distro}
   assert all('strace' in {v['comm'] for v in item['members']} for item in observations.values())
   pids={int(v['job']['attempt']['process_identity']['worker_id'].rsplit(':',2)[1]) for v in observations.values()}
   assert pids=={r['identity']['pid'] for r in workers.running} and len(pids)==2
   record({'stage':'actual_MPI_and_UDF_running_before_worker_death','batch':batch,'observations':observations,'old_heads':heads})
   with ThreadPoolExecutor(max_workers=2) as pool:killed=[f.result() for f in [pool.submit(kill_owned_worker,r) for r in workers.running]]
   workers.running=[]
   exits={}
   for mode,item in observations.items():
    births=[v['identity'] for v in item['members']]
    exits[mode]=wait_for(lambda:exited_native(item),'Observed native birth survived actual worker death',90)
   record({'stage':'actual_workers_and_observed_native_births_exited','workers':killed,'native_exits':exits})
  if store:assert not (recovery.root/'s3-cache').exists()
  with recovery:
   recovered={}
   for mode,item in observations.items():
    jid=item['job']['id'];job=recovery.wait_job(jid,expected='interrupted',timeout=90)
    assert recovery.call('GET',actors[mode].prefix+'/testing/professional-cancel-heads')==heads[mode]
    drain=recovery.call('GET','/testing/workflow-job-drain/'+str(jid))
    witnesses=recovery.call('GET','/testing/professional-worker-guardians/'+str(jid))
    certificate=recovery.call('GET','/testing/professional-worker-certificate/'+str(jid)) if drain['verified'] else None
    recovered[mode]={'job':job,'drain':drain,'old_head_preserved':True,'guardian_events':witnesses,'certificate':certificate}
   record({'stage':'actual_recovery_after_worker_exit','recovered':recovered,'no_model_or_native_replay':True})
   assert all(v['drain']['verified'] for v in recovered.values()),'Actual workers and native births exited, but durable recovery drain proof is unavailable'
   for value in recovered.values():
    certificate=value['certificate'];assert certificate['checkpoint']['schema']=='vibeflow.job-write-checkpoint.v4'
    assert certificate['original_root_stop'] is None and all(v is None for v in certificate['original_child_stops'])
    assert len(certificate['actual_corruption_refusals'])==15
   def interrupted_flow(rid):
    value=recovery.call('GET','/workflow-runs/'+rid)
    return value if value['status']=='interrupted' else None
   histories={rid:wait_for(lambda:interrupted_flow(rid),'Old worker-death workflow did not settle interrupted',60) for rid in ids.values()}
   old_batch=recovery.call('GET','/workflow-batches/'+batch['id']);assert old_batch['status']=='interrupted'
   old_jobs={mode:[j['id'] for j in recovery.call('GET',v.prefix+'/jobs')] for mode,v in actors.items()}
   assert recovery.call('POST','/workflow-batches',body,expect=202,extra_headers={'Idempotency-Key':key})['id']==batch['id']
   original_worker_code=worker_support.WORKER
   worker_support.WORKER=DELIVERY_OBSERVER+original_worker_code
   try:workers.start()
   finally:worker_support.WORKER=original_worker_code
   old_ids={item['job']['id'] for item in observations.values()}
   for jid in old_ids:assert recovery.call('POST','/testing/professional-worker-redeliver/'+str(jid),{})['enqueued']
   def actual_old_delivery_results():
    rows=[]
    for worker in workers.running:
     path=worker['root']/'actual-deliveries.jsonl'
     if path.exists():rows.extend(json.loads(line) for line in path.read_text(encoding='utf8').splitlines())
    selected=[v for v in rows if v['job_id'] in old_ids]
    return selected if {v['job_id'] for v in selected}==old_ids else None
   consumed=wait_for(actual_old_delivery_results,'Restarted real workers did not consume old redelivery',60)
   assert all(v['actual_result']=='already_owned_or_terminal' for v in consumed)
   assert old_jobs=={mode:[j['id'] for j in recovery.call('GET',v.prefix+'/jobs')] for mode,v in actors.items()}
   record({'stage':'actual_restarted_workers_refused_old_jobs','actual_Redis_delivery_results':consumed,'no_new_attempts_or_jobs':True})
   workspace=recovery.call('GET','/workspaces')[0]['id']
   serial=copy.copy(api);serial.case_id=recovery.call('POST','/workspaces/'+str(workspace)+'/cases',{'name':'Actual serial after native worker death'})['id'];serial.prefix='/cases/'+str(serial.case_id);actors['serial']=serial
   saved=serial.call('GET',serial.prefix+'/caseDict')
   text=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':{'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
   text=set_block(text,'numericalMethod',{'solver':'icoFoam'})
   text=set_block(text,'postProcessing',{'runDir':'C-run/case-1','fields':{'p':{'unit':'m2/s2'}},'dataExtraction':{'volumeAverages':{'meanPressure':{'field':'p','time':.5}}}})
   write(serial,'PUT',serial.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
   for value in actors.values():
    for path,content in inputs.items():put(value,path,content)
    dictionary=recovery.call('GET',value.prefix+'/caseDict')
    if dictionary['workflow_state']!='A':write(recovery,'POST',value.prefix+'/caseDict:advance',{'state':'A','confirmed':True,'expected_revision':dictionary['dict_revision']})
   seeded=put(serial,'A-geo/geometry.py','import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n')
   def drain(jid):
    recovery.wait_job(jid,timeout=300)
    wait_for(lambda:recovery.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'New explicit producer did not drain',120)
    wait_for(lambda:not recovery.call('GET','/jobs/'+str(jid))['completion_pending'],'New explicit producer did not settle',180)
   if seeded.get('auto_preview_job'):drain(seeded['auto_preview_job'])
   fresh_body={'members':[],'max_concurrent':2}
   for mode,value in actors.items():
    parameters={'geometry_mode':'export'}
    if mode!='serial':parameters.update(solve_mode=mode)
    parameters.update({'subdomains':2} if mode=='mpi' else {'udf_name':'myFunctionObject'} if mode=='udf' else {})
    fresh_body['members'].append({'key':mode,'case_id':value.case_id,'target':'postprocess','run_dir':'C-run/case-1',
     'expected_revision':recovery.call('GET',value.prefix+'/caseDict')['dict_revision'],'parameters':parameters,'budget':{'timeout_seconds':180,'max_log_bytes':16777216}})
   fresh_key=uuid.uuid4().hex;fresh=recovery.call('POST','/workflow-batches',fresh_body,expect=202,extra_headers={'Idempotency-Key':fresh_key});assert fresh['id']!=batch['id'];new_ids={v['key']:v['id'] for v in fresh['members']}
   for stage in ['confirm_B','confirm_C']:
    for mode,value in actors.items():confirm(recovery,value,new_ids[mode],stage)
   fields=[];proofs=[]
   for mode,value in actors.items():
    flow,_=waiting(recovery,new_ids[mode],'confirm_D');jid=next(s for s in flow['steps'] if s['key']=='solve')['attempts'][0]['job_id'];drain(jid)
    job=recovery.call('GET','/jobs/'+str(jid));publication=job['attempt']['outputs']['publication'];assert publication['status']=='committed' and job['attempt']['outputs']['completion']['alias_error'] is None
    manifest=recovery.call('GET','/testing/workflow-publication/'+publication['id'])['manifest'];assert manifest['descriptor']['handler']=={'mpi':'workflow-mpi.v1','udf':'workflow-udf.v1','serial':'solve.v1'}[mode]
    data=vectors(recovery.call('GET',value.prefix+'/raw?path='+quote('C-run/case-1/0.5/U'),raw=True));assert len(data)==400;fields.append(data)
    confirm(recovery,value,new_ids[mode],'confirm_D');flow,_=waiting(recovery,new_ids[mode],'confirm_E')
    post=next(s for s in flow['steps'] if s['key']=='postprocess')['attempts'][0]['job_id'];drain(post)
    metrics=json.loads(recovery.call('GET',value.prefix+'/raw?path='+quote('D-post/data/postprocess-'+str(post)+'/metrics.json'),raw=True))
    mean=next(v['value'] for v in metrics['metrics'] if v['name']=='meanPressure.p@0.5');assert metrics['readiness']['report_ready'] and abs(mean-.022259352333744136)<1e-5
    confirm(recovery,value,new_ids[mode],'confirm_E');proofs.append({'mode':mode,'actual_job':jid,'publication':publication['id'],'actual_cells':400,'actual_postprocess':post,'mean_pressure':mean,'actual_readiness':True})
   assert max(abs(a-b) for other in fields[1:] for row,other_row in zip(fields[0],other) for a,b in zip(row,other_row))<=1e-5
   completed=wait_for(lambda:(b if (b:=recovery.call('GET','/workflow-batches/'+fresh['id']))['status']=='succeeded' else None),'Explicit post-death replacement did not succeed',90)
   before={mode:[j['id'] for j in recovery.call('GET',v.prefix+'/jobs')] for mode,v in actors.items()}
   assert recovery.call('POST','/workflow-batches',fresh_body,expect=202,extra_headers={'Idempotency-Key':fresh_key})['id']==fresh['id']
   assert before=={mode:[j['id'] for j in recovery.call('GET',v.prefix+'/jobs')] for mode,v in actors.items()}
   assert recovery.call('GET','/workflow-batches/'+batch['id'])==old_batch
   for rid,history in histories.items():assert recovery.call('GET','/workflow-runs/'+rid)==history
   record({'stage':'explicit_post_worker_death_three_mode_replacement','new_batch_id':completed['id'],'actual_proofs':proofs,
    'old_histories_preserved':True,'same_key_no_replay':True,'independent_empty_S3_cache':bool(store),'explicit_B_C_D_E':True})
  workers.stop()
  result={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,'baseline':original,
   'intentional_actual_worker_deaths':killed,'replacement_workers':workers.receipts,
   'scope':'Actual MPI/UDF worker death, independent Windows and kernel scope closure, v4 audited drain with original missing stops preserved, real old-message refusal and explicit new MPI/UDF/serial workflows. Other mode/fault conditions remain separately audited.'}
  (api.root/'professional-worker-death.json').write_text(json.dumps(result,indent=2),encoding='utf8')
  api.create_case('Independent actual worker death recovery evidence readback');return result
 except BaseException as error:
  (api.root/'failed-professional-worker-death.json').write_text(json.dumps({'type':type(error).__name__,'detail':str(error)[:3000],'checks':checks},indent=2),encoding='utf8');raise
 finally:
  primary=sys.exc_info()[0];errors=[];native_cleanup=[]
  for mode,item in observations.items():
   try:
    births=[v['identity'] for v in item['members']];states=native_inspect(item['distro'],{'probe':births})
    action=stop_observed_native(item['distro'],births) if any(v['state']!='exited' for v in states) else None
    states=wait_for(lambda:exited_native(item),'Observed native fault cleanup not complete',30)
    native_cleanup.append({'mode':mode,'actual_observed_exits':states,'exact_birth_cleanup_action':action})
   except Exception as error:errors.append({'mode':mode,'native':type(error).__name__})
  for value in actors.values():
   try:
    for job in value.call('GET',value.prefix+'/jobs'):
     if job['status'] in {'queued','running'}:value.call('POST','/jobs/'+str(job['id'])+':cancel',{})
   except Exception as error:errors.append({'case_id':value.case_id,'cancel':type(error).__name__})
  try:workers.stop()
  except Exception as error:errors.append({'worker_stop':type(error).__name__})
  (api.root/'professional-worker-death-cleanup.json').write_text(json.dumps({'errors':errors,'intentional_worker_deaths':killed,'observed_native_cleanup':native_cleanup,'remaining_worker_stops':workers.receipts},indent=2),encoding='utf8')
  if errors and primary is None:raise RuntimeError('Actual worker-death cleanup was not verified; inspect saved receipt')


async def main():
 root=Path(tempfile.mkdtemp(prefix='professional-worker-death-redis-',dir=SERVER.parents[1]/'tmp'));broker=Broker(root);passed=False
 print('PROFESSIONAL_WORKER_DEATH_OWNED_REDIS '+str(root),flush=True)
 try:
  await broker.start();await live_main(selected_override=partial(scenario,broker=broker,loop=asyncio.get_running_loop()),fixture_override=FIXTURE,store_volume_max=16,environment_overrides=broker.settings());passed=True
 finally:
  try:await broker.stop()
  finally:(root/'result.json').write_text(json.dumps({'passed':passed,'mock_llm_calls':0,'actual_Redis_births':broker.births,'actual_Redis_stops':broker.stops},indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
