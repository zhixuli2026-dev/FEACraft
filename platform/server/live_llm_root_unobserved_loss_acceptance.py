"""Actual created root loses its first observation commit; full-scope recovery."""
import asyncio,json
from app.services import process_identity_native as identity
import live_llm_remote_worker_acceptance as baseline
from live_llm_root_startup_loss_acceptance import scenario

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
  validate(case,job,attempt);validate_certificate(case,job,attempt,original)
  proof=original['recovery'];registration=proof['root_preparation'];intent=registration['intent']
  assert registration['observations']==[] and registration['prepared'] is registration['admission'] is None
  assert json.loads(attempt.process_identity_json)==json.loads(attempt.child_processes_json)=={}
  observed=proof['guardians'][0]['observed'];receipt=observed['guardian_receipt']
  assert observed['host_identity'] is observed['host_exit'] is None
  assert receipt['active_processes']==0 and receipt['membership']['complete'] and receipt['membership']['total']>=1
  root=('recovery','root_preparation');guard=('recovery','guardians',0,'observed')
  # Structurally valid protocol mutation only, never a model reply or a
  # substitute process execution. The original chain must remain empty.
  invented={'schema':'vibeflow.process-preparation.v1','stage':'created','launch_nonce':intent['launch_nonce'],
   'runtime':intent['runtime'],'host':receipt['guardian'],'native':None,'scope':None,
   'gate_sha256':intent['gate_sha256'],'identity_probe_sha256':intent['identity_probe_sha256'],'observed_at':original['recorded_at']}
  from app.services.process_preparation import validate as validate_observation
  validate_observation(invented)
  edits=[('invented_stop',('stop',),{}),('missing_guardian',('recovery','guardians'),[]),
   ('live_owner',('recovery','worker_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('changed_snapshot',('recovery','snapshot_sha256'),'0'*64),('missing_preparation',root,None),
   ('changed_claim',(*root,'intent','claim_sha256'),'0'*64),('invented_original_running',(*root,'original_phase'),'running'),
   ('changed_cancellation',(*root,'cancel_requested_at'),'2000-01-01T00:00:00'),
   ('schema_downgrade',('recovery','schema'),'vibeflow.job-scope-recovery.v1'),
   ('invented_observation',(*root,'observations'),[invented]),('invented_host_birth',(*guard,'host_identity'),receipt['guardian']),
   ('invented_host_exit',(*guard,'host_exit'),{'state':'exited','reason':'process_exited'}),
   ('missing_preparation_binding',(*guard,'root_preparation_sha256'),None),
   ('delete_host_identity',(*guard,'host_identity'),None),('delete_host_exit',(*guard,'host_exit'),None),
   ('incomplete_job_membership',(*guard,'guardian_receipt','membership','complete'),False)]
  if proof['native_retirements']:
   assert len(proof['native_retirements'])==1 and proof['native_retirements'][0]['native_gate_exit'] is None
   edits += [('missing_kernel_history',('recovery','native_history'),[]),('missing_kernel_retirement',('recovery','native_retirements'),[]),
    ('invented_native_exit',('recovery','native_retirements',0,'native_gate_exit'),{'state':'exited','reason':'process_exited'}),
    ('invented_admission',('recovery','native_retirements',0,'admission'),{})]
  refused=[]
  for name,path,value in edits:
   altered=copy.deepcopy(original);target=altered
   for key in path[:-1]:target=target[key]
   if name.startswith('delete_'):del target[path[-1]]
   else:target[path[-1]]=value
   try:validate_certificate(case,job,attempt,altered)
   except (ValueError,KeyError,TypeError,AttributeError):refused.append(name)
   else:raise AssertionError('Altered unobserved-root certificate accepted: '+name)
  assert json.loads(attempt.write_checkpoint_json)==original
  print(json.dumps({'original_valid':True,'refused':refused,'database_modified':False,'unknown_births_preserved':True}))
 await engine.dispose()
asyncio.run(main())
'''

async def scenarios(api,worker,selected):
    for runtime,cancel in [('windows',False),('wsl',False),('wsl',True)]:
        await scenario(api,worker,selected,runtime=runtime,stage='unobserved',cancel=cancel,certificate_check=CHECK)
    await scenario(api,worker,selected,runtime='wsl',stage='created')

async def verify(configuration,selected,admin,observed):
    windows={'host_id':identity.host_id(),'platform':'windows'}
    environment={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({
        'windows':{'host_id':observed['host_id'],'platform':'linux'},'wsl':windows}),
        'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'command.geometry':windows})}
    return await baseline.verify(configuration,selected,admin,observed,after_publish=scenarios,environment_overrides=environment)

if __name__=='__main__':
    asyncio.run(baseline.main(entry='platform/server/live_llm_root_unobserved_loss_acceptance.py',verifier=verify))
