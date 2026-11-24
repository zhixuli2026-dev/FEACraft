"""Real running MPI/UDF and queued serial batch cancellation and replacement."""
import ast,asyncio,copy,hashlib,json,math,sys,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from urllib.parse import quote
from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main as live_main,put,write
from live_llm_failure_fallback_acceptance import wait_for,kill_owned_api
from live_llm_workflow_batch_redis_acceptance import peer as redis_peer
from live_llm_mpi_cancel_recovery_acceptance import actual_ranks
from live_llm_udf_cancel_recovery_acceptance import actual_phase,native_inspect
from live_llm_mpi_workflow_acceptance import vectors
from workflow_batch_redis_support import Broker,Workers
from testsupport import SERVER
import live_llm_workflow_batch_professional_acceptance as baseline

EXTRA=r'''
from app.services.artifact_publication import current
from app import storage
import hashlib
@app.get('/api/v1/cases/{case_id}/testing/professional-cancel-heads')
async def professional_cancel_heads(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=False);_,row=await current(db,case.id,'solve')
 path='C-run/case-1/0.5/U'
 return {'solve':row.id if row else None,'field_sha256':hashlib.sha256(storage.get_bytes(case.s3_prefix,path)).hexdigest() if storage.exists(case.s3_prefix,path) else None}
'''
FIXTURE=baseline.FIXTURE+EXTRA


def peer(api,store,broker,role):
 value=redis_peer(api,store,broker,role);path=value.root/'redis_peer.py'
 code=path.read_text(encoding='utf8')+EXTRA;ast.parse(code);path.write_text(code,encoding='utf8');return value


def scenario(api,*,broker,loop,store=None,**_):
 (api.root/'professional-cancel-entry-source.py').write_bytes(Path(__file__).read_bytes())
 owner=(api.token,api.case_id,api.prefix)
 original=baseline.scenario(api,broker=broker,loop=loop,store=store)
 # The baseline creates a separate readback account at its end. Continue the
 # professional cases under their original owner, whose rights remain real.
 api.token,api.case_id,api.prefix=owner
 first=original['checks'][0];delivery=first['all_jobs_actual_worker_bound']['jobs']
 cases={mode:next(j for j in delivery if j['job_id']==first[field])['case_id']
  for mode,field in [('mpi','MPI_job'),('udf','UDF_solve_job')]}
 actors={};checks=[];workers=Workers(api,broker,store)
 workers.sequence=len(list(api.root.glob('batch-worker-*')))
 run_dir='C-run/case-1';inputs=cavity_2d()
 def record(v):
  checks.append(v);(api.root/'professional-batch-cancel-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
 def actor(cid):
  value=copy.copy(api);value.case_id=cid;value.prefix='/cases/'+str(cid);return value
 for mode,cid in cases.items():actors[mode]=actor(cid)
 workspace=api.call('GET','/workspaces')[0]['id']
 cid=api.call('POST','/workspaces/'+str(workspace)+'/cases',{'name':'Actual queued serial professional member'})['id']
 actors['serial']=actor(cid)
 def reset(server,value):
  dictionary=server.call('GET',value.prefix+'/caseDict')
  if dictionary['workflow_state']!='A':
   write(server,'POST',value.prefix+'/caseDict:advance',{'state':'A','confirmed':True,'expected_revision':dictionary['dict_revision']})
 def waiting(server,rid,key):
  def observed():
   flow=server.call('GET','/workflow-runs/'+rid)
   if flow['status'] in {'failed','cancelled','interrupted'}:
    jobs={str(a['job_id']):server.call('GET','/jobs/'+str(a['job_id'])) for s in flow['steps'] for a in s['attempts']}
    (api.root/'failed-professional-cancel-workflow.json').write_text(json.dumps({'workflow':flow,'jobs':jobs},indent=2),encoding='utf8')
    raise AssertionError('Actual professional cancel workflow failed; inspect saved jobs')
   step=next(s for s in flow['steps'] if s['key']==key)
   return (flow,step) if step['status']=='awaiting_confirmation' else None
  return wait_for(observed,'Professional cancel member did not reach '+key,300)
 def drain(server,jid):
  server.wait_job(jid,timeout=300)
  wait_for(lambda:server.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Professional cancel writer did not drain',120)
  wait_for(lambda:not server.call('GET','/jobs/'+str(jid))['completion_pending'],'Professional cancel publication did not settle',180)
 def confirm(server,value,rid,key):
  flow,step=waiting(server,rid,key)
  for s in flow['steps']:
   if s['status']=='succeeded' and s['attempts']:drain(server,s['attempts'][-1]['job_id'])
  out=write(server,'POST',value.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
   'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
  dispatch=out.get('workflow_dispatch',{})
  kind=baseline.confirmed_dispatch_pending_kind(out)
  if kind:
   # A durable ready workflow can wait before a Job exists when all user
   # slots are occupied or another real owner write is settling. The SQL
   # dispatcher must resume it; never repeat an already committed confirmation.
   record({'stage':'actual_'+kind+'_backpressure','run_id':rid,'confirmation':key,'dispatch':dispatch})
 def request(server):
  members=[]
  for mode,value in actors.items():
   parameters={'geometry_mode':'export'}
   if mode=='mpi':parameters.update(solve_mode='mpi',subdomains=2)
   if mode=='udf':parameters.update(solve_mode='udf',udf_name='myFunctionObject')
   members.append({'key':mode,'case_id':value.case_id,'target':'postprocess','run_dir':run_dir,
    'expected_revision':server.call('GET',value.prefix+'/caseDict')['dict_revision'],'parameters':parameters,
    'budget':{'timeout_seconds':180,'max_log_bytes':16777216}})
  return {'members':members,'max_concurrent':2}
 source=peer(api,store,broker,'source');observer=peer(api,store,broker,'observer');recovery=peer(api,store,broker,'recovery')
 try:
  serial=actors['serial'];saved=serial.call('GET',serial.prefix+'/caseDict')
  text=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':{'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
  text=set_block(text,'numericalMethod',{'solver':'icoFoam'})
  text=set_block(text,'postProcessing',{'runDir':run_dir,'fields':{'p':{'unit':'m2/s2'}},
   'dataExtraction':{'volumeAverages':{'meanPressure':{'field':'p','time':.5}}}})
  write(serial,'PUT',serial.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
  for path,body in inputs.items():put(serial,path,body)
  seeded=put(serial,'A-geo/geometry.py','import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n')
  if seeded.get('auto_preview_job'):drain(serial,seeded['auto_preview_job'])
  for mode,value in actors.items():
   reset(api,value)
   if mode=='serial':continue
   control=inputs[run_dir+'/system/controlDict'].replace('endTime         0.5;','endTime         1000000;').replace(
    'deltaT          0.005;','deltaT          0.00001;').replace('writeInterval   20;','writeInterval   1000000000;')
   assert all(v in control for v in ['1000000;','0.00001;','1000000000;'])
   mesh=inputs[run_dir+'/system/blockMeshDict'].replace('(20 20 1)','(100 100 1)');assert mesh!=inputs[run_dir+'/system/blockMeshDict']
   put(value,run_dir+'/system/controlDict',control);put(value,run_dir+'/system/blockMeshDict',mesh)
  original_heads={mode:api.call('GET',value.prefix+'/testing/professional-cancel-heads') for mode,value in actors.items()}
  assert all(original_heads[m]['solve'] and original_heads[m]['field_sha256'] for m in ['mpi','udf'])
  assert original_heads['serial']=={'solve':None,'field_sha256':None}
  with source,observer:
   workers.start();worker_ids=workers.identities();body=request(source);key=uuid.uuid4().hex
   batch=source.call('POST','/workflow-batches',body,expect=202,extra_headers={'Idempotency-Key':key})
   ids={m['key']:m['id'] for m in batch['members']}
   for value in actors.values():confirm(source,value,ids[next(m for m,v in actors.items() if v is value)],'confirm_B')
   # Serial's mesh is ready before both available slots enter long native solves.
   waiting(source,ids['serial'],'confirm_C')
   for mode in ['mpi','udf']:confirm(source,actors[mode],ids[mode],'confirm_C')
   flow=source.call('GET','/workflow-runs/'+ids['mpi']);step=next(s for s in flow['steps'] if s['key']=='solve')
   mpi_job,members,distro=actual_ranks(observer,step['attempts'][0]['job_id'])
   observations={'mpi':{'job':mpi_job,'members':members,'distro':distro}}
   _,udf_job,members,distro=actual_phase(observer,api.root,ids['udf'],'solve')
   observations['udf']={'job':udf_job,'members':members,'distro':distro}
   assert all('strace' in {m['comm'] for m in o['members']} for o in observations.values())
   record({'stage':'actual_running_MPI_and_UDF_observed','observations':observations})
   confirm(source,actors['serial'],ids['serial'],'confirm_C')
   def queued_serial():
    flow=observer.call('GET','/workflow-runs/'+ids['serial']);step=next(s for s in flow['steps'] if s['key']=='solve')
    if not step['attempts']:
     assert step['status']=='ready'
     return {'job_id':None,'step_id':step['id'],'mode':'durable_ready_before_job_admission'}
    assert len(step['attempts'])==1;job=observer.call('GET','/jobs/'+str(step['attempts'][0]['job_id']))
    assert job['attempt']['phase']=='queued' and job['attempt']['process_identity'] is None
    return {'job_id':job['id'],'step_id':step['id'],'mode':'admitted_queued_attempt'}
   queued=wait_for(queued_serial,'Third professional solve did not remain queued',60)
   delivered=source.call('GET','/testing/batch-delivery/'+batch['id'])
   assert all(o['job']['attempt']['process_identity']['worker_id'] for o in observations.values())
   assert all(j['worker'] in worker_ids for j in delivered['jobs'] if j['job_id'] in [o['job']['id'] for o in observations.values()])
   record({'stage':'actual_two_native_and_one_queued','batch_id':batch['id'],'observations':observations,
    'queued_serial_job':queued['job_id'],'queued_serial':queued,'actual_workers':worker_ids,'actual_delivery':delivered,'old_solve_heads':original_heads})
   token=api.token
   try:
    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'Other isolated batch owner','password':'isolated-test-123'})
    api.token=other['token'];api.call('POST','/workflow-batches/'+batch['id']+':cancel',{'expected_version':batch['version']},expect=404)
   finally:api.token=token
   def cancel(server):return server.call('POST','/workflow-batches/'+batch['id']+':cancel',{'expected_version':batch['version']})
   with ThreadPoolExecutor(max_workers=2) as pool:answers=[f.result() for f in [pool.submit(cancel,observer),pool.submit(cancel,api)]]
   assert all(a['version']==1 and a['status'] in {'cancelling','cancelled'} for a in answers)
   stopped=[]
   for mode,observed in observations.items():
    jid=observed['job']['id'];observer.wait_job(jid,expected='cancelled',timeout=120)
    wait_for(lambda:observer.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Cancelled professional writer did not drain',120)
    def terminal():
     job=observer.call('GET','/jobs/'+str(jid));attempt=job['attempt'];stop=attempt.get('process_stop')
     return job if attempt['phase']=='cancelled' and stop and stop['verified'] else None
    job=wait_for(terminal,'Professional cancellation did not persist verified stop',90)
    children=list(job['attempt']['child_processes']['children'].values());assert len(children)==1 and children[0]['stop']['verified']
    assert children[0]['stop']['native_state']=='exited' and (job['attempt']['outputs'].get('publication') or {}).get('status')!='committed'
    def exited():
     states=native_inspect(observed['distro'],{'probe':[m['identity'] for m in observed['members']]})
     return states if all(s['state']=='exited' for s in states) else None
    exits=wait_for(exited,'Observed cancelled native member survived',90)
    assert source.call('GET',actors[mode].prefix+'/testing/professional-cancel-heads')==original_heads[mode]
    stopped.append({'mode':mode,'job_id':jid,'root_and_native_stop_verified':True,'observed_members_all_exited':exits,
     'incomplete_not_published':True,'old_solve_head_and_field_preserved':True})
   if queued['job_id'] is not None:
    queued_job=observer.wait_job(queued['job_id'],expected='cancelled');assert queued_job['attempt']['process_identity'] is None
   else:
    serial_flow=observer.call('GET','/workflow-runs/'+ids['serial'])
    serial_step=next(s for s in serial_flow['steps'] if s['id']==queued['step_id'])
    assert serial_step['status']=='cancelled' and not serial_step['attempts']
   assert source.call('GET',actors['serial'].prefix+'/testing/professional-cancel-heads')==original_heads['serial']
   settled=wait_for(lambda:(b if (b:=observer.call('GET','/workflow-batches/'+batch['id']))['status']=='cancelled' else None),'Cancelled batch did not settle',90)
   assert cancel(observer)['version']==1
   histories={rid:observer.call('GET','/workflow-runs/'+rid) for rid in ids.values()}
   before=[[j['id'] for j in source.call('GET',v.prefix+'/jobs')] for v in actors.values()]
   pid=source.call('GET','/testing/workflow-api-owner')['pid'];launcher=kill_owned_api(source,pid)
   record({'stage':'cross_API_professional_batch_cancelled','actual_native_stops':stopped,'queued_serial_never_started':True,
    'other_owner_refused':True,'concurrent_cancel_single_version':True,'source_API_killed':pid,'verified_launcher_pid':launcher})
  if store:assert not (recovery.root/'s3-cache').exists()
  with recovery:
   assert recovery.call('GET','/workflow-batches/'+batch['id'])==settled
   assert recovery.call('POST','/workflow-batches',body,expect=202,extra_headers={'Idempotency-Key':key})['id']==batch['id']
   assert before==[[j['id'] for j in recovery.call('GET',v.prefix+'/jobs')] for v in actors.values()]
   for rid,history in histories.items():assert recovery.call('GET','/workflow-runs/'+rid)==history
   for value in actors.values():
    for path,content in inputs.items():put(value,path,content)
    reset(recovery,value)
   fresh_body=request(recovery);fresh_key=uuid.uuid4().hex
   fresh=recovery.call('POST','/workflow-batches',fresh_body,expect=202,extra_headers={'Idempotency-Key':fresh_key})
   assert fresh['id']!=batch['id'];new_ids={m['key']:m['id'] for m in fresh['members']}
   for stage in ['confirm_B','confirm_C']:
    for mode,value in actors.items():confirm(recovery,value,new_ids[mode],stage)
   fields=[];proofs=[]
   for mode,value in actors.items():
    flow,_=waiting(recovery,new_ids[mode],'confirm_D');step=next(s for s in flow['steps'] if s['key']=='solve')
    jid=step['attempts'][0]['job_id'];drain(recovery,jid)
    job=recovery.call('GET','/jobs/'+str(jid));pub=job['attempt']['outputs']['publication'];assert pub['status']=='committed'
    assert job['attempt']['outputs']['completion']['alias_error'] is None
    manifest=recovery.call('GET','/testing/workflow-publication/'+pub['id'])['manifest']
    assert manifest['descriptor']['handler']=={'mpi':'workflow-mpi.v1','udf':'workflow-udf.v1','serial':'solve.v1'}[mode]
    data=vectors(recovery.call('GET',value.prefix+'/raw?path='+quote(run_dir+'/0.5/U'),raw=True));assert len(data)==400;fields.append(data)
    confirm(recovery,value,new_ids[mode],'confirm_D');flow,_=waiting(recovery,new_ids[mode],'confirm_E')
    post=next(s for s in flow['steps'] if s['key']=='postprocess');post_jid=post['attempts'][0]['job_id'];drain(recovery,post_jid)
    result=json.loads(recovery.call('GET',value.prefix+'/raw?path='+quote('D-post/data/postprocess-'+str(post_jid)+'/metrics.json'),raw=True))
    mean=next(r['value'] for r in result['metrics'] if r['name']=='meanPressure.p@0.5')
    assert result['readiness']['report_ready'] and abs(mean-.022259352333744136)<1e-5
    confirm(recovery,value,new_ids[mode],'confirm_E')
    proofs.append({'mode':mode,'actual_job':jid,'publication':pub['id'],'actual_cells':400,'actual_postprocess':post_jid,'mean_pressure':mean,'actual_readiness':True})
   assert max(abs(a-b) for other in fields[1:] for row,other_row in zip(fields[0],other) for a,b in zip(row,other_row))<=1e-5
   completed=wait_for(lambda:(b if (b:=recovery.call('GET','/workflow-batches/'+fresh['id']))['status']=='succeeded' else None),'Explicit replacement batch did not succeed',90)
   stable=[[j['id'] for j in recovery.call('GET',v.prefix+'/jobs')] for v in actors.values()]
   assert recovery.call('POST','/workflow-batches',fresh_body,expect=202,extra_headers={'Idempotency-Key':fresh_key})['id']==fresh['id']
   assert stable==[[j['id'] for j in recovery.call('GET',v.prefix+'/jobs')] for v in actors.values()]
   assert recovery.call('GET','/workflow-batches/'+batch['id'])==settled
   for rid,history in histories.items():assert recovery.call('GET','/workflow-runs/'+rid)==history
   record({'stage':'explicit_new_three_mode_batch_completed','new_batch':completed['id'],'actual_proofs':proofs,
    'old_history_preserved':True,'same_key_no_replay':True,'independent_empty_S3_cache':bool(store),'explicit_B_C_D_E':True})
  workers.stop()
  report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,'workers':workers.receipts,
   'baseline':original,'scope':'Real Redis MPI/UDF running and serial queued batch cancellation, all observed native births stopped, explicit new three-mode 400-cell workflows and postprocess. In-flight worker death and prepared same-case cancellation remain.'}
  (api.root/'professional-batch-cancel.json').write_text(json.dumps(report,indent=2),encoding='utf8')
  api.create_case('Independent professional batch cancellation evidence readback');return report
 except BaseException as error:
  jobs=[]
  for v in actors.values():
   try:jobs.extend(v.call('GET','/jobs/'+str(j['id'])) for j in v.call('GET',v.prefix+'/jobs'))
   except Exception as observed:jobs.append({'inspection_error':type(observed).__name__})
  (api.root/'failed-professional-batch-cancel.json').write_text(json.dumps({'type':type(error).__name__,'detail':str(error)[:3000],'jobs':jobs},indent=2),encoding='utf8');raise
 finally:
  primary=sys.exc_info()[0];errors=[]
  try:
   for value in actors.values():
    try:
     for job in value.call('GET',value.prefix+'/jobs'):
      if job['status'] in {'queued','running'} or job['phase'] not in {'succeeded','failed','cancelled','interrupted'} or job['completion_pending']:
       if job['phase'] not in {'succeeded','failed','cancelled','interrupted'}:value.call('POST','/jobs/'+str(job['id'])+':cancel',{})
       def drained_terminal():
        current=value.call('GET','/jobs/'+str(job['id']));attempt=current['attempt'];stop=attempt.get('process_stop')
        return (current['status'] not in {'queued','running'} and attempt['phase'] in {'succeeded','failed','cancelled','interrupted'}
         and (attempt['process_identity'] is None or (stop and stop['verified'])) and not current['completion_pending'])
       wait_for(drained_terminal,'Professional cancel cleanup did not persist terminal stop',180)
    except Exception as error:errors.append({'case_id':value.case_id,'jobs':type(error).__name__})
  finally:
   try:workers.stop()
   except Exception as error:errors.append({'workers':type(error).__name__})
   (api.root/'professional-batch-cancel-cleanup.json').write_text(json.dumps({'errors':errors,'primary_preserved':primary is not None,'workers':workers.receipts},indent=2),encoding='utf8')
  if errors and primary is None:raise RuntimeError('Professional batch cancel cleanup failed; inspect receipt')


async def main():
 root=Path(tempfile.mkdtemp(prefix='professional-batch-cancel-redis-',dir=SERVER.parents[1]/'tmp'));broker=Broker(root);passed=False
 print('PROFESSIONAL_CANCEL_OWNED_REDIS '+str(root),flush=True)
 try:
  await broker.start()
  await live_main(selected_override=partial(scenario,broker=broker,loop=asyncio.get_running_loop()),fixture_override=FIXTURE,
   store_volume_max=16,environment_overrides=broker.settings());passed=True
 finally:
  try:await broker.stop()
  finally:(root/'result.json').write_text(json.dumps({'passed':passed,'mock_llm_calls':0,'actual_Redis_births':broker.births,'actual_Redis_stops':broker.stops},indent=2),encoding='utf8')

if __name__=='__main__':asyncio.run(main())
