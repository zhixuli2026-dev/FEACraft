"""Actual legacy argv failure, current CadQuery export and real model readback.

No LLM/native reply is fabricated. Private routes create the known v1 graph
through its original scheduler and alter only owned evidence for refusal tests.
"""
import asyncio,hashlib,json,time,uuid
from urllib.parse import quote
from casedict_fallback_capacity_acceptance import read_settled
from live_llm_job_repeat_acceptance import main,put,write

FIXTURE=r'''
import sys,uuid,hashlib
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.deps import get_current_user
from app.models import User,Job,JobAttempt,WorkflowRun,WorkflowStep
from app.routers.workspaces import get_owned_case
from app.services import workflow_scheduler as migration_scheduler,job_delivery
from app.services.case_write_capacity import committing
from app.services.workflow_execution import owned
migration_faults={}

@app.post('/api/v1/cases/{case_id}/testing/legacy-workflow')
async def migration_legacy(case_id:int,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=True)
 async with committing(db,case,content_write=False):
  run,_=await migration_scheduler.create_run(db,case_id=case.id,user=user,target='mesh',run_dir='C-run/case-1',
   expected_revision=case.dict_revision,idempotency_key=uuid.uuid4().hex,timeout_seconds=300)
  steps=await migration_scheduler.reconcile(db,run.id)
  steps=await migration_scheduler.complete_preflight(db,run.id,expected_version=steps[0].version)
  step=next(row for row in steps if row.step_key=='mesh')
  spec={'command':'Historical fixed argv','case_prefix':case.s3_prefix,'runtime':'windows',
   'argv':[sys.executable,'Ref/legacy.py'],'evidence_run_dir':None,'timeout_seconds':300,'max_log_bytes':16777216}
  job,_=await migration_scheduler.queue_step(db,run.id,step.id,expected_version=step.version,spec=spec,
   input_paths=['Ref/legacy.py','0-caseDict/caseDict'])
  await job_delivery.request(db,job,user_id=user.id,mode='local')
  identifier=job.id;run_id=run.id
 await job_delivery.deliver(identifier)
 return {'job_id':identifier,'run_id':run_id}

@app.get('/api/v1/workflow-runs/{run_id}/testing/history')
async def migration_history(run_id:str,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
 run=await owned(db,run_id,user,write=False)
 rows=[run,*list(await db.scalars(select(WorkflowStep).where(WorkflowStep.run_id==run.id).order_by(WorkflowStep.step_key)))]
 attempts=list(await db.scalars(select(JobAttempt).join(WorkflowStep,WorkflowStep.id==JobAttempt.step_id)
  .where(WorkflowStep.run_id==run.id).order_by(JobAttempt.id)))
 rows+=attempts
 for attempt in attempts:rows.append(await db.get(Job,attempt.job_id))
 value=[{col.name:str(getattr(row,col.name)) for col in row.__table__.columns} for row in rows]
 return {'raw_history_sha256':hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest(),
  'raw_plan_sha256':hashlib.sha256(run.plan_json.encode()).hexdigest()}

@app.post('/api/v1/workflow-runs/{run_id}/testing/evidence-fault')
async def migration_fault(run_id:str,body:dict,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
 run=await owned(db,run_id,user)
 case=await get_owned_case(run.case_id,user,db,write=True)
 async with committing(db,case,content_write=False):
  if body['mode']=='restore':
   identifier,kind,column,old,updated=migration_faults.pop(body['receipt'])
   row=await db.get({'run':WorkflowRun,'step':WorkflowStep,'attempt':JobAttempt}[kind],identifier)
   setattr(row,column,old);row.updated_at=updated
   return {'restored':True}
  mode=body['mode'];assert mode in {'plan','version','dependencies','ownership','drain'}
  if mode in {'plan','version'}:
   row=run;kind='run';column='plan_json' if mode=='plan' else 'version'
   value=json.dumps({'schema':'unknown-custom-plan'}) if mode=='plan' else run.version+1
  else:
   step=await db.scalar(select(WorkflowStep).where(WorkflowStep.run_id==run.id,WorkflowStep.step_key=='mesh'))
   row=step;kind='step'
   if mode=='dependencies':column='dependencies_json';value='{broken'
   elif mode=='ownership':column='active_attempt_id';value=body['attempt_id']
   else:
    row=await db.get(JobAttempt,step.active_attempt_id);kind='attempt';column='write_checkpoint_json';value='{}'
  receipt=uuid.uuid4().hex;migration_faults[receipt]=(row.id,kind,column,getattr(row,column),row.updated_at);setattr(row,column,value)
 return {'receipt':receipt,'scope':'owned protocol evidence mutation; no native/model result substituted'}
'''


def wait_for(check,description,seconds=60):
 deadline=time.monotonic()+seconds
 while True:
  value=check()
  if value:return value
  if time.monotonic()>deadline:raise TimeoutError(description)
  time.sleep(.1)


def submit(api,endpoint,body,key,expect=200):
 deadline=time.monotonic()+30
 while True:
  result=api.call('POST',endpoint,body,expect=(expect,409),extra_headers={'Idempotency-Key':key})
  detail=result.get('detail')
  if isinstance(detail,dict) and detail.get('code') in {'storage_quota_busy','validation_busy'}:
   if time.monotonic()>deadline:raise TimeoutError('Migration write admission did not drain')
   time.sleep(.1);continue
  if expect==200:assert 'id' in result,result
  return result


def scenario(api,*,store=None,**_):
 api.create_case('Actual legacy workflow migration')
 checks=[]
 def save():
  (api.root/'workflow-plan-migration-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
 def geometry(size):
  return 'import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box('+','.join([str(size)]*3)+')}\n'
 saved=put(api,'A-geo/geometry.py',geometry(1))
 preview_job=api.wait_job(saved['auto_preview_job']) if saved.get('auto_preview_job') else api.wait_job(write(api,'POST',api.prefix+'/geometry:preview',{})['job_id'])
 assert preview_job['attempt']['outputs']['publication']['status']=='committed'
 api.advance('B')
 executions=api.root/'actual-migration-old-executions';started=api.root/'actual-migration-old-started';release=api.root/'actual-migration-old-release'
 old_code='from pathlib import Path\nimport time\nwith Path('+repr(str(executions))+').open("a") as f:f.write("once\\n")\nPath('+repr(str(started))+').touch()\nwhile not Path('+repr(str(release))+').exists():time.sleep(.05)\nraise SystemExit(23)\n'
 put(api,'Ref/legacy.py',old_code)
 legacy=write(api,'POST',api.prefix+'/testing/legacy-workflow',{})
 wait_for(started.exists,'Actual historical argv did not start')
 old_id=legacy['run_id'];route='/workflow-runs/'+old_id;endpoint=route+':migrate'
 denied=api.call('GET',route+'/migration',expect=409);assert denied['detail']['code']=='state_conflict',denied
 current=api.call('GET',route)
 denied=api.call('POST',route+':dispatch',{'expected_version':current['version']},expect=409)
 assert denied['detail']['code']=='recovery_required',denied
 release.touch();failed=api.wait_job(legacy['job_id'],expected='failed')
 assert failed['exit_code']==23 and failed['attempt']['process_stop']['verified']
 def terminal():
  value=api.call('GET',route)
  return value if value['status']=='failed' else None
 old_flow=wait_for(terminal,'Failed historical workflow did not reconcile')
 # A completed Job can still be settling its audited write checkpoint.
 wait_for(lambda:api.call('GET','/jobs/'+str(legacy['job_id'])+'/operations')['reprepare']['available'],
  'Actual old writer did not drain')
 history=api.call('GET',route+'/testing/history')
 assert all(step['status'] in {'succeeded','failed','blocked'} for step in old_flow['steps'])
 assert read_settled(api,api.prefix+'/caseDict')['workflow_state']=='B'
 checks.append({'actual_legacy_job':legacy['job_id'],'actual_exit_code':23,'old_run':old_id,
  'running_migration_refused':True,'legacy_public_dispatch_refused':True,'physical_stop_verified':True});save()
 def proposed():
  plan=api.call('GET',route+'/migration');assert plan['required']
  return plan,{'expected_version':plan['source_run_version'],'expected_revision':plan['current_revision'],
   'expected_event_sequence':plan['current_event_sequence'],'expected_source_plan_sha256':plan['source_plan_sha256'],
   'confirmed_reset':True,'target':'geometry','run_dir':'C-run/case-1','parameters':{'geometry_mode':'export'},
   'budget':{'timeout_seconds':120,'max_log_bytes':16777216}}
 plan,body=proposed();assert plan['source_plan_sha256']==history['raw_plan_sha256']
 for tag,changed,codes in (
  ('confirmation',dict(body,confirmed_reset=False),{'confirmation_required'}),
  ('version',dict(body,expected_version=body['expected_version']+1),{'state_conflict'}),
  ('plan digest',dict(body,expected_source_plan_sha256='0'*64),{'state_conflict'}),
  ('event',dict(body,expected_event_sequence=body['expected_event_sequence']+1),{'input_stale'}),
  ('revision',dict(body,expected_revision=body['expected_revision']+1),{'revision_conflict'})):
  denied=submit(api,endpoint,changed,uuid.uuid4().hex,expect=409)
  assert denied['detail']['code'] in codes,(tag,denied)
 api.call('POST',endpoint,body,expect=422)
 api.call('POST',endpoint,dict(body,command='arbitrary'),expect=422,extra_headers={'Idempotency-Key':uuid.uuid4().hex})
 owner=api.token
 other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'Other owner','password':'isolated-test-123'})
 try:
  api.token=other['token'];api.call('GET',route+'/migration',expect=404)
  api.call('POST',endpoint,body,expect=404,extra_headers={'Idempotency-Key':uuid.uuid4().hex})
 finally:api.token=owner
 assert len(api.call('GET',api.prefix+'/workflow-runs'))==1
 checks.append({'confirmation_version_plan_revision_event_key_shape_and_ownership_refused':True});save()
 for mode,codes in (('plan',{'recovery_required'}),('dependencies',{'evidence_invalid'}),
  ('ownership',{'evidence_invalid'}),('drain',{'recovery_required'})):
  fault=write(api,'POST',route+'/testing/evidence-fault',{'mode':mode,'attempt_id':preview_job['attempt']['id']})
  try:
   denied=submit(api,endpoint,body,uuid.uuid4().hex,expect=409)
   assert denied['detail']['code'] in codes,(mode,denied)
  finally:write(api,'POST',route+'/testing/evidence-fault',{'mode':'restore','receipt':fault['receipt']})
 restored=api.call('GET',route+'/testing/history')
 assert restored==history,{'before':history,'after':restored}
 checks.append({'owned_evidence_faults':['unknown_plan','corrupt_dependencies','wrong_attempt_owner','missing_drain'],
  'history_restored_exactly':True,'scope':'protocol faults; real inference and native execution are not substituted'});save()
 # Live bytes now differ from the historical argv. Migration must not replay it.
 put(api,'Ref/legacy.py','raise RuntimeError("OBSOLETE_ARGV_MUST_NEVER_REPLAY")\n')
 new_source=geometry(2);saved=put(api,'A-geo/geometry.py',new_source)
 if saved.get('auto_preview_job'):api.wait_job(saved['auto_preview_job'])
 plan,body=proposed();assert plan['current_stage']=='B'
 key=uuid.uuid4().hex;new=submit(api,endpoint,body,key)
 assert new['id']!=old_id and new['legacy_migration']['source_run_id']==old_id
 assert new['legacy_migration_reset']['from_state']=='B' and new['legacy_migration_reset']['to_state']=='A'
 def awaiting():
  value=api.call('GET','/workflow-runs/'+new['id'])
  if value['status'] in {'failed','interrupted'}:raise AssertionError('Migrated workflow failed: '+json.dumps(value))
  return value if value['status']=='awaiting_confirmation' else None
 waiting=wait_for(awaiting,'Actual migrated geometry did not await B confirmation',180)
 assert read_settled(api,api.prefix+'/caseDict')['workflow_state']=='A'
 step=next(row for row in waiting['steps'] if row['key']=='geometry')
 assert len(step['attempts'])==1
 actual=api.wait_job(step['attempts'][0]['job_id'])
 assert actual['attempt']['handler']['key']=='geometry.export'
 assert actual['attempt']['workspace']['files']['A-geo/geometry.py']['sha256']==hashlib.sha256(new_source.encode()).hexdigest()
 assert actual['attempt']['outputs']['publication']['status']=='committed'
 raw=api.call('GET',api.prefix+'/raw?path='+quote('A-geo/exports/box.step'),raw=True)
 path=api.root/'migrated-current-box.step';path.write_bytes(raw)
 import cadquery as cq
 volume=cq.importers.importStep(str(path)).val().Volume();assert abs(volume-8)<1e-9
 assert submit(api,endpoint,body,key)['id']==new['id']
 denied=submit(api,endpoint,dict(body,target='mesh'),key,expect=409)
 assert denied['detail']['code']=='idempotency_conflict'
 assert api.call('GET',route)==old_flow and api.call('GET',route+'/testing/history')==history
 assert executions.read_text().splitlines()==['once']
 checks.append({'new_run':new['id'],'actual_registered_cadquery_job':actual['id'],'actual_step_volume':volume,
  'current_inputs_used':True,'explicit_A_reset':True,'awaits_B_confirmation':True,
  'same_key_same_run':True,'changed_same_key_refused':True,'legacy_argv_executions':1,'legacy_history_unchanged':True});save()
 fault=write(api,'POST',route+'/testing/evidence-fault',{'mode':'version'})
 try:
  denied=api.call('POST','/workflow-runs/'+new['id']+':dispatch',{'expected_version':waiting['version']},expect=409)
  assert denied['detail']['code']=='input_stale',denied
 finally:write(api,'POST',route+'/testing/evidence-fault',{'mode':'restore','receipt':fault['receipt']})
 confirmation=next(row for row in waiting['steps'] if row['key']=='confirm_B')
 write(api,'POST',api.prefix+'/caseDict:advance',{'state':'B','confirmed':True,
  'expected_revision':confirmation['confirmation_revision'],'workflow_step_id':confirmation['id'],
  'expected_step_version':confirmation['version']})
 completed=api.call('GET','/workflow-runs/'+new['id']);assert completed['status']=='succeeded'
 revision=read_settled(api,api.prefix+'/caseDict')['dict_revision']
 assert submit(api,endpoint,body,key)['id']==completed['id']
 assert read_settled(api,api.prefix+'/caseDict')['dict_revision']==revision
 assert len(api.call('GET',api.prefix+'/workflow-runs'))==2 and api.call('GET',route+'/testing/history')==history
 checks.append({'B_confirmation_explicit':True,'completed':True,'completed_replay_no_stage_or_job_repeat':True,
  'source_binding_refuses_later_history_version_change':True});save()
 report={'passed':True,'backend':'s3' if store else 'fs','checks':checks,'mock_llm_calls':0,
  'model_selects_operations':False,'scope':'N5-06 legacy v1 migration; failure rollback and Agent recovery remain unverified'}
 (api.root/'workflow-plan-migration.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent actual migration evidence model readback')
 return report


if __name__=='__main__':
 from s3testsupport import ObjectStore
 from testsupport import SERVER
 runtime=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
 asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,
  store_factory=lambda:ObjectStore(executable=runtime,volume_max=16,authenticated=True)))
