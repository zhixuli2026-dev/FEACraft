"""Actual host files, two users, legacy DB ownership and bounded scan diagnostics."""
FIXTURE=r'''
import asyncio,hashlib,json,os,uuid
from pathlib import Path
from fastapi import Depends,HTTPException
from sqlalchemy import select
from app.deps import get_current_user
from app.config import get_settings
from app.db import SessionLocal
from app.models import NativeCliSession,User,Plan
from app.services import codex_sessions as sessions,codex_records as records,process_identity_native as identity
from app.services import storage_usage as usage,node_storage_budget as budget,quota
from app.services.cli_private_paths import root as private_root

@app.post('/api/v1/test/cli-private-usage')
async def private_usage(body:dict,user=Depends(get_current_user)):
 settings=get_settings();maximum=settings.storage_measurement_max_entries
 root=private_root(settings);session=None;unknown_file=None;original_quotas=None;plan_id=None
 evidence={'passed':False,'mock_llm_calls':0,'llm_calls':0,'checks':[]}
 report=Path(__file__).parent/'cli-private-usage-checks.json'
 def save():report.write_text(json.dumps(evidence,indent=2),encoding='utf8')
 def checked(name,**data):
  evidence['checks'].append({'name':name,'kind':'actual_infrastructure',**data});save()
 async def measured(identifier):return (await usage.measure_user(identifier)).bytes
 try:
  other=body['other_user_id'];assert other!=user.id
  async with SessionLocal() as db:assert await db.get(User,other) is not None
  session=sessions.Session(body['case_id'],None,user.id);await session.start();await session.close()
  async with SessionLocal() as db:evidence['session']=records.public(await db.get(NativeCliSession,session.id))
  evidence['actual_exit']=identity.probe(evidence['session']['process']['root']['host'])
  assert evidence['session']['cleanup_verified'] and evidence['actual_exit']['state']=='exited'
  selected=root/'users'/str(user.id)/'sessions'/session.id
  assert session.input_root.parent==selected and selected.is_dir()
  checked('actual_cli_uses_user_namespaced_session_directory_and_exits')
  environment,other_workspace,_=await asyncio.to_thread(sessions.private_environment,other,str(uuid.uuid4()))
  _,pending_workspace,_=await asyncio.to_thread(sessions.private_environment,user.id,str(uuid.uuid4()))
  first=await measured(user.id);second=await measured(other)
  account=root/'users'/str(user.id)/'windows'/'tmp'/'usage.bin';account.write_bytes(os.urandom(8192))
  assert await measured(user.id)==first+8192 and await measured(other)==second
  first+=8192;checked('private_account_bytes_are_charged_only_to_their_user')
  pending=pending_workspace/'retained.bin';pending.write_bytes(os.urandom(4096))
  assert await measured(user.id)==first+4096;first+=4096
  checked('failed_before_owner_commit_session_state_remains_attributable')
  other_file=other_workspace/'other.bin';other_file.write_bytes(os.urandom(32768))
  assert await measured(other)==second+32768 and await measured(user.id)==first
  checked('other_user_session_bytes_do_not_change_first_user_usage')
  local=selected/'workspace'/'retained.bin';local.write_bytes(os.urandom(16384));first+=16384
  assert await measured(user.id)==first
  legacy=root/'sessions'/session.id;legacy.parent.mkdir(exist_ok=True)
  assert selected.resolve().is_relative_to(root.resolve()) and legacy.resolve().is_relative_to(root.resolve()) and not legacy.exists()
  os.rename(selected,legacy)
  assert await measured(user.id)==first and await measured(other)==second+32768
  checked('actual_legacy_directory_uses_persisted_session_creator_without_double_counting')
  orphan=root/'sessions'/str(uuid.uuid4());orphan.mkdir();(orphan/'workspace').mkdir()
  assert await measured(user.id)==first
  checked('proven_empty_legacy_directory_has_no_invented_owner_or_bytes')
  unknown_file=orphan/'workspace'/'unknown.bin';unknown_file.write_bytes(os.urandom(128))
  old=hashlib.sha256(unknown_file.read_bytes()).hexdigest()
  try:await measured(user.id)
  except usage.MeasurementUnavailable:pass
  else:raise AssertionError('Nonempty unknown legacy state was silently omitted')
  assert hashlib.sha256(unknown_file.read_bytes()).hexdigest()==old
  checked('unknown_nonempty_legacy_directory_refuses_measurement_and_preserves_data')
  unknown_file.unlink();unknown_file=None
  assert await measured(user.id)==first
  # Real quota row in this disposable database; unchanged developer plan.
  quota_file=pending_workspace/'quota.bin';quota_file.write_bytes(os.urandom(2*1024*1024))
  async with SessionLocal() as db:
   plan=await db.scalar(select(Plan).where(Plan.code=='free'));plan_id=plan.id;original_quotas=plan.quotas
   value=json.loads(plan.quotas);value['storage_mb']=1;plan.quotas=json.dumps(value);await db.commit()
  async with SessionLocal() as db:
   try:await quota.check_storage_quota(db,user,0)
   except HTTPException as error:assert error.status_code==429
   else:raise AssertionError('Actual private retained bytes did not block user quota')
  checked('actual_plan_quota_rejects_private_retained_bytes')
  async with SessionLocal() as db:
   plan=await db.get(Plan,plan_id);plan.quotas=original_quotas;await db.commit()
  original_quotas=None
  settings.storage_measurement_max_entries=1
  try:await asyncio.to_thread(budget.observe,private=True)
  except Exception as error:
   diagnostic=error.storage_observation
   assert getattr(error,'code',None)=='node_storage_unavailable' and diagnostic['reason']=='entry_limit'
   assert diagnostic['entries']>1 and diagnostic['elapsed_seconds']>=0 and diagnostic['phase'] in {'claims','public_inventory','private_inventory'}
   assert str(root) not in json.dumps(diagnostic)
  else:raise AssertionError('Actual scan entry limit unexpectedly admitted')
  settings.storage_measurement_max_entries=maximum
  checked('actual_scan_failure_records_bounded_phase_count_and_reason_without_paths',observation=diagnostic)
  evidence['passed']=True;save();return evidence
 except BaseException as error:
  evidence['failure_type']=type(error).__name__;save();raise
 finally:
  settings.storage_measurement_max_entries=maximum
  if unknown_file is not None and unknown_file.exists():unknown_file.unlink()
  if original_quotas is not None:
   async with SessionLocal() as db:
    plan=await db.get(Plan,plan_id);plan.quotas=original_quotas;await db.commit()
  if session is not None and not session.stopping:
   try:await session.close(error='AcceptanceCleanup')
   except Exception:pass
'''


def verify(api,**_):
    import httpx,uuid
    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
        'password':'isolated-test-123','name':'Private usage second user'})
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=180,trust_env=False) as client:
        response=client.post('/test/cli-private-usage',json={'case_id':api.case_id,'other_user_id':other['user']['id']})
        response.raise_for_status();result=response.json()
    assert result['passed'] and len(result['checks'])==9
    print('CLI_PRIVATE_USAGE_PASS '+str(api.root),flush=True)
    return result
