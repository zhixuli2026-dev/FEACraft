"""Actual WSL CLI, native files, rename/symlink faults and quota scans."""
FIXTURE=r'''
import asyncio,hashlib,json,os,stat,subprocess,time,uuid
from pathlib import Path
from fastapi import Depends,HTTPException
from sqlalchemy import select
from app.deps import get_current_user
from app.db import SessionLocal
from app.models import Plan,NativeCliSession
from app.config import get_settings
from app.services import codex_sessions as sessions,codex_records as records,process_identity_native as identity
from app.services import storage_usage as usage,node_storage_budget as budget,cli_native_usage as native_usage,quota
from app.services import cli_user_capacity as capacity
from app.services.cli_private_paths import native_root
from app.services.runtime_config import get_runtime_setting
from app.services.codex_projection import Secrets

@app.post('/api/v1/test/cli-native-usage')
async def native_usage_checks(body:dict,user=Depends(get_current_user)):
 settings=get_settings();original_native=settings.codex_wsl_state_root
 limit=settings.node_storage_limit_mb;minimum=settings.node_storage_min_free_mb;maximum=settings.storage_measurement_max_entries
 session=None;base=None;offline=None;original_quotas=None;plan_id=None
 evidence={'passed':False,'mock_llm_calls':0,'llm_calls':0,'checks':[]}
 path=Path(__file__).parent/'cli-native-usage-checks.json'
 def save():path.write_text(json.dumps(evidence,indent=2),encoding='utf8')
 def checked(name,**data):
  evidence['checks'].append({'name':name,'kind':'actual_infrastructure',**data});save()
 distro=get_runtime_setting('wsl_distro','vibeflow-linux')
 async def native(code,*args):
  return await asyncio.to_thread(subprocess.run,['wsl.exe','-d',distro,'-u','nobody','--exec','/usr/bin/python3','-c',code,*args],
   capture_output=True,text=True,encoding='utf8',timeout=20,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
 async def measured(identifier):return (await usage.measure_user(identifier)).bytes
 async def denied_inventory():
  try:await measured(user.id)
  except usage.MeasurementUnavailable:pass
  else:raise AssertionError('Unknown native inventory was counted as empty')
  try:await asyncio.to_thread(budget.observe,private=True)
  except Exception as error:
   assert getattr(error,'code',None)=='node_storage_unavailable'
   return error.storage_observation
  else:raise AssertionError('Unknown native node inventory was admitted')
 try:
  created=await native("import tempfile; print(tempfile.mkdtemp(prefix='vibeflow-cli-native-',dir='/tmp'))")
  selected=created.stdout.strip();assert selected.startswith('/tmp/vibeflow-cli-native-') and '/' not in selected[len('/tmp/'):]
  settings.codex_wsl_state_root=selected;settings.node_storage_min_free_mb=0
  base=native_root(settings);assert base.resolve(strict=True)==base
  evidence['retained_native_test_root']=selected
  session=sessions.Session(body['case_id'],None,user.id,runtime='wsl');await session.start()
  evidence['initialize_result']=session.client.initialize_result
  active=await asyncio.to_thread(budget.observe,private=True)
  evidence['active_inventory']=active
  assert active['scope']=='host_and_configured_native_cli' and not session.stopping
  links=[]
  for directory,dirs,files in os.walk(base,followlinks=False):
   for name in dirs+files:
    candidate=Path(directory)/name
    try:info=candidate.lstat()
    except FileNotFoundError:continue
    if native_usage.link_entry(info):links.append({'relative':candidate.relative_to(base).as_posix(),'bytes':info.st_size})
  evidence['actual_cli_links']=links;save()
  await session.close()
  async with SessionLocal() as db:evidence['session']=records.public(await db.get(NativeCliSession,session.id))
  evidence['actual_host_exit']=identity.probe(evidence['session']['process']['root']['host'])
  assert evidence['session']['cleanup_verified'] and evidence['session']['stop']['native_state']=='exited'
  assert evidence['actual_host_exit']['state']=='exited'
  checked('actual_linux_cli_initializes_with_native_inventory_and_verified_native_exit')
  first=await measured(user.id);second=await measured(body['other_user_id'])
  before=await asyncio.to_thread(budget.observe,private=True)
  native_file=Path(session.selected['auth_home']).parent/'native-retained.bin'
  native_file.write_bytes(os.urandom(2*1024*1024));digest=hashlib.sha256(native_file.read_bytes()).hexdigest()
  assert await measured(user.id)==first+2*1024*1024 and await measured(body['other_user_id'])==second
  checked('actual_native_account_bytes_are_charged_to_correct_user')
  after=await asyncio.to_thread(budget.observe,private=True)
  assert after['bytes']==before['bytes']+2*1024*1024
  checked('configured_native_bytes_increase_shared_node_inventory_exactly')
  other=base/'users'/str(body['other_user_id']);other.mkdir(mode=0o700,exist_ok=True)
  (other/'other.bin').write_bytes(os.urandom(8192))
  assert await measured(body['other_user_id'])==second+8192 and await measured(user.id)==first+2*1024*1024
  checked('second_native_user_does_not_change_first_user_inventory')
  settings.node_storage_limit_mb=1
  try:await asyncio.to_thread(budget.observe,private=True)
  except Exception as error:assert getattr(error,'code',None)=='node_storage_full'
  else:raise AssertionError('Actual native retained bytes did not block node limit')
  settings.node_storage_limit_mb=limit
  checked('actual_native_bytes_block_node_capacity')
  async with SessionLocal() as db:
   plan=await db.scalar(select(Plan).where(Plan.code=='free'));plan_id=plan.id;original_quotas=plan.quotas
   values=json.loads(plan.quotas);values['storage_mb']=1;plan.quotas=json.dumps(values);await db.commit()
  async with SessionLocal() as db:
   try:await quota.check_storage_quota(db,user,0)
   except HTTPException as error:assert error.status_code==429
   else:raise AssertionError('Actual native retained bytes did not block user quota')
  async with SessionLocal() as db:
   plan=await db.get(Plan,plan_id);plan.quotas=original_quotas;await db.commit()
  original_quotas=None
  checked('actual_native_bytes_block_existing_plan_quota')
  secret='PRIVATE_'+uuid.uuid4().hex;vault=Secrets(session.selected['auth_home'])
  async with SessionLocal() as db:await capacity.remember(db,user.id,vault,[secret]);await db.commit()
  assert Secrets(session.selected['auth_home']).load()==[secret]
  checked('actual_native_vault_uses_user_peak_admission_with_native_inventory')
  # Real dangling and directory links stay metadata, never target traversal.
  link_root=base/'users'/str(user.id)
  before_links=await measured(user.id)
  before_node=await asyncio.to_thread(budget.observe,private=True)
  target=selected+'/users/'+str(body['other_user_id'])
  await native('import os,sys; os.symlink(sys.argv[1],sys.argv[2]); os.symlink(sys.argv[3],sys.argv[4])',
   target,selected+'/users/'+str(user.id)+'/other-user-link',
   selected+'/absent-target',selected+'/users/'+str(user.id)+'/dangling-link')
  actual_links=[link_root/'other-user-link',link_root/'dangling-link']
  assert all(native_usage.link_entry(item.lstat()) for item in actual_links)
  link_bytes=sum(item.lstat().st_size for item in actual_links)
  assert await measured(user.id)==before_links+link_bytes
  assert (await asyncio.to_thread(budget.observe,private=True))['bytes']==before_node['bytes']+link_bytes
  checked('native_links_charge_own_bytes_without_following_other_user_or_missing_target',bytes=link_bytes)
  # Move only the verified, uniquely created and now inactive test root.
  offline=base.with_name(base.name+'-offline')
  assert base.resolve()==base and offline.parent==base.parent and offline.name==base.name+'-offline' and not offline.exists()
  base.rename(offline)
  observation=await denied_inventory()
  assert observation['phase']=='native_private_inventory' and observation['reason']=='native_unavailable'
  offline.rename(base);offline=None
  assert hashlib.sha256(native_file.read_bytes()).hexdigest()==digest
  checked('missing_configured_native_root_refuses_user_and_node_measurement',observation=observation)
  offline=base.with_name(base.name+'-offline');assert not offline.exists() and base.resolve()==base
  base.rename(offline)
  await native('import os,sys; os.symlink(sys.argv[1],sys.argv[2],target_is_directory=True)',selected+'-offline',selected)
  observation=await denied_inventory()
  assert observation['phase']=='native_private_inventory'
  assert native_usage.link_entry(base.lstat());base.unlink();offline.rename(base);offline=None
  assert hashlib.sha256(native_file.read_bytes()).hexdigest()==digest
  checked('actual_native_root_symlink_refused_without_modifying_target',observation=observation)
  settings.storage_measurement_max_entries=1
  counter=usage.Counter(settings,time.monotonic()+10,None)
  try:await asyncio.to_thread(native_usage.scan,settings,counter)
  except usage.MeasurementUnavailable:assert counter.entries>1
  else:raise AssertionError('Actual native directory entry bound was bypassed')
  settings.storage_measurement_max_entries=maximum
  checked('native_inventory_obeys_actual_entry_bound')
  session=sessions.Session(body['case_id'],None,user.id,runtime='wsl');await session.start()
  settings.node_storage_limit_mb=1
  until=time.monotonic()+20
  while session.close_task is None and time.monotonic()<until:await asyncio.sleep(.05)
  assert session.close_task is not None,'Native runtime capacity did not schedule closure'
  try:await session.close()
  except RuntimeError:pass
  async with SessionLocal() as db:evidence['overflow_session']=records.public(await db.get(NativeCliSession,session.id))
  row=evidence['overflow_session']
  assert row['phase']=='failed' and row['error']=='node_storage_full' and row['cleanup_verified']
  assert row['stop']['native_state']=='exited' and identity.probe(row['process']['root']['host'])['state']=='exited'
  settings.node_storage_limit_mb=limit
  assert hashlib.sha256(native_file.read_bytes()).hexdigest()==digest
  checked('actual_native_capacity_observer_closure_is_drained_and_persisted_with_files_retained')
  evidence['passed']=True;save();return evidence
 except BaseException as error:
  evidence['failure_type']=type(error).__name__
  evidence['storage_observation']=getattr(error,'storage_observation',None)
  save();raise
 finally:
  settings.node_storage_limit_mb=limit;settings.node_storage_min_free_mb=minimum;settings.storage_measurement_max_entries=maximum
  if base is not None and offline is not None and offline.exists():
   if base.exists() and native_usage.link_entry(base.lstat()):base.unlink()
   if not base.exists():offline.rename(base)
  if original_quotas is not None:
   async with SessionLocal() as db:
    plan=await db.get(Plan,plan_id);plan.quotas=original_quotas;await db.commit()
  if session is not None:
   try:await session.close(error='AcceptanceCleanup')
   except Exception:pass
  # Keep this isolated API's native root configured for the subsequent real
  # model readback and its actual user quota checks; never change deployment env.
  if not evidence['passed']:settings.codex_wsl_state_root=original_native
'''


def verify(api,**_):
    import httpx,uuid
    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
        'password':'isolated-test-123','name':'Native inventory second user'})
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=180,trust_env=False) as client:
        response=client.post('/test/cli-native-usage',json={'case_id':api.case_id,'other_user_id':other['user']['id']})
        response.raise_for_status();result=response.json()
    assert result['passed'] and len(result['checks'])==12
    print('CLI_NATIVE_USAGE_PASS '+str(api.root),flush=True)
    return result
