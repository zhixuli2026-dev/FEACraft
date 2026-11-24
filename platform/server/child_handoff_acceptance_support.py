"""Actual child reservation/allocation/guardian loss hooks; no model substitution."""
import asyncio
from app.services import process_identity_native as identity,windows_scope_guardian as guardian
from app.services.owned_process import probe_native


def fault_for(stage):
    assert stage in {'reserved','allocation_intent','allocation_prepared','allocation_returned','allocated','launch_intended','guardian_returned'}
    return 'HANDOFF_STAGE='+repr(stage)+'\n'+'''
from contextvars import ContextVar
from app.services import native_child_records,child_launch_registration,job_native_scopes,job_scope_guardians,owned_process
async def lose_child_owner(owner,identifier,extra=None):
 root=Path(FAULT_ROOT)
 if not (root/'child-armed').exists():return
 (root/'child-armed').unlink()
 target=root/'child-prepared.json';staged=target.with_suffix('.staged')
 staged.write_text(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id,'child_id':identifier,
  'worker':identity.observe(os.getpid()),'stage':HANDOFF_STAGE,'independent_extra':extra}))
 staged.replace(target)
 while not (root/'child-crash-now').exists():await asyncio.sleep(.02)
 os._exit(17)
original_reserve=native_child_records.reserve
async def reserve_child(owner,parent,identifier,*args,**kwargs):
 result=await original_reserve(owner,parent,identifier,*args,**kwargs)
 if HANDOFF_STAGE=='reserved':await lose_child_owner(owner,identifier)
 return result
native_child_records.reserve=reserve_child
original_record=job_native_scopes.record
async def record_scope(owner,kind,bound,value,**kwargs):
 child=bound['child_id']
 if child is not None and HANDOFF_STAGE=='allocation_returned' and kind==job_native_scopes.ALLOCATED:
  await lose_child_owner(owner,child,{'allocation':value})
 result=await original_record(owner,kind,bound,value,**kwargs)
 selected={'allocation_intent':job_native_scopes.INTENT,'allocation_prepared':job_native_scopes.PREPARED,'allocated':job_native_scopes.ALLOCATED}
 if child is not None and kind==selected.get(HANDOFF_STAGE):
  await lose_child_owner(owner,child,{'preparation':value} if kind==job_native_scopes.PREPARED else {'allocation':value} if kind==job_native_scopes.ALLOCATED else None)
 return result
job_native_scopes.record=record_scope
original_launch=child_launch_registration.reserve
async def reserve_launch(owner,parent,identifier,*args,**kwargs):
 result=await original_launch(owner,parent,identifier,*args,**kwargs)
 if HANDOFF_STAGE=='launch_intended':await lose_child_owner(owner,identifier)
 return result
child_launch_registration.reserve=reserve_launch
selected_guardian=ContextVar('handoff_child_guardian',default=None)
original_guardian=job_scope_guardians.prepare
async def prepare_guardian(owner,owned,**kwargs):
 token=selected_guardian.set((owner,kwargs.get('child_id')))
 try:return await original_guardian(owner,owned,**kwargs)
 finally:selected_guardian.reset(token)
job_scope_guardians.prepare=prepare_guardian
original_process_guardian=owned_process.OwnedProcess.prepare_guardian
async def process_guardian(self,*args,**kwargs):
 result=await original_process_guardian(self,*args,**kwargs)
 current=selected_guardian.get()
 if HANDOFF_STAGE=='guardian_returned' and current is not None and current[1] is not None:
  await lose_child_owner(current[0],current[1],{'guardian':{'request':self.guardian.request,'prepared':self.guardian.prepared}})
 return result
owned_process.OwnedProcess.prepare_guardian=process_guardian
'''


async def verify_independent(marker):
    extra=marker.get('independent_extra') or {};result={'births':[],'guardian_closed':None}
    value=extra.get('guardian')
    if value:
        deadline=asyncio.get_running_loop().time()+30
        while True:
            try:receipt=guardian.read(value['request'],'receipt.json')
            except FileNotFoundError:receipt=None
            observed=await asyncio.to_thread(identity.probe,value['prepared']['guardian'])
            if receipt is not None and observed['state']=='exited':break
            assert asyncio.get_running_loop().time()<deadline,'Actual uncommitted guardian did not finish'
            await asyncio.sleep(.1)
        guardian.validate_receipt(value['request'],value['prepared'],receipt)
        result['births'].append({'identity':value['prepared']['guardian'],'exit':observed});result['guardian_closed']=True
    prepared=extra.get('preparation') or extra.get('allocation',{}).get('preparation')
    if prepared:
        for birth,distro in [(prepared['host']['host'],None),(prepared['native']['controller'],prepared['request']['distribution'])]:
            observed=await probe_native(distro,birth) if distro else await asyncio.to_thread(identity.probe,birth)
            assert observed['state']=='exited';result['births'].append({'identity':birth,'exit':observed})
    return result


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
  proof=original['recovery'];child=next(iter(proof['child_handoff_closures']));value=proof['child_handoff_closures'][child]
  h=('recovery','child_handoff_closures',child);c=(*h,'closure')
  assert proof['schema']=='vibeflow.job-child-handoff-recovery.v1' and original['stop'] is None
  assert not any(kind in {'attempt.child_launch_observed','attempt.child_launch_prepared','attempt.native_scope_admitted','attempt.child_running'} for kind,_ in value['history'])
  edits=[('invented_stop',('stop',),{}),('missing_parent_scope',('recovery','guardians'),[]),
   ('live_worker',(*h,'worker_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('changed_snapshot',('recovery','snapshot_sha256'),'0'*64),('missing_child_closures',('recovery','child_handoff_closures'),{}),
   ('changed_child',(*h,'child_id'),'00000000-0000-0000-0000-000000000000'),
   ('changed_history',(*h,'history_sha256'),'0'*64),('changed_parent',(*h,'root_guardian_sha256'),'0'*64),
   ('missing_reservation',(*h,'history'),value['history'][1:]),
   ('changed_release_contract',(*h,'history',0,1,'preparation_contract','business_release'),'unconditional'),
   ('invented_unknown_exit',(*c,'unknown_process_exit_inferred'),True),
   ('invented_business_release',(*c,'business_not_released'),False),
   ('changed_unknown_bootstraps',(*c,'unobserved_bootstraps'),[] if value['closure']['unobserved_bootstraps'] else ['invented_process']),
   ('changed_native_history',('recovery','native_history'),[] if proof['native_history'] else [['invented',{}]]),
   ('schema_downgrade',('recovery','schema'),'vibeflow.job-child-startup-recovery.v1')]
  if value['closure']['retirement']:
   edits += [('missing_retirement',(*c,'retirement'),None),('missing_known_births',(*c,'known_birth_exits'),[]),
    ('changed_retirement_operation',(*c,'retirement','request','operation'),'observe')]
  else:edits.append(('invented_retirement',(*c,'retirement'),{}))
  if attempt.phase=='cancelled':edits.append(('changed_cancellation',('recovery','cancel_requested_at'),None))
  refused=[]
  for name,path,value in edits:
   altered=copy.deepcopy(original);target=altered
   for key in path[:-1]:target=target[key]
   target[path[-1]]=value
   try:validate_certificate(case,job,attempt,altered)
   except (ValueError,KeyError,TypeError,AttributeError):refused.append(name)
   else:raise AssertionError('Altered child handoff certificate accepted: '+name)
  assert json.loads(attempt.write_checkpoint_json)==original
  print(json.dumps({'original_valid':True,'refused':refused,'database_modified':False}))
 await engine.dispose()
asyncio.run(main())
'''
