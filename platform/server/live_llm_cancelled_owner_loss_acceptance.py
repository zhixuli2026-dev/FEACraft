"""Real cancelled running owners on Windows/WSL/Linux; no simulated inference."""
import asyncio,json
from app.services import process_identity_native as identity
import live_llm_remote_worker_acceptance as baseline
from live_llm_remote_namespace_acceptance import CrashWorker,scenario as linux_scenario
from live_llm_root_startup_loss_acceptance import scenario as windows_scenario

CHECK='''
import asyncio,copy,json,sys
from sqlalchemy import select
from app.db import SessionLocal,engine
from app.models import Job,JobAttempt,Case
from app.services.job_scope_recovery import validate_certificate
from app.services.job_write_checkpoint import validate
async def main():
 async with SessionLocal() as db:
  job=await db.get(Job,int(sys.argv[1]))
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
  case=await db.get(Case,job.case_id)
  original=json.loads(attempt.write_checkpoint_json)
  assert job.status==attempt.phase=='cancelled' and attempt.reason_code=='cancel_requested'
  assert original['recovery']['cancel_requested_at']==attempt.cancel_requested_at.isoformat()
  validate(case,job,attempt);validate_certificate(case,job,attempt,original)
  edits=[('invented_stop',('stop',),{}),('missing_guardians',('recovery','guardians'),[]),
   ('live_owner',('recovery','worker_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('changed_snapshot',('recovery','snapshot_sha256'),'0'*64),
   ('missing_cancellation',('recovery','cancel_requested_at'),None),
   ('changed_cancellation',('recovery','cancel_requested_at'),'2000-01-01T00:00:00'),
   ('changed_original_identity',('process_identity_json',),'{}'),
   ('changed_original_output',('recovery','original_output_manifest_json'),'\u007b"invented":true\u007d'),
   ('changed_guardian_birth',('recovery','guardians',0,'prepared','prepared','guardian','pid'),1),
   ('boolean_case_id',('case_id',),True)]
  if original['recovery']['native_history']:
   edits += [('missing_kernel_history',('recovery','native_history'),[]),
    ('missing_kernel_retirement',('recovery','native_retirements'),[]),
    ('live_native_gate',('recovery','native_retirements',0,'native_gate_exit'),{'state':'match','reason':'birth_and_scope_match'})]
  refused=[]
  for name,path,value in edits:
   altered=copy.deepcopy(original);target=altered
   for key in path[:-1]:target=target[key]
   target[path[-1]]=value
   try:validate_certificate(case,job,attempt,altered)
   except (ValueError,KeyError,TypeError,AttributeError):refused.append(name)
   else:raise AssertionError('Altered cancelled-owner certificate accepted: '+name)
  assert json.loads(attempt.write_checkpoint_json)==original
  print(json.dumps({'original_valid':True,'refused':refused,'database_modified':False}))
 await engine.dispose()
asyncio.run(main())
'''


async def scenarios(api,worker,selected):
    await linux_scenario(api,worker,selected,filename='namespace-recovery-normal.json')
    await linux_scenario(api,worker,selected,cancel_after_exit=True,filename='namespace-recovery-cancelled.json')
    for runtime in ['windows','wsl']:
        await windows_scenario(api,worker,selected,runtime=runtime,stage='running',cancel=True,
            cancel_before_exit=True,certificate_check=CHECK)
    await windows_scenario(api,worker,selected,runtime='wsl',stage='admitted',cancel=True)


async def verify(configuration,selected,admin,observed):
    windows={'host_id':identity.host_id(),'platform':'windows'}
    environment={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({'windows':{'host_id':observed['host_id'],'platform':'linux'},'wsl':windows}),
        'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'command.geometry':windows})}
    return await baseline.verify(configuration,selected,admin,observed,worker_factory=CrashWorker,
        after_publish=scenarios,environment_overrides=environment)


if __name__=='__main__':asyncio.run(baseline.main(entry='platform/server/live_llm_cancelled_owner_loss_acceptance.py',verifier=verify))
