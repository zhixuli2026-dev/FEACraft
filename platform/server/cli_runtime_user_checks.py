"""Real CLI processes, actual file growth, quota updates and unknown inventory."""
FIXTURE=r'''
import asyncio,hashlib,json,math,os,subprocess,time,uuid
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from app.config import get_settings
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Plan,NativeCliSession
from app.services import codex_sessions as sessions,codex_records as records,process_identity_native as identity
from app.services import cli_runtime_storage as runtime_usage,storage_usage
from app.services.cli_private_paths import root as host_root

@app.post('/api/v1/test/cli-runtime-user')
async def runtime_user_checks(body:dict,user=Depends(get_current_user)):
 settings=get_settings();native_before=settings.codex_wsl_state_root
 report={'passed':False,'mock_llm_calls':0,'checks':[],'sessions':[]}
 path=Path(__file__).parent/'cli-runtime-user-checks.json'
 def save():path.write_text(json.dumps(report,indent=2),encoding='utf8')
 def checked(name,**values):report['checks'].append({'name':name,'kind':'actual_infrastructure',**values});save()
 plan_id=None;original=None;current=None;unknown=None
 async def quota_value(mb):
  async with SessionLocal() as db:
   plan=await db.get(Plan,plan_id);values=json.loads(original);values['storage_mb']=mb;plan.quotas=json.dumps(values);await db.commit()
 async def restore():
  async with SessionLocal() as db:
   plan=await db.get(Plan,plan_id);plan.quotas=original;await db.commit()
 async def record(session):
  async with SessionLocal() as db:value=records.public(await db.get(NativeCliSession,session.id))
  assert value['cleanup_verified'] and identity.probe(value['process']['root']['host'])['state']=='exited'
  if session.runtime=='wsl':assert value['stop']['native_state']=='exited'
  report['sessions'].append(value);save();return value
 async def wait_close(session,code):
  until=time.monotonic()+40
  while session.close_task is None and time.monotonic()<until:await asyncio.sleep(.05)
  assert session.close_task is not None,'Actual user inventory did not close CLI'
  try:await session.close()
  except RuntimeError:pass
  row=await record(session)
  assert row['phase']=='failed' and row['error']==code
  return row
 try:
  async with SessionLocal() as db:
   plan=await db.scalar(select(Plan).where(Plan.code=='free'));plan_id=plan.id;original=plan.quotas
  result=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','-u','nobody','--exec','/usr/bin/python3','-c',
   "import tempfile; print(tempfile.mkdtemp(prefix='vibeflow-cli-user-runtime-',dir='/tmp'))"],
   capture_output=True,text=True,encoding='utf8',check=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
  selected=result.stdout.strip();assert selected.startswith('/tmp/vibeflow-cli-user-runtime-') and '/' not in selected[5:]
  settings.codex_wsl_state_root=selected;report['retained_native_test_root']=selected
  for runtime in ('windows','wsl'):
   current=sessions.Session(body['case_id'],None,user.id,runtime=runtime);await current.start()
   observed=await runtime_usage.observe(user.id)
   assert not current.stopping and observed['bytes']<=observed['limit_bytes']
   checked(runtime+'_actual_startup_passes_owner_inventory')
   other=(await storage_usage.measure_user(body['other_user_id'])).bytes
   folder=Path(current.selected['auth_home']).parent
   payload=folder/('runtime-growth-'+uuid.uuid4().hex+'.bin')
   await quota_value(math.ceil(observed['bytes']/1024/1024)+1)
   payload.write_bytes(os.urandom(3*1024*1024));digest=hashlib.sha256(payload.read_bytes()).hexdigest()
   started=time.monotonic();await wait_close(current,'cli_user_storage_full')
   assert hashlib.sha256(payload.read_bytes()).hexdigest()==digest
   assert (await storage_usage.measure_user(body['other_user_id'])).bytes==other
   checked(runtime+'_actual_user_growth_stops_cli_and_preserves_files',elapsed_seconds=round(time.monotonic()-started,3))
   await restore()
   # Create actual unknown legacy state: node totals can count it but no user
   # can silently claim zero ownership. The runtime must close conservatively.
   current=sessions.Session(body['case_id'],None,user.id,runtime=runtime);await current.start()
   unknown=host_root(settings)/'sessions'/str(uuid.uuid4());unknown.mkdir(parents=True)
   unknown_file=unknown/'retained.bin';unknown_file.write_bytes(os.urandom(8192))
   digest=hashlib.sha256(unknown_file.read_bytes()).hexdigest()
   await wait_close(current,'cli_user_storage_unavailable')
   assert hashlib.sha256(unknown_file.read_bytes()).hexdigest()==digest
   destination=host_root(settings)/'users'/str(user.id)/('test-retained-'+unknown.name)
   assert unknown.resolve().is_relative_to(host_root(settings).resolve())
   assert destination.parent.resolve().is_relative_to(host_root(settings).resolve()) and not destination.exists()
   until=time.monotonic()+5
   while True:
    try:unknown.rename(destination);break
    except PermissionError as error:
     if getattr(error,'winerror',None) not in {32,33} or time.monotonic()>=until:raise
     await asyncio.sleep(.05)
   unknown=None
   assert hashlib.sha256((destination/'retained.bin').read_bytes()).hexdigest()==digest
   checked(runtime+'_unknown_owner_inventory_closes_cli_without_deleting_retained_state')
   await restore()
  # The real native initializer completes, then actual external file growth
  # races the final launch gate. No stdio responses or model calls are replaced.
  current=sessions.Session(body['case_id'],None,user.id,runtime='wsl')
  original_prepared=current.state_prepared
  async def growing_before_launch():
   observed=await runtime_usage.observe(user.id)
   await quota_value(math.ceil(observed['bytes']/1024/1024)+1)
   (Path(current.selected['auth_home']).parent/'before-launch.bin').write_bytes(os.urandom(3*1024*1024))
   await original_prepared()
  current.state_prepared=growing_before_launch
  try:await current.start()
  except Exception as error:assert getattr(error,'code',None)=='cli_user_storage_full'
  else:raise AssertionError('Native CLI launched after owner quota overflow')
  row=await record(current)
  assert row['error']=='cli_user_storage_full' and current.client.transport.native_state_prepared
  assert not current.client.transport.started
  checked('actual_native_initializer_growth_rejected_before_cli_launch')
  await restore()
  # Actual persisted plan corruption must refuse observation, never assume
  # a permissive quota. Restore before subsequent real-model acceptance.
  async with SessionLocal() as db:
   plan=await db.get(Plan,plan_id);values=json.loads(original);values['storage_mb']='unknown';plan.quotas=json.dumps(values);await db.commit()
  try:await runtime_usage.observe(user.id)
  except Exception as error:assert getattr(error,'code',None)=='cli_user_storage_unavailable'
  else:raise AssertionError('Invalid persisted quota was accepted')
  await restore();checked('invalid_actual_plan_quota_refuses_runtime_observation')
  report['passed']=True;save();return report
 except BaseException as error:
  report['failure_type']=type(error).__name__;report['failure_code']=getattr(error,'code',None);save();raise
 finally:
  if original is not None:await restore()
  if current is not None:
   try:await current.close(error='AcceptanceCleanup')
   except Exception:pass
  if not report['passed']:settings.codex_wsl_state_root=native_before
'''


def verify(api,**_):
    import httpx,uuid
    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
        'password':'isolated-test-123','name':'Runtime quota second user'})
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=300,trust_env=False) as client:
        response=client.post('/test/cli-runtime-user',json={'case_id':api.case_id,'other_user_id':other['user']['id']})
        response.raise_for_status();result=response.json()
    assert result['passed'] and len(result['checks'])==8 and len(result['sessions'])==5
    print('CLI_RUNTIME_USER_PASS '+str(api.root),flush=True)
    return result
