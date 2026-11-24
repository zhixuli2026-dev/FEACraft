"""Actual launch intent / uncommitted guardian loss with fresh publication."""
import asyncio,json
from app.services import process_identity_native as identity
import live_llm_remote_worker_acceptance as baseline
from live_llm_allocation_loss_acceptance import scenario

CHECK='''
import asyncio,copy,json,sys
from sqlalchemy import select
from app.db import SessionLocal,engine
from app.models import Job,JobAttempt,Case
from app.services.job_scope_recovery import validate_certificate
from app.services.job_write_checkpoint import validate
async def main():
 async with SessionLocal() as db:
  job=await db.get(Job,int(sys.argv[1]));attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id));case=await db.get(Case,job.case_id)
  original=json.loads(attempt.write_checkpoint_json);validate(case,job,attempt);validate_certificate(case,job,attempt,original)
  proof=original['recovery'];root=('recovery',);closure=(*root,'closure');launch=(*root,'launch_history',0,1)
  assert proof['schema']=='vibeflow.job-launch-preparation-recovery.v1' and len(proof['launch_history'])==1
  assert original['stop'] is None and json.loads(attempt.process_identity_json)==json.loads(attempt.child_processes_json)=={}
  assert proof['closure']['unobserved_bootstraps']==['windows_guardian']
  edits=[('invented_stop',('stop',),{}),('live_owner',(*root,'worker_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('changed_snapshot',(*root,'snapshot_sha256'),'0'*64),('changed_history_binding',(*closure,'history_sha256'),'0'*64),
   ('invented_release',(*closure,'business_not_released'),False),('invented_unknown_exit',(*closure,'unknown_process_exit_inferred'),True),
   ('changed_claim',('claim_sha256',),'0'*64),('missing_launch',(*root,'launch_history'),[]),
   ('duplicate_launch',(*root,'launch_history'),proof['launch_history']*2),
   ('legacy_launch',(*launch,'preparation_contract'),None),
   ('changed_contract',(*launch,'preparation_contract','business_release'),'unconditional'),
   ('changed_launch_nonce',(*launch,'launch_nonce'),'0'*64),('changed_gate',(*launch,'gate_sha256'),'0'*64),
   ('invented_running',(*root,'original_phase'),'running'),('changed_cancellation',(*root,'cancel_requested_at'),'2000-01-01T00:00:00'),
   ('invented_original_output',(*root,'original_output_manifest_json'),'{"process_stop":{}}'),
   ('schema_downgrade',(*root,'schema'),'vibeflow.job-allocation-recovery.v1'),
   ('missing_unknown_guardian',(*closure,'unobserved_bootstraps'),[])]
  if proof['native_history']:
   edits += [('missing_kernel_history',(*root,'native_history'),[]),('missing_retirement',(*closure,'retirement'),None),
    ('missing_known_births',(*closure,'known_birth_exits'),[]),
    ('live_creator',(*closure,'known_birth_exits',1,'exit'),{'state':'match','reason':'birth_and_scope_match'})]
  else:
   assert proof['closure']['retirement'] is None and proof['closure']['known_birth_exits']==[]
   edits += [('invented_retirement',(*closure,'retirement'),{}),('invented_known_births',(*closure,'known_birth_exits'),[{}])]
  refused=[]
  for name,path,value in edits:
   altered=copy.deepcopy(original);target=altered
   for key in path[:-1]:target=target[key]
   if name=='legacy_launch':del target[path[-1]]
   else:target[path[-1]]=value
   try:validate_certificate(case,job,attempt,altered)
   except (ValueError,KeyError,TypeError,AttributeError):refused.append(name)
   else:raise AssertionError('Altered guardian-handoff certificate accepted: '+name)
  assert json.loads(attempt.write_checkpoint_json)==original
  print(json.dumps({'original_valid':True,'refused':refused,'database_modified':False,'unknown_guardian_preserved':True}))
 await engine.dispose()
asyncio.run(main())
'''

async def scenarios(api,worker,selected):
    for runtime,stage,cancel in [('windows','launch_intended',False),('windows','guardian_returned',False),
            ('wsl','launch_intended',False),('wsl','guardian_returned',False),('wsl','guardian_returned',True)]:
        await scenario(api,worker,selected,stage,cancel,runtime=runtime,certificate_check=CHECK)
    await scenario(api,worker,selected,'unacknowledged')
    from live_llm_root_unobserved_loss_acceptance import CHECK as root_check
    from live_llm_root_startup_loss_acceptance import scenario as root_scenario
    await root_scenario(api,worker,selected,runtime='wsl',stage='unobserved',certificate_check=root_check)

async def verify(configuration,selected,admin,observed):
    windows={'host_id':identity.host_id(),'platform':'windows'}
    environment={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({'windows':{'host_id':observed['host_id'],'platform':'linux'},'wsl':windows}),
        'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'command.geometry':windows})}
    return await baseline.verify(configuration,selected,admin,observed,after_publish=scenarios,environment_overrides=environment)

if __name__=='__main__':
    asyncio.run(baseline.main(entry='platform/server/live_llm_guardian_handoff_acceptance.py',verifier=verify))
