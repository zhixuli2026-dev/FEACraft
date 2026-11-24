"""Real native initialization, committed quota credit and actual cancellation."""
FIXTURE=r'''
import asyncio,hashlib,json,math,os,subprocess,time,uuid
from datetime import timedelta
from pathlib import Path
from fastapi import Depends,HTTPException
from sqlalchemy import select
from app.deps import get_current_user
from app.db import SessionLocal
from app.config import get_settings
from app.models import Plan,NativeCliSession,CliInitializationReservation
from app.services import codex_sessions as sessions,codex_records as records,process_identity_native as identity
from app.services import cli_initialization_capacity as capacity,storage_usage,quota
from app.services.storage_admission import reserved_bytes,lock_owner
from app.services.codex_wsl_state import initialization_payload
from app.services.durable_events import now

@app.post('/api/v1/test/cli-initialization')
async def initialization_checks(body:dict,user=Depends(get_current_user)):
 settings=get_settings();original_root=settings.codex_wsl_state_root
 current=None;task=None;release=asyncio.Event();plan_id=None;original_plan=None;pad=None
 evidence={'passed':False,'mock_llm_calls':0,'checks':[],'sessions':[]}
 path=Path(__file__).parent/'cli-initialization-checks.json'
 def save():path.write_text(json.dumps(evidence,indent=2),encoding='utf8')
 def checked(name,**data):evidence['checks'].append({'name':name,'kind':'actual_infrastructure',**data});save()
 async def credit(identifier):
  async with SessionLocal() as db:
   row=await db.get(CliInitializationReservation,identifier)
   return None if row is None else {'session_id':row.session_id,'size':row.size,'remaining_bytes':row.remaining_bytes,
    'state':row.state,'receipt':json.loads(row.receipt_json),'binding':json.loads(row.binding_json)}
 async def stopped(session):
  async with SessionLocal() as db:row=records.public(await db.get(NativeCliSession,session.id))
  assert row['cleanup_verified']
  if row['process']['root'] is not None:
   assert row['stop']['native_state']=='exited'
   assert identity.probe(row['process']['root']['host'])['state']=='exited'
  evidence['sessions'].append(row);save();return row
 try:
  result=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','-u','nobody','--exec','/usr/bin/python3','-c',
   "import tempfile; print(tempfile.mkdtemp(prefix='vibeflow-cli-init-',dir='/tmp'))"],
   capture_output=True,text=True,encoding='utf8',check=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
  selected=result.stdout.strip();assert selected.startswith('/tmp/vibeflow-cli-init-') and '/' not in selected[5:]
  settings.codex_wsl_state_root=selected;evidence['retained_native_test_root']=selected
  current=sessions.Session(body['case_id'],None,user.id,runtime='wsl')
  await current.prepare()
  initial=await credit(current.id);size=sum(map(len,initialization_payload(current.native_state)))
  assert initial['state']=='active' and initial['size']==size and initial['remaining_bytes']==size
  assert current.client.transport.owned.proc is None and not Path(current.selected['auth_home']).exists()
  checked('native_credit_is_committed_before_any_process_or_native_file',size=size)
  measured=(await storage_usage.measure_user(user.id)).bytes
  async with SessionLocal() as db:
   plan=await db.scalar(select(Plan).where(Plan.code=='free'));plan_id=plan.id;original_plan=plan.quotas
   mb=math.ceil((measured+size)/1024/1024)+1;values=json.loads(original_plan);values['storage_mb']=mb
   plan.quotas=json.dumps(values);await db.commit()
  pad=current.input_root.parent/'quota-padding.bin';pad.write_bytes(os.urandom(mb*1024*1024-measured-size+1))
  async with SessionLocal() as db:
   assert await reserved_bytes(db,user.id)>=size
   try:await quota.check_storage_quota(db,user,0)
   except HTTPException as error:assert error.status_code==429
   else:raise AssertionError('Committed native credit was not included in next admission')
  assert (await credit(current.id))['state']=='active'
  checked('actual_remaining_capacity_one_byte_short_includes_native_credit')
  pad.unlink();pad=None
  async with SessionLocal() as db:
   plan=await db.get(Plan,plan_id);plan.quotas=original_plan;await db.commit()
  # A real stale lease is not proof that initialization can no longer run.
  async with SessionLocal() as db:
   row=await db.get(NativeCliSession,current.id);row.lease_expires_at=now()-timedelta(days=1);await db.commit()
  async with SessionLocal() as db:
   await lock_owner(db,user.id);await capacity.reconcile(db,user.id);await db.commit()
  assert (await credit(current.id))['state']=='active'
  checked('actual_expired_lease_without_stop_receipt_keeps_full_credit')
  await current.close();await stopped(current)
  settled=await credit(current.id)
  assert settled['state']=='settled' and settled['remaining_bytes']==0 and settled['receipt']['status']=='verified_stop'
  checked('verified_not_started_receipt_settles_credit')
  # The registered bridge really exists and waits for its initialization permit.
  current=sessions.Session(body['case_id'],None,user.id,runtime='wsl');ready=asyncio.Event()
  original_bridge=current.register_bridge
  async def held_bridge(birth):
   await original_bridge(birth)
   assert (await credit(current.id))['state']=='active'
   assert not Path(current.selected['auth_home']).exists()
   ready.set();await release.wait()
  current.register_bridge=held_bridge
  task=asyncio.create_task(current.start())
  await asyncio.wait_for(ready.wait(),35)
  task.cancel();await asyncio.gather(task,return_exceptions=True);task=None
  await stopped(current)
  assert (await credit(current.id))['receipt']['status']=='verified_stop'
  assert not Path(current.selected['auth_home']).exists()
  checked('real_registered_bridge_cancelled_before_initializer_releases_only_after_verified_exit')
  current=sessions.Session(body['case_id'],body['conversation_id'],user.id,runtime='wsl')
  original_prepared=current.state_prepared
  async def completed_initializer():
   before=await credit(current.id);assert before['state']=='active'
   home=Path(current.selected['auth_home'])
   assert (home/'config.toml').stat().st_size+(home.parent/'owner.json').stat().st_size==before['size']
   await original_prepared()
   after=await credit(current.id)
   assert after['state']=='settled' and after['remaining_bytes']==0 and after['receipt']['status']=='prepared'
   evidence['prepared_credit']=after;save()
  current.state_prepared=completed_initializer
  await current.start()
  assert not current.stopping and current.client.initialize_result['platformOs']=='linux'
  checked('actual_initializer_exact_bytes_and_committed_prepared_receipt_before_cli_launch')
  await current.close();await stopped(current)
  evidence['retained_home']=current.selected['auth_home']
  evidence['retained_config_sha256']=hashlib.sha256((Path(current.selected['auth_home'])/'config.toml').read_bytes()).hexdigest()
  async with SessionLocal() as db:
   await lock_owner(db,user.id);await capacity.reconcile(db,user.id);await capacity.reconcile(db,user.id)
   assert await reserved_bytes(db,user.id)==0;await db.commit()
  assert (await credit(current.id))['receipt']['status']=='prepared'
  checked('repeated_reconciliation_preserves_prepared_receipt_and_zero_remaining_credit')
  evidence['passed']=True;save();return evidence
 except BaseException as error:
  evidence['failure_type']=type(error).__name__;evidence['failure_code']=getattr(error,'code',None);save();raise
 finally:
  release.set()
  if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
  if current is not None:
   try:await current.close(error='AcceptanceCleanup')
   except Exception:pass
  if original_plan is not None:
   async with SessionLocal() as db:
    plan=await db.get(Plan,plan_id);plan.quotas=original_plan;await db.commit()
  if not evidence['passed']:settings.codex_wsl_state_root=original_root

@app.get('/api/v1/test/cli-initialization-deleted')
async def initialization_deleted(user=Depends(get_current_user)):
 path=Path(__file__).parent/'cli-initialization-checks.json';evidence=json.loads(path.read_text(encoding='utf8'))
 identifier=evidence['prepared_credit']['session_id']
 async with SessionLocal() as db:
  assert await db.get(NativeCliSession,identifier) is None
  assert await db.get(CliInitializationReservation,identifier) is None
 assert hashlib.sha256((Path(evidence['retained_home'])/'config.toml').read_bytes()).hexdigest()==evidence['retained_config_sha256']
 evidence['checks'].append({'name':'actual_conversation_delete_removes_only_settled_credit_and_preserves_private_files','kind':'actual_infrastructure'})
 path.write_text(json.dumps(evidence,indent=2),encoding='utf8');return evidence
'''


def verify(api,**_):
    import httpx
    conversation=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual initialized credit deletion','permission_mode':'ask'})
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=240,trust_env=False) as client:
        response=client.post('/test/cli-initialization',json={'case_id':api.case_id,'conversation_id':conversation['id']})
        response.raise_for_status();result=response.json()
    assert result['passed'] and len(result['checks'])==7 and len(result['sessions'])==3
    api.call('DELETE','/conversations/'+str(conversation['id']))
    result=api.call('GET','/test/cli-initialization-deleted')
    assert len(result['checks'])==8
    print('CLI_INITIALIZATION_PASS '+str(api.root),flush=True)
    return result
