"""Actual serial/MPI workflow comparison, restart and downstream confirmation.

No model or native execution response is substituted. The final wrapper calls
the authorized real model twice to read back the observed engineering evidence.
"""
import asyncio,json,math,re,time,uuid
from urllib.parse import quote
from cae_fixtures import cavity_2d
from app.services.casedict import set_block
import live_llm_agent_recovery_acceptance as peers
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_failure_fallback_acceptance import wait_for,kill_owned_api
from live_llm_workflow_plan_migration_acceptance import submit


FIXTURE=r'''
import os
from fastapi import Depends
from sqlalchemy import select
from app.db import get_db
from app.deps import get_current_user
from app.models import Job,JobAttempt,ArtifactPublication
from app.routers.workspaces import get_owned_case
from app.services.artifact_publication import read_manifest
from app.services.job_write_checkpoint import validate

@app.get('/api/v1/testing/workflow-api-owner')
async def workflow_api_owner(user=Depends(get_current_user)):return {'pid':os.getpid()}

@app.get('/api/v1/testing/workflow-publication/{identifier}')
async def workflow_publication(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 publication=await db.get(ArtifactPublication,identifier)
 case=await get_owned_case(publication.case_id,user,db,write=False)
 _,manifest=read_manifest(case.s3_prefix,publication)
 return {'kind':publication.kind,'manifest':manifest}

@app.get('/api/v1/testing/workflow-job-drain/{identifier}')
async def workflow_job_drain(identifier:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,identifier);case=await get_owned_case(job.case_id,user,db,write=False)
 attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==identifier).order_by(JobAttempt.number.desc()).limit(1))
 try:validate(case,job,attempt)
 except (ValueError,TypeError,KeyError,AttributeError):return {'verified':False}
 return {'verified':True}
'''


def peer(api,store,role):
 value=peers.peer(api,store,role)
 with (value.root/'agent_peer_fixture.py').open('a',encoding='utf8') as stream:stream.write(FIXTURE)
 value.token=api.token;value.case_id=api.case_id;value.prefix=api.prefix
 return value


def vectors(raw):
 text=raw.decode('utf8')
 match=re.search(r'internalField\s+nonuniform\s+List<vector>\s+(\d+)\s*\((.*?)\)\s*;',text,re.S)
 assert match,'Actual solver did not write an explicit vector field'
 values=[tuple(float(x) for x in row.split()) for row in re.findall(r'\(([^()]*)\)',match.group(2))]
 assert len(values)==int(match.group(1))==400
 assert all(len(v)==3 and all(math.isfinite(x) and abs(x)<5 for x in v) for v in values)
 return values


def scenario(api,*,store=None,**_):
 run_dir='C-run/case-1';checks=[]
 def record(value):
  checks.append(value)
  (api.root/'mpi-workflow-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
 def waiting(server,identifier,key):
  def observed():
   value=server.call('GET','/workflow-runs/'+identifier)
   if value['status'] in {'failed','interrupted','cancelled'}:raise AssertionError(value)
   step=next(s for s in value['steps'] if s['key']==key)
   return (value,step) if step['status']=='awaiting_confirmation' else None
  return wait_for(observed,'Actual workflow did not reach '+key,240)
 def confirm(server,identifier,key):
  value,step=waiting(server,identifier,key)
  for producer in value['steps']:
   if producer['status']=='succeeded' and producer['attempts']:
    job_id=producer['attempts'][-1]['job_id'];server.wait_job(job_id)
    wait_for(lambda:server.call('GET','/testing/workflow-job-drain/'+str(job_id))['verified'],
     'Prior workflow producer did not finish its actual write-drain receipt')
  result=write(server,'POST',server.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
   'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
  if result.get('workflow_dispatch',{}).get('status')=='pending':
   record({'confirmed_stage':step['confirmation_state'],'dispatch_pending':result['workflow_dispatch']})
   raise AssertionError('Confirmed stage dispatch was deferred: '+json.dumps(result['workflow_dispatch']))
  return value
 api.create_case('Actual canonical MPI workflow bridge')
 source=peer(api,store,'mpi-workflow-source');recovery=peer(api,store,'mpi-workflow-recovery')
 def observe_http(server,label):
  original=server.opener.open
  def observed(request,*args,**kwargs):
   started=time.monotonic();failure=None
   try:return original(request,*args,**kwargs)
   except BaseException as error:failure=type(error).__name__;raise
   finally:
    with (api.root/'mpi-workflow-http.jsonl').open('a',encoding='utf8') as stream:
     stream.write(json.dumps({'role':label,'method':request.get_method(),'path':request.selector,
      'seconds':round(time.monotonic()-started,4),'error':failure})+'\n')
  server.opener.open=observed
 for server,label in [(api,'main'),(source,'source'),(recovery,'recovery')]:observe_http(server,label)
 with source:
  saved=source.call('GET',source.prefix+'/caseDict')
  content=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':
   {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
  content=set_block(content,'numericalMethod',{'solver':'icoFoam'})
  content=set_block(content,'postProcessing',{'runDir':run_dir,
   'fields':{'p':{'unit':'m2/s2'}},
   'dataExtraction':{'volumeAverages':{'meanPressure':{'field':'p','time':.5}}}})
  write(source,'PUT',source.prefix+'/caseDict',{'content':content,'expected_revision':saved['dict_revision']})
  fixture=cavity_2d()
  for path,value in fixture.items():put(source,path,value)
  seeded=put(source,'A-geo/geometry.py','import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n')
  if seeded.get('auto_preview_job'):source.wait_job(seeded['auto_preview_job'])
  revision=source.call('GET',source.prefix+'/caseDict')['dict_revision']
  serial=source.call('POST',source.prefix+'/workflow-runs',{'target':'solve','run_dir':run_dir,'expected_revision':revision,
   'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':120,'max_log_bytes':16777216}})
  confirm(source,serial['id'],'confirm_B');confirm(source,serial['id'],'confirm_C')
  serial_wait,_=waiting(source,serial['id'],'confirm_D')
  serial_step=next(s for s in serial_wait['steps'] if s['key']=='solve')
  serial_job=source.wait_job(serial_step['attempts'][-1]['job_id'])
  serial_field=source.call('GET',source.prefix+'/raw?path='+quote(run_dir+'/0.5/U'),raw=True)
  reference=vectors(serial_field)
  confirm(source,serial['id'],'confirm_D')
  old_history=source.call('GET','/workflow-runs/'+serial['id']);assert old_history['status']=='succeeded'
  controls={path:source.call('GET',source.prefix+'/raw?path='+quote(path),raw=True) for path in fixture if '/system/' in path}
  record({'actual_serial_job':serial_job['id'],'actual_400_cells':True,'serial_last_time':.5,'serial_D_confirmation_explicit':True})

  saved=source.call('GET',source.prefix+'/caseDict')
  request={'target':'postprocess','expected_version':old_history['version'],'expected_revision':saved['dict_revision'],
   'confirmed_reset':True,'parameters':{'geometry_mode':'export','solve_mode':'mpi','subdomains':2},
   'budget':{'timeout_seconds':120,'max_log_bytes':16777216}}
  key=uuid.uuid4().hex
  mpi=submit(source,'/workflow-runs/'+serial['id']+':recompute',request,key)
  assert mpi['recomputation']['source_run_id']==serial['id'] and mpi['recomputation']['rebuild']==['solve','postprocess']
  confirm(source,mpi['id'],'confirm_B');confirm(source,mpi['id'],'confirm_C')
  mpi_wait,confirmation=waiting(source,mpi['id'],'confirm_D')
  producer=next(s for s in mpi_wait['steps'] if s['key']=='solve')
  assert producer['handler']=='solve.openfoam.mpi' and len(producer['attempts'])==1
  job=source.wait_job(producer['attempts'][0]['job_id'],timeout=360)
  publication=job['attempt']['outputs']['publication']
  assert publication['status']=='committed' and publication['kind']=='solve'
  observed=api.call('GET','/testing/workflow-publication/'+publication['id'])
  descriptor=observed['manifest']['descriptor'];native=descriptor['native']['receipt']['native']
  assert descriptor['handler']=='workflow-mpi.v1' and descriptor['subdomains']==2
  assert set(native['processors'])=={'processor0','processor1'} and native['uid']!=0
  assert set(native['steps'])=={'decompose','solve','reconstruct'} and all(s['returncode']==0 for s in native['steps'].values())
  assert descriptor['input_publications'] and descriptor['input_publications'][0]['require_current']
  values=vectors(source.call('GET',source.prefix+'/raw?path='+quote(run_dir+'/0.5/U'),raw=True))
  relative=max(abs(a-b) for v,w in zip(reference,values) for a,b in zip(v,w))/max(1,max(abs(x) for row in reference for x in row))
  assert relative<=1e-5,relative
  for path,original in controls.items():assert source.call('GET',source.prefix+'/raw?path='+quote(path),raw=True)==original
  if run_dir+'/system/decomposeParDict' not in fixture:
   source.call('GET',source.prefix+'/raw?path='+quote(run_dir+'/system/decomposeParDict'),expect=404,raw=True)
  graph=source.call('GET',source.prefix+'/dependencies?run_dir='+run_dir)
  channels={c['kind']:c for c in graph['channels']}
  assert channels['solve']['status']==channels['mesh']['status']=='fresh'
  assert source.call('GET',source.prefix+'/caseDict')['workflow_state']=='C'
  denied=source.call('POST',source.prefix+'/caseDict:advance',{'state':'D','confirmed':False,
   'expected_revision':confirmation['confirmation_revision'],'workflow_step_id':confirmation['id'],
   'expected_step_version':confirmation['version']},expect=409)
  assert denied['detail']['code']=='confirmation_required'
  wait_for(lambda:source.call('GET','/testing/workflow-job-drain/'+str(job['id']))['verified'],'Actual MPI writer did not drain')
  before_jobs=[j['id'] for j in source.call('GET',source.prefix+'/jobs')]
  actual_pid=source.call('GET','/testing/workflow-api-owner')['pid']
  launcher=kill_owned_api(source,actual_pid)
  record({'actual_MPI_job':job['id'],'canonical_publication':publication['id'],'actual_two_nonroot_ranks':True,
   'actual_decompose_solve_reconstruct':True,'serial_MPI_velocity_relative_error':relative,
   'private_decomposition_not_written_to_live_controls':True,'mesh_and_solve_remain_fresh':True,
   'D_confirmation_explicit':True,'actual_API_pid_killed':actual_pid,'verified_launcher_pid':launcher})

 with recovery:
  assert recovery.call('GET','/workflow-runs/'+mpi['id'])==mpi_wait
  assert [j['id'] for j in recovery.call('GET',recovery.prefix+'/jobs')]==before_jobs
  confirm(recovery,mpi['id'],'confirm_D')
  post_wait,_=waiting(recovery,mpi['id'],'confirm_E')
  post=next(s for s in post_wait['steps'] if s['key']=='postprocess')
  post_job=recovery.wait_job(post['attempts'][-1]['job_id'])
  post_pub=post_job['attempt']['outputs']['publication'];assert post_pub['status']=='committed'
  post_manifest=api.call('GET','/testing/workflow-publication/'+post_pub['id'])['manifest']
  assert post_manifest['descriptor']['input_publications'][0]['publication_id']==publication['id']
  metrics=json.loads(recovery.call('GET',recovery.prefix+'/raw?path='+quote('D-post/data/postprocess-'+str(post_job['id'])+'/metrics.json'),raw=True))
  mean=next(row['value'] for row in metrics['metrics'] if row['name']=='meanPressure.p@0.5')
  assert math.isfinite(mean) and metrics['readiness']['report_ready']
  confirm(recovery,mpi['id'],'confirm_E')
  completed=recovery.call('GET','/workflow-runs/'+mpi['id']);assert completed['status']=='succeeded'
  assert recovery.call('GET','/workflow-runs/'+serial['id'])==old_history
  assert submit(recovery,'/workflow-runs/'+serial['id']+':recompute',request,key)['id']==mpi['id']
  record({'actual_registered_postprocess_job':post_job['id'],'postprocess_pins_MPI_publication':True,
   'actual_mean_pressure':mean,'actual_extraction_readiness_passed':True,
   'restart_preserved_completed_MPI_without_rerun':True,'explicit_D_and_E_confirmation':True,
   'original_serial_workflow_unchanged':True,'same_key_replayed_same_workflow':True})
  # A different process count requires a new solve, even if the old MPI output is fresh.
  dictionary=recovery.call('GET',recovery.prefix+'/caseDict')
  changed={'target':'solve','expected_version':completed['version'],'expected_revision':dictionary['dict_revision'],
   'confirmed_reset':True,'parameters':{'geometry_mode':'export','solve_mode':'mpi','subdomains':4}}
  replanned=submit(recovery,'/workflow-runs/'+mpi['id']+':recompute',changed,uuid.uuid4().hex)
  assert replanned['recomputation']['rebuild']==['solve'] and set(replanned['recomputation']['reused'])=={'geometry','mesh'}
  cancel=recovery.call('POST','/workflow-runs/'+replanned['id']+':cancel',{'expected_version':replanned['version']})
  assert cancel['status']=='cancelled'
  assert not next(s for s in cancel['steps'] if s['key']=='solve')['attempts']
  record({'changed_subdomains_requires_new_solve':True,'four_rank_plan_cancelled_before_execution':True,
   'scope':'This last check verifies planning/cancellation, not an actual four-rank solve'})
 assert source.proc.poll() is not None and recovery.proc.poll() is not None
 report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,
  'scope':'N3-07 MPI workflow producer, confirmation, downstream extraction and restart; UDF, batches and full cancellation/recovery matrix remain pending'}
 (api.root/'mpi-workflow.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent actual MPI workflow evidence readback')
 return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
