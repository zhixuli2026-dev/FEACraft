"""Actual installed CLI lifetime and storage faults; no model/RPC substitution."""
import httpx

FIXTURE=r'''
import asyncio,hashlib,json,os,time,uuid
from datetime import timezone
from pathlib import Path
from fastapi import Depends
from app.deps import get_current_user
from app.config import get_settings
from app.db import SessionLocal
from app.models import NativeCliSession
from app import storage
from app.services import codex_sessions as sessions,codex_records as records
from app.services import node_storage_budget as budget,process_identity_native as identity,request_deadline
from app.services.cli_private_paths import root as private_root

@app.post('/api/v1/test/cli-runtime-storage')
async def runtime_storage(body:dict,user=Depends(get_current_user)):
 settings=get_settings();limit=settings.node_storage_limit_mb;minimum=settings.node_storage_min_free_mb
 root=private_root(settings);session=None;filler=None;link=None
 evidence={'passed':False,'mock_llm_calls':0,'llm_calls':0,'checks':[],'sessions':[],'observations':[]}
 original_observe=budget.observe
 def observed(**kwargs):
  started=time.monotonic()
  try:
   result=original_observe(**kwargs)
   evidence['observations'].append({'elapsed':round(time.monotonic()-started,3),**result})
   return result
  except Exception as error:
   causes=[];current=error
   while current is not None:
    causes.append({'type':type(current).__name__,'errno':getattr(current,'errno',None),
      'detail':str(current) if type(current).__name__=='MeasurementUnavailable' else None})
    current=current.__cause__
   evidence['observations'].append({'elapsed':round(time.monotonic()-started,3),'causes':causes})
   raise
 budget.observe=observed
 path=Path(__file__).parent/'cli-runtime-checks.json'
 def save():path.write_text(json.dumps(evidence,indent=2),encoding='utf8')
 def checked(name,**data):
  evidence['checks'].append({'name':name,'kind':'actual_infrastructure',**data});save()
 async def row():
  async with SessionLocal() as db:return records.public(await db.get(NativeCliSession,session.id))
 async def retired(error):
  deadline=time.monotonic()+25
  while time.monotonic()<deadline:
   if session.retire_task is not None and session.retire_task.done():break
   await asyncio.sleep(.1)
  else:raise TimeoutError('Runtime monitor did not retire the actual CLI')
  value=await row();birth=value['process']['root']['host'];actual=identity.probe(birth)
  assert value['phase']==('failed' if error else 'closed') and value['error']==error,value
  assert value['cleanup_verified'] and actual['state']=='exited'
  assert session.storage_task.done() and session.heartbeat_task.done() and session.watch_task.done()
  evidence['sessions'].append({'session':value,'actual_exit':actual});save()
  return value
 try:
  settings.node_storage_min_free_mb=0
  # Keep a real session alive beyond its actual inherited 45-second startup
  # deadline. No clock substitution or fabricated CLI initialization result.
  session=sessions.Session(body['case_id'],None,user.id);started=time.monotonic()
  await session.start();initial=await row()
  await asyncio.sleep(max(0,request_deadline.CLI_STARTUP_SECONDS+2*sessions.HEARTBEAT_INTERVAL-(time.monotonic()-started)))
  current=await row()
  assert current['phase']=='active' and not session.storage_task.done() and not session.heartbeat_task.done()
  async with SessionLocal() as db:
   actual=await db.get(NativeCliSession,session.id)
   assert actual.heartbeat_at.replace(tzinfo=timezone.utc)>actual.created_at.replace(tzinfo=timezone.utc)
   assert actual.lease_expires_at.replace(tzinfo=timezone.utc)>records.now()
  checked('runtime_monitor_and_lease_survive_actual_startup_deadline',elapsed_seconds=round(time.monotonic()-started,2))
  await session.close();await retired(None)
  checked('normal_close_drains_storage_monitor_and_actual_process')

  session=sessions.Session(body['case_id'],None,user.id);await session.start()
  filler=session.input_root.parent/'runtime-growth.bin';filler.write_bytes(os.urandom(2*1024*1024))
  digest=hashlib.sha256(filler.read_bytes()).hexdigest()
  relative='Ref/runtime-retained-'+uuid.uuid4().hex+'.txt'
  retained=Path(session.selected['cwd'])/relative;retained.parent.mkdir(exist_ok=True)
  retained.write_bytes(b'ACTUAL_RUNTIME_LOCAL_EDIT')
  settings.node_storage_limit_mb=1
  await retired('node_storage_full')
  assert filler.exists() and hashlib.sha256(filler.read_bytes()).hexdigest()==digest
  assert retained.read_bytes()==b'ACTUAL_RUNTIME_LOCAL_EDIT'
  if storage._use_s3():
   assert session.pending_stage() and not storage.exists(session.selected['source_prefix'],relative)
  checked('actual_private_growth_stops_cli_and_retains_unsynced_files',retained_sha256=digest,
    s3_stage_retained=session.pending_stage() if storage._use_s3() else None)
  filler.unlink();filler=None;settings.node_storage_limit_mb=limit

  session=sessions.Session(body['case_id'],None,user.id);await session.start()
  outside=Path(__file__).parent/('runtime-outside-'+uuid.uuid4().hex);outside.mkdir()
  sentinel=outside/'sentinel';sentinel.write_bytes(b'UNCHANGED_OUTSIDE_RUNTIME_ROOT')
  link=root/('runtime-link-'+uuid.uuid4().hex);os.symlink(outside,link,target_is_directory=True)
  await retired('node_storage_unavailable')
  assert sentinel.read_bytes()==b'UNCHANGED_OUTSIDE_RUNTIME_ROOT'
  checked('unmeasurable_private_tree_stops_actual_cli_without_following_link')
  link.unlink();link=None
  assert not list((storage._fs_root()/budget.DIRECTORY).glob('*.json'))
  evidence['claims_empty']=True;evidence['passed']=True;save();return evidence
 except BaseException as error:
  evidence['failure_type']=type(error).__name__;save();raise
 finally:
  settings.node_storage_limit_mb=limit;settings.node_storage_min_free_mb=minimum
  if link is not None and link.is_symlink():link.unlink()
  if session is not None:
   try:await session.close(error='AcceptanceCleanup')
   except Exception:pass
  budget.observe=original_observe
'''


def verify(api,**_):
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                      timeout=180,trust_env=False) as client:
        response=client.post('/test/cli-runtime-storage',json={'case_id':api.case_id})
        response.raise_for_status();result=response.json()
    assert result['passed'] and len(result['checks'])==4 and len(result['sessions'])==3
    assert result['claims_empty']
    print('CLI_RUNTIME_STORAGE_PASS '+str(api.root),flush=True)
    return result
