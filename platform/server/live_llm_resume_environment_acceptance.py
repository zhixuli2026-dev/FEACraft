"""Actual cancelled OpenFOAM checkpoint, runtime/solver rejection and readback."""
import asyncio,copy,json,re
from functools import partial
from pathlib import Path
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_solve_resume_acceptance import scenario,TIMING_FIXTURE
from app.services.solve_resume_artifacts import require_toolchain

FIXTURE=TIMING_FIXTURE+r'''
from app.services import runtime_config as checkpoint_runtime
checkpoint_runtime_original=None
@app.post('/api/v1/testing/checkpoint-runtime/{job_id}')
async def checkpoint_runtime_change(job_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 global checkpoint_runtime_original
 job=await db.get(Job,job_id)
 if job is None:raise HTTPException(404)
 await get_owned_case(job.case_id,user,db,write=True)
 attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
 assert job.status=='cancelled' and attempt.phase=='cancelled'
 config=checkpoint_runtime._CONFIG_PATH.resolve();assert config.parent==Path(__file__).parent.resolve()
 if body['action']=='change':
  assert checkpoint_runtime_original is None
  checkpoint_runtime_original=(config.exists(),config.read_bytes() if config.exists() else b'')
  values=checkpoint_runtime.update_runtime_settings({'wsl_distro':'openfoam2406-default'})
  assert values['wsl_distro']=='openfoam2406-default'
  return {'actual_configured_distro':'openfoam2406-default','owned_runtime_config':True}
 assert body['action']=='restore' and checkpoint_runtime_original is not None
 existed,data=checkpoint_runtime_original
 if existed:config.write_bytes(data)
 else:config.unlink()
 checkpoint_runtime_original=None
 return {'restored':True}
'''

def negative_checks(api,work,body,refused):
 manifest=json.loads((work.parent/'manifest.json').read_text(encoding='utf8'))
 identifier=manifest['source']['job_id']
 original=json.loads((work/'C-run/case-1/solve-invocation.json').read_text(encoding='utf8'))['toolchain']
 assert original['distro']=='vibeflow-linux' and original['solver']=='icoFoam'
 write(api,'POST','/testing/checkpoint-runtime/'+str(identifier),{'action':'change'})
 try:
  denied=refused(body);assert denied['detail']['code']=='runtime_unavailable',denied
 finally:write(api,'POST','/testing/checkpoint-runtime/'+str(identifier),{'action':'restore'})
 jobs_before={job['id'] for job in api.call('GET',api.prefix+'/jobs')}
 input_rejections={}
 for name,relative,pattern,replacement in (
     ('solver','system/controlDict',r'\bapplication\s+icoFoam\s*;','application pisoFoam;'),
     ('mesh','system/blockMeshDict',r'\(20 20 1\)','(24 20 1)'),
     ('initial_field','0/U',r'uniform \(1 0 0\)','uniform (2 0 0)')):
  path='C-run/case-1/'+relative
  current=api.call('GET',api.prefix+'/raw?path='+path,raw=True).decode('utf8')
  changed,count=re.subn(pattern,replacement,current)
  assert count==1,relative
  put(api,path,changed)
  try:
   rejection=refused(body)
   assert rejection['detail']['code']=='input_stale' and path in rejection['detail']['message'],rejection
   input_rejections[name]=rejection
  finally:put(api,path,current)
 assert {job['id'] for job in api.call('GET',api.prefix+'/jobs')}==jobs_before
 solver=input_rejections['solver']
 fields=['solver','executable','executable_sha256','openfoam','distro','python','worker_sha256','runner_sha256']
 for field in fields:
  for missing in (False,True):
   actual=copy.deepcopy(original)
   if missing:actual.pop(field)
   else:actual[field]='changed-'+str(actual[field])
   try:require_toolchain(actual,original)
   except ValueError:pass
   else:raise AssertionError('Checkpoint toolchain field accepted: '+field)
 receipt={'actual_runtime_selection_refused':denied,'actual_current_solver_change_refused':solver,
  'changed_live_inputs_refused':input_rejections,'no_new_job_after_rejections':True,
  'native_receipt_field_faults':fields,'field_fault_scope':'pure protocol on actual native receipt; no changed OS or Python claimed',
  'original_runtime_restored':True,'original_live_control_restored':True,'mock_llm_calls':0}
 (api.root/'resume-environment-negative.json').write_text(json.dumps(receipt,indent=2),encoding='utf8')
 return [{'actual_runtime_selection':denied},{'actual_current_solver_selection':solver},
  {'changed_live_inputs':input_rejections,'no_extra_job':True},
  {'toolchain_fields':fields,'scope':'pure_protocol_on_actual_native_receipt','changed_and_missing_each_refused':True}]

if __name__=='__main__':asyncio.run(main(selected_override=partial(scenario,extra_negative_checks=negative_checks),fixture_override=FIXTURE,store_volume_max=16))
