"""Actual child launch boundary hooks and immutable certificate checks; no model provider."""

def fault_for(stage):
    assert stage in {'unobserved','created','host_ready','native_ready'}
    return 'OBSERVATION_STAGE='+repr(stage)+'\n'+'''
from app.services import child_launch_registration
original_child_observed=child_launch_registration.observed
async def observe_then_lose_owner(owner,parent_nonce,identifier,value):
 root=Path(FAULT_ROOT)
 armed=(root/'child-armed').exists()
 selected=armed and (OBSERVATION_STAGE=='unobserved' or value['stage']==OBSERVATION_STAGE)
 if not (selected and OBSERVATION_STAGE=='unobserved'):
  await original_child_observed(owner,parent_nonce,identifier,value)
 if selected:
  (root/'child-armed').unlink()
  target=root/'child-prepared.json';staged=target.with_suffix('.staged')
  staged.write_text(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id,'child_id':identifier,
   'worker':identity.observe(os.getpid()),'preparation':value,'stage':OBSERVATION_STAGE}))
  staged.replace(target)
  while not (root/'child-crash-now').exists():await asyncio.sleep(.02)
  os._exit(17)
child_launch_registration.observed=observe_then_lose_owner
'''


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
  proof=original['recovery'];child=next(iter(proof['child_preparations']));entries=proof['child_preparations'][child]
  assert not any(kind in {'attempt.child_launch_prepared','attempt.native_scope_admitted','attempt.child_running'} for kind,_ in entries)
  index=next(i for i,item in enumerate(proof['guardians']) if item['prepared']['child_id']==child)
  g=('recovery','guardians',index,'observed');h=('recovery','child_preparations',child)
  edits=[('invented_stop',('stop',),{}),('missing_guardian',('recovery','guardians'),[]),
   ('live_owner',('recovery','worker_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('changed_snapshot',('recovery','snapshot_sha256'),'0'*64),('missing_history',h,[]),
   ('changed_history_digest',(*g,'child_preparation_sha256'),'0'*64),
   ('changed_reservation',(*h,0,1,'purpose'),'wrong_tool'),
   ('changed_launch',(*h,1,1,'launch_nonce'),'0'*64),
   ('changed_host_identity',(*g,'host_identity'),None if proof['guardians'][index]['observed']['host_identity'] else {}),
   ('reordered_history',h,list(reversed(entries))),('schema_downgrade',('recovery','schema'),'vibeflow.job-scope-recovery.v1')]
  if len(entries)>2:
   edits += [('truncated_observations',h,entries[:-1]),('changed_observed_birth',(*h,2,1,'preparation','host','pid'),1)]
  if proof['native_retirements']:
   edits += [('missing_retirement',('recovery','native_retirements'),[]),
    ('missing_native_history',('recovery','native_history'),[]),
    ('changed_scope_inode',('recovery','native_retirements',0,'retirement','request','arguments','scope','inode'),1),
    ('changed_native_exit',('recovery','native_retirements',0,'native_gate_exit'),{'state':'match','reason':'birth_and_scope_match'})]
  if attempt.phase=='cancelled':
   assert proof['cancel_requested_at']==attempt.cancel_requested_at.isoformat()
   edits.append(('changed_cancel_request',('recovery','cancel_requested_at'),None))
  refused=[]
  for name,path,value in edits:
   altered=copy.deepcopy(original);target=altered
   for key in path[:-1]:target=target[key]
   target[path[-1]]=value
   try:validate_certificate(case,job,attempt,altered)
   except (ValueError,KeyError,TypeError,AttributeError):refused.append(name)
   else:raise AssertionError('Altered child observation certificate accepted: '+name)
  assert json.loads(attempt.write_checkpoint_json)==original
  print(json.dumps({'original_valid':True,'refused':refused,'database_modified':False}))
 await engine.dispose()
asyncio.run(main())
'''
