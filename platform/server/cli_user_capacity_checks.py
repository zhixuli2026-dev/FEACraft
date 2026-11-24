"""Real user quota transactions and private writes; no simulated CLI inference."""
FIXTURE=r'''
import asyncio,base64,hashlib,json,math,os,threading,uuid
from pathlib import Path
from fastapi import Depends,HTTPException
from sqlalchemy import select
from app.deps import get_current_user
from app.db import SessionLocal
from app.models import Plan,User,NativeCliSession,OwnerStorageReservation
from app.config import get_settings
from app import storage
from app.services import codex_sessions as sessions,codex_records as records,process_identity_native as identity
from app.services import cli_user_capacity as capacity,codex_images as images,storage_usage,quota
from app.services.cli_private_paths import root as private_root
from app.services.codex_projection import Secrets

@app.post('/api/v1/test/cli-user-capacity')
async def user_capacity(body:dict,user=Depends(get_current_user)):
 root=private_root(get_settings());session=None;filler=None;operation=None;peer=None
 original_private=sessions.private_environment;release=threading.Event();ready=threading.Event()
 evidence={'passed':False,'mock_llm_calls':0,'llm_calls':0,'checks':[]}
 path=Path(__file__).parent/'cli-user-capacity-checks.json'
 async with SessionLocal() as db:
  plan=await db.scalar(select(Plan).where(Plan.code=='free'));plan_id=plan.id;original_quotas=plan.quotas
 def save():path.write_text(json.dumps(evidence,indent=2),encoding='utf8')
 def checked(name,**data):
  evidence['checks'].append({'name':name,'kind':'actual_infrastructure',**data});save()
 async def reset():
  nonlocal filler
  if filler is not None:filler.unlink(missing_ok=True);filler=None
  async with SessionLocal() as db:
   value=await db.get(Plan,plan_id);value.quotas=original_quotas;await db.commit()
 async def remaining(size):
  nonlocal filler
  await reset()
  used=(await storage_usage.measure_user(user.id)).bytes
  limit=math.ceil(used/(1024*1024))+2
  filler=root/'users'/str(user.id)/'quota-filler.bin'
  filler.write_bytes(os.urandom(limit*1024*1024-used-size))
  async with SessionLocal() as db:
   value=await db.get(Plan,plan_id);quotas=json.loads(original_quotas);quotas['storage_mb']=limit
   value.quotas=json.dumps(quotas);await db.commit()
 async def rejected(operation):
  try:await operation()
  except HTTPException as error:assert error.status_code==429,error.status_code
  else:raise AssertionError('User quota unexpectedly admitted private bytes')
 try:
  session=sessions.Session(body['case_id'],None,user.id);await session.start();await session.close()
  async with SessionLocal() as db:evidence['session']=records.public(await db.get(NativeCliSession,session.id))
  evidence['actual_exit']=identity.probe(evidence['session']['process']['root']['host'])
  assert evidence['session']['cleanup_verified'] and evidence['actual_exit']['state']=='exited'
  checked('actual_installed_cli_startup_uses_user_quota_and_exits')
  await remaining(32*1024)
  denied=sessions.Session(body['case_id'],None,user.id)
  await rejected(denied.start)
  assert denied.client is None and denied.owner is None and not (root/'users'/str(user.id)/'sessions'/denied.id).exists()
  checked('startup_peak_denied_before_private_directory_or_process_creation')
  await remaining(128*1024)
  identifier=str(uuid.uuid4())
  async with SessionLocal() as db:
   environment,workspace,_=await capacity.initialize(db,user.id,identifier,None,runtime='windows',distro=None)
   await db.commit()
  assert workspace.is_dir()
  checked('bounded_private_initialization_admitted_with_available_user_bytes')
  await reset();vault=Secrets(environment['CODEX_HOME']);first='PRIVATE_'+uuid.uuid4().hex;second='PRIVATE_'+uuid.uuid4().hex
  async with SessionLocal() as db:await capacity.remember(db,user.id,vault,[first]);await db.commit()
  old=hashlib.sha256(vault.path.read_bytes()).hexdigest();key=hashlib.sha256(vault.key_path.read_bytes()).hexdigest()
  size=vault.update_size([second]);await remaining(size-1)
  async def remember():
   async with SessionLocal() as db:await capacity.remember(db,user.id,vault,[second]);await db.commit()
  await rejected(remember)
  assert hashlib.sha256(vault.path.read_bytes()).hexdigest()==old and hashlib.sha256(vault.key_path.read_bytes()).hexdigest()==key
  checked('exact_vault_replacement_peak_denied_without_changing_old_files')
  await reset();await remember();assert Secrets(environment['CODEX_HOME']).load()==[first,second]
  checked('actual_vault_merge_commits_under_user_quota_lock')
  async with SessionLocal() as db:
   try:await capacity.remember(db,body['other_user_id'],vault,['PRIVATE_'+uuid.uuid4().hex])
   except ValueError:pass
   else:raise AssertionError('Another user was billed for this vault')
  checked('vault_account_binding_refuses_another_billing_user')
  from app.routers.codex import NativeImageIn
  raw=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG1sAAAAASUVORK5CYII=')
  prepared=images.prepare([NativeImageIn(name='quota.png',mime='image/png',data=base64.b64encode(raw).decode())])
  publication=images.Publication(session,str(uuid.uuid4()),prepared)
  await remaining(2*len(raw)-1)
  async def image_write():
   # Same helper and parent transaction as submit, without a model turn.
   async with SessionLocal() as db:
    await capacity.images(db,session,prepared)
    await asyncio.to_thread(publication.write);await db.commit()
  await rejected(image_write)
  assert not publication.folder.exists() and not storage.exists(session.selected['source_prefix'],prepared[0][0]['path'])
  checked('both_image_copies_are_admitted_before_either_write')
  await reset()
  # This separate authorized scope carries the real S3 owner journal when S3
  # is selected. It never fabricates a CLI response or launches a model turn.
  async with session.access(storage_capacity=True,private_bytes=len(raw)) as db:
   from app.services.owner_storage_budget import current
   scope=current()
   if storage._use_s3():assert scope is not None and scope.future_bytes==len(raw)
   await capacity.images(db,session,prepared)
   await asyncio.to_thread(publication.write);publication.committing=True
  publication.verify()
  assert publication.files[0][0].read_bytes()==storage.get_bytes(session.selected['source_prefix'],prepared[0][0]['path'])==raw
  if storage._use_s3():
   async with SessionLocal() as db:
    ticket=await db.get(OwnerStorageReservation,scope.ticket['id'])
    assert ticket.state=='settled' and ticket.remaining_bytes==0
    receipt=json.loads(ticket.receipt_json);assert receipt['closed'] and receipt['pending'] is None
   evidence['remote_capacity_settled']=True
  checked('actual_image_copies_commit_with_remote_credit_settled_when_applicable')
  def delayed(*args,**kwargs):
   result=original_private(*args,**kwargs);ready.set()
   if not release.wait(15):raise TimeoutError('Actual private writer release not delivered')
   return result
  sessions.private_environment=delayed
  async def writer():
   async with SessionLocal() as db:
    await capacity.initialize(db,user.id,str(uuid.uuid4()),None,runtime='windows',distro=None)
    await db.commit()
  async def competitor():
   async with SessionLocal() as db:
    try:await quota.check_storage_quota(db,await db.get(User,user.id),0)
    except HTTPException as error:return error.status_code
    return 200
  operation=asyncio.create_task(writer())
  assert await asyncio.to_thread(ready.wait,10)
  for _ in range(3):operation.cancel();await asyncio.sleep(.02)
  assert not operation.done()
  peer=asyncio.create_task(competitor());await asyncio.sleep(.2)
  async with SessionLocal() as db:dialect=db.get_bind().dialect.name
  if dialect=='postgresql':assert peer.done() and peer.result()==409
  else:assert not peer.done()
  release.set()
  try:await operation
  except asyncio.CancelledError:pass
  else:raise AssertionError('Cancelled writer unexpectedly returned success')
  result=await asyncio.wait_for(peer,10);assert result==(409 if dialect=='postgresql' else 200)
  assert await competitor()==200
  checked('repeated_cancellation_keeps_user_lock_until_actual_writer_drains',database=dialect)
  evidence['passed']=True;save();return evidence
 except BaseException as error:
  evidence['failure_type']=type(error).__name__;save();raise
 finally:
  release.set();sessions.private_environment=original_private
  if operation is not None:await asyncio.gather(operation,return_exceptions=True)
  if peer is not None:await asyncio.gather(peer,return_exceptions=True)
  await reset()
  if session is not None and not session.stopping:
   try:await session.close(error='AcceptanceCleanup')
   except Exception:pass
'''


def verify(api,**_):
    import httpx,uuid
    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
        'password':'isolated-test-123','name':'Capacity second user'})
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=180,trust_env=False) as client:
        response=client.post('/test/cli-user-capacity',json={'case_id':api.case_id,'other_user_id':other['user']['id']})
        response.raise_for_status();result=response.json()
    assert result['passed'] and len(result['checks'])==9
    print('CLI_USER_CAPACITY_PASS '+str(api.root),flush=True)
    return result
