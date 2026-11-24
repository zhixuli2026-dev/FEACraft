"""Real CCX/OpenFOAM, owned SQL corruption and actual executable change.

No model/CLI inference or native result is substituted. SQL corruptions are
explicit fault fixtures on real isolated records and are restored per case.
"""
import asyncio,hashlib,json,time,uuid
from pathlib import Path
from live_llm_job_repeat_acceptance import main,put,write,repeat
from live_llm_solver_repeat_acceptance import FIXTURE as SOLVER_FIXTURE,fail,literal,displacement
from live_llm_post_report_repeat_acceptance import FIXTURE as POST_FIXTURE,seed_thermal,RUN,fail as post_fail
from live_llm_engineering_repeat_acceptance import case_root,retry_job,rerun_job,frozen_retry

FAULTS={
 'handler_unknown':'recovery_required','handler_version':'recovery_required',
 'workspace_case':'recovery_required','workspace_attempt':'recovery_required','workspace_job':'recovery_required',
 **{key:'evidence_invalid' for key in ('handler_missing','handler_wrong','execution_hash','execution_spec',
  'submission_spec','submission_hash','submission_binding','submission_case','job_type','job_command',
  'publication_missing','publication_attempt','publication_case','publication_kind','publication_schema',
  'publication_descriptor','toolchain_declaration','toolchain_descriptor')},
}
FIXTURE=SOLVER_FIXTURE+POST_FIXTURE+r'''
import hashlib,os,shutil
from sqlalchemy import select
from fastapi import Depends,HTTPException
from app.db import get_db
from app.deps import get_current_user
from app.models import Job,JobAttempt,JobSubmissionRequest,ArtifactPublication,Case
from app.routers.workspaces import get_owned_case
from app.services.case_write_capacity import committing
from app.services.durable_events import json_text
from app.services.calculix import calculix_executable
integrity_root=Path(__file__).parent
integrity_installed=Path(calculix_executable()).resolve()
integrity_bin=integrity_root/'owned-calculix';integrity_bin.mkdir()
integrity_tool=integrity_bin/'ccx_static.exe';shutil.copyfile(integrity_installed,integrity_tool)
assert hashlib.sha256(integrity_tool.read_bytes()).digest()==hashlib.sha256(integrity_installed.read_bytes()).digest()
os.environ['VIBEFLOW_CALCULIX']=str(integrity_tool)
integrity_fault={}

@app.post('/api/v1/testing/repeat-integrity/{job_id}')
async def integrity_mutate(job_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id)
 if job is None:raise HTTPException(404)
 case=await get_owned_case(job.case_id,user,db,write=True)
 async with committing(db,case,content_write=False):
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
  request=await db.scalar(select(JobSubmissionRequest).where(JobSubmissionRequest.job_id==job.id))
  assert job.status=='failed' and attempt.phase=='failed'
  if body['fault']=='restore':
   assert integrity_fault['job_id']==job.id
   publication=await db.get(ArtifactPublication,integrity_fault['publication_id'])
   for name,target in [('job',job),('attempt',attempt),('request',request),('publication',publication)]:
    for key,value in integrity_fault[name].items():setattr(target,key,value)
   integrity_fault.clear();await db.flush()
   return {'restored':True}
  assert not integrity_fault
  document=json.loads(attempt.input_manifest_json);reservation=json.loads(attempt.publication_json)
  publication=await db.get(ArtifactPublication,reservation['publication_id'])
  other=await db.scalar(select(Case.id).where(Case.id!=case.id).order_by(Case.id).limit(1))
  donor=await db.get(JobAttempt,body['donor_attempt_id']);assert donor is not None and donor.job_id!=job.id
  assert (await db.get(Job,donor.job_id)).case_id==case.id
  fields={'job':('type','command'),'attempt':('input_manifest_json','publication_json'),
   'request':('execution_spec_json','request_hash','case_id'),'publication':('attempt_id','case_id','kind')}
  integrity_fault.update(job_id=job.id,publication_id=publication.id,
   **{name:{key:getattr(target,key) for key in fields[name]} for name,target in
    [('job',job),('attempt',attempt),('request',request),('publication',publication)]})
  label=body['fault']
  if label=='handler_unknown':document['handler']['key']='unknown.professional'
  elif label=='handler_version':document['handler']['version']='unknown'
  elif label=='handler_missing':document.pop('handler')
  elif label=='handler_wrong':document['handler']['key']='su2.solve'
  elif label=='execution_hash':document['handler']['execution_sha256']='0'*64
  elif label=='execution_spec':document['execution_spec']['timeout_seconds']+=1
  elif label=='submission_spec':
   changed=json.loads(request.execution_spec_json);changed['timeout_seconds']+=1;request.execution_spec_json=json_text(changed)
  elif label=='submission_hash':request.request_hash='0'*64
  elif label=='submission_binding':document['submission_request_hash']='0'*64
  elif label=='submission_case':request.case_id=other
  elif label=='job_type':job.type='mesh'
  elif label=='job_command':job.command='Different durable command'
  elif label=='workspace_case':document['workspace']['source']['case_id']=other
  elif label=='workspace_attempt':document['workspace']['attempt_id']=donor.id
  elif label=='workspace_job':document['workspace']['source']['job_id']=donor.job_id
  elif label=='publication_missing':reservation['publication_id']=str(uuid.uuid4())
  elif label=='publication_attempt':publication.attempt_id=donor.id
  elif label=='publication_case':publication.case_id=other
  elif label=='publication_kind':publication.kind='mesh'
  elif label=='publication_schema':reservation['schema']='unknown.publication'
  elif label=='publication_descriptor':reservation['descriptor']['handler']='su2.v1'
  elif label=='toolchain_declaration':document['handler']['toolchain']['execution_sha256']='0'*64
  elif label=='toolchain_descriptor':reservation['descriptor']['executable_sha256']='0'*64
  elif label=='legacy_no_submission_binding':document.pop('submission_request_hash')
  else:raise AssertionError('Unsupported owned integrity fault')
  attempt.input_manifest_json=json_text(document);attempt.publication_json=json_text(reservation)
  await db.flush()
 return {'injected':label,'owned_database_only':True}
'''

def failures(api,source,failed,donor):
 option=api.call('GET','/jobs/'+str(source)+'/operations')
 body={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
 assert option['retry']['available'] and not option['resume']['available'] and option['resume']['reason']['code']=='checkpoint_required'
 before={row['id'] for row in api.call('GET',api.prefix+'/jobs')};checks=[]
 for label,expected in FAULTS.items():
  write(api,'POST','/testing/repeat-integrity/'+str(source),{'fault':label,'donor_attempt_id':donor['attempt']['id']})
  try:
   options=api.call('GET','/jobs/'+str(source)+'/operations')
   assert not any(options[key]['available'] for key in ('retry','rerun','resume','reprepare')),(label,options)
   assert options['retry']['reason']['code']==expected,(label,options)
   denied=repeat(api,source,body,uuid.uuid4().hex,expect=409)
   assert denied['detail']['code']==expected,(label,denied)
   checks.append({'fault':label,'code':expected,'operation_unavailable':True,'repeat_refused':True})
  finally:write(api,'POST','/testing/repeat-integrity/'+str(source),{'fault':'restore'})
  assert {row['id'] for row in api.call('GET',api.prefix+'/jobs')}==before
  restored=api.call('GET','/jobs/'+str(source)+'/operations');assert restored['retry']['available']
  (api.root/'repeat-integrity-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
 write(api,'POST','/testing/repeat-integrity/'+str(source),{'fault':'legacy_no_submission_binding','donor_attempt_id':donor['attempt']['id']})
 try:assert api.call('GET','/jobs/'+str(source)+'/operations')['retry']['available']
 finally:write(api,'POST','/testing/repeat-integrity/'+str(source),{'fault':'restore'})
 return checks

def confirmed_rerun(api,identifier,parameters):
 """Reconfirm current guard after an explicit rejection in this owned case."""
 deadline=time.monotonic()+30;refusals=[]
 while True:
  option=api.call('GET','/jobs/'+str(identifier)+'/operations')
  body={'action':'rerun','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version'],
   'expected_revision':option['expected_revision'],'expected_event_sequence':option['expected_event_sequence'],'parameters':parameters}
  key=uuid.uuid4().hex
  while True:
   result=api.call('POST','/jobs/'+str(identifier)+':repeat',body,expect=(202,409),extra_headers={'Idempotency-Key':key})
   if 'id' in result:
    done=api.wait_job(result['id'],timeout=360)
    assert done['attempt']['number']==1 and done['attempt']['root_job_id']==done['id']
    assert done['attempt']['outputs']['publication']['status']=='committed'
    (api.root/'integrity-rerun-reconfirmations.json').write_text(json.dumps(refusals,indent=2),encoding='utf8')
    return done
   code=result['detail']['code']
   assert code in {'input_stale','storage_quota_busy','validation_busy'},result
   refusals.append({'job_id':identifier,'guard':{key:body[key] for key in ('expected_revision','expected_event_sequence')},
    'code':code,'same_key_preserved_for_busy':code!='input_stale','new_guard_read_after_stale':code=='input_stale'})
   if time.monotonic()>deadline:raise TimeoutError('Current rerun guard did not settle')
   time.sleep(.05)
   if code=='input_stale':break

def scenario(api,*,store=None,**_):
 api.create_case('Actual solver repeat integrity')
 deck=literal('calculix_smoke.py','DECK');path='C-run/calculix/beam.inp';resource='Ref/beam.inc'
 put(api,resource,deck);put(api,path,'*INCLUDE, INPUT=Ref/beam.inc\n')
 first,failed=fail(api,'calculix.solve','/calculix:solve',{'input_path':path})
 second,donor=fail(api,'calculix.solve','/calculix:solve',{'input_path':path})
 root=case_root(api,failed,store);frozen=root/failed['attempt']['workspace']['run_path']
 original_values,_=displacement((frozen/'C-run/calculix/beam.frd').read_bytes())
 assert all(-.02<original_values[n][2]<-.01 for n in range(5,9))
 checks=failures(api,first,failed,donor)
 done,body,key=retry_job(api,first);frozen_retry(failed,done,'calculix.solve')
 publication=done['attempt']['outputs']['publication']['id'];old_bundle=root/'.vibeflow/publications'/publication/'files/C-run/calculix/beam.frd'
 historical=old_bundle.read_bytes()
 retired=api.call('GET','/jobs/'+str(first)+'/operations')
 assert not retired['retry']['available'] and retired['retry']['reason']['code']=='state_conflict'
 assert repeat(api,first,body,key)['id']==done['id']
 denied=repeat(api,first,body,uuid.uuid4().hex,expect=409);assert denied['detail']['code']=='state_conflict'
 option=api.call('GET','/jobs/'+str(second)+'/operations')
 retry={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
 put(api,resource,deck+'\n** ACTUAL_CURRENT_INCLUDE_CHANGE\n')
 try:
  denied=repeat(api,second,retry,uuid.uuid4().hex,expect=409);assert denied['detail']['code']=='input_stale'
 finally:put(api,resource,deck)
 denied=repeat(api,second,{**retry,'action':'resume','parameters':{'time':'1'}},uuid.uuid4().hex,expect=409)
 assert denied['detail']['code']=='checkpoint_required'
 tool=api.root/'owned-calculix/ccx_static.exe';original_tool=tool.read_bytes();old_hash=hashlib.sha256(original_tool).hexdigest()
 # Modify only the exclusively owned copy. A PE overlay preserves the real
 # program; the old declared checksum must still block its native child.
 tool.write_bytes(original_tool+b'\nVIBEFLOW_OWNED_EXECUTABLE_REVISION\n');new_hash=hashlib.sha256(tool.read_bytes()).hexdigest()
 assert old_hash!=new_hash
 changed=repeat(api,second,retry,uuid.uuid4().hex)
 refused=api.wait_job(changed['id'],expected='failed');changed_root=case_root(api,refused,store)/refused['attempt']['workspace']['run_path']
 assert 'CalculiX executable changed before launch' in refused['log_backlog']
 assert refused['attempt']['child_processes']['children']=={}
 assert not (changed_root/'.vibeflow/calculix/solver.log').exists() and not (changed_root/'C-run/calculix/beam.frd').exists()
 fresh=confirmed_rerun(api,second,{'input_path':path});fresh_root=case_root(api,fresh,store)/fresh['attempt']['workspace']['run_path']
 values,_=displacement((fresh_root/'C-run/calculix/beam.frd').read_bytes())
 assert max(abs(values[n][j]-original_values[n][j]) for n in values for j in range(3))<1e-12
 receipt=json.loads((fresh_root/'.vibeflow/calculix/receipt.json').read_text(encoding='utf8'))
 assert receipt['executable_sha256']==new_hash and old_bundle.read_bytes()==historical
 assert hashlib.sha256(Path(__file__).resolve().parents[2].joinpath('third_party/calculix-2.23-bin/calculix_2.23_4win/ccx_static.exe').read_bytes()).hexdigest()==old_hash
 tool_result={'actual_owned_executable_changed':True,'old_sha256':old_hash,'current_sha256':new_hash,
  'retry_allocated_but_native_child_refused_before_launch':True,'refused_retry_job':refused['id'],
  'explicit_current_rerun_uses_changed_real_executable':True,'rerun_job':fresh['id'],
  'actual_displacement_unchanged':True,'installed_tool_unmodified':True,'old_publication_preserved':True}
 api.create_case('Actual upstream publication replacement refusal');solved=seed_thermal(api)
 first_post,post_failed,_=post_fail(api,'postprocess.run','/postprocess:run',{'run_dir':RUN})
 old_refs=post_failed['attempt']['workspace']['publications'];old_mesh=next(row for row in old_refs if row['kind']=='mesh')
 mesh=next(row for row in api.call('GET',api.prefix+'/jobs') if row.get('attempt',{}).get('id')==old_mesh['attempt_id'])
 replacement=confirmed_rerun(api,mesh['id'],{'run_dir':RUN})
 assert replacement['attempt']['outputs']['publication']['id']!=old_mesh['publication_id']
 option=api.call('GET','/jobs/'+str(first_post)+'/operations')
 body={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
 before={row['id'] for row in api.call('GET',api.prefix+'/jobs')}
 denied=repeat(api,first_post,body,uuid.uuid4().hex,expect=409);assert denied['detail']['code']=='input_stale'
 assert {row['id'] for row in api.call('GET',api.prefix+'/jobs')}==before
 assert api.call('GET','/jobs/'+str(first_post))['status']=='failed'
 report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','database_fault_checks':checks,
  'actual_failed_native_ccx_sources':2,'positive_frozen_retry_job':done['id'],'retired_head_new_key_refused':True,
  'committed_key_replay_preserved':True,'actual_current_include_change_refused':True,
  'calculix_resume_checkpoint_required':True,'legacy_missing_submission_binding_compatibility_protocol':True,
  'actual_toolchain_change':tool_result,'actual_upstream_mesh_replaced':True,'old_postprocess_retry_refused':True,
  'new_mesh_job':replacement['id'],'original_postprocess_failure_preserved':True,
  'model_selects_operations':False,'fault_scope':'owned SQL protocol faults on real native attempts; real native and real-model evidence separate'}
 (api.root/'repeat-integrity.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent integrity evidence model readback');return report

if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
