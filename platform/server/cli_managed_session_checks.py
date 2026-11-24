"""Actual application Session admission/closure; installed CLI, no fake inference."""
FIXTURE=r'''
import asyncio,json,os,subprocess
from pathlib import Path
from fastapi import Depends
from app.deps import get_current_user
from app.config import get_settings
from app.db import SessionLocal
from app.models import NativeCliSession,CliScopeAllocation
from app.services import codex_sessions as sessions,codex_records as records,cli_scope_allocation as allocations
from app.services.workflow_state import ExecutionConflict
from app.services import process_identity_native as identity
_base=Path(__file__).parent
if (_base/'managed-session-native-root.json').exists():
 get_settings().codex_wsl_state_root=json.loads((_base/'managed-session-native-root.json').read_text())['root']

async def native_root():
 path=_base/'managed-session-native-root.json'
 if path.exists():return
 result=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--exec','/usr/bin/python3','-I','-S','-c',
  "import tempfile,os,json; p=tempfile.mkdtemp(prefix='vibeflow-managed-session-',dir='/tmp'); os.chown(p,65534,65534); print(json.dumps(p))"],
  capture_output=True,text=True,encoding='utf8',timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
 assert result.returncode==0
 selected=json.loads(result.stdout);get_settings().codex_wsl_state_root=selected
 with path.open('x',encoding='utf8') as stream:json.dump({'root':selected},stream);stream.flush();os.fsync(stream.fileno())

@app.post('/api/v1/test/managed-session-normal')
async def normal(body:dict,user=Depends(get_current_user)):
 await native_root()
 report={'passed':False,'sessions':[],'mock_llm_calls':0,'cli_model_inference_verified':False}
 for case_id in (None,body['case_id']):
  session=sessions.Session(case_id,None,user.id,runtime='wsl')
  try:
   await session.start()
   async with SessionLocal() as db:
    row=await db.get(NativeCliSession,session.id);active=records.public(row)
    allocation=await db.get(CliScopeAllocation,session.id)
    _,_,allocated,retired=allocations.document(allocation)
    assert retired=={'kind':'adopted','receipt':{'session_id':session.id}}
    assert active['phase']=='active' and active['process']['management']['allocation']==allocated
    assert active['process']['management']['guardian'] and active['process']['management']['preflight']
    assert active['process']['root']['native']['process']['uid']==0 and active['process']['bridge']['uid']==65534
   try:await allocations.retire(session.scope_owner)
   except ExecutionConflict as error:assert error.code=='recovery_required'
   else:raise AssertionError('Adopted scope was retired by pre-session recovery')
   # Read actual installed CLI account information, without invoking inference.
   account=await session.client.get_account()
   assert isinstance(account,dict)
  finally:await session.close()
  async with SessionLocal() as db:
   row=await db.get(NativeCliSession,session.id);closed=records.public(row)
   assert closed['phase']=='closed' and closed['cleanup_verified']
   management=closed['process']['management'];proof=management['retirement']
   assert proof['scope']['result']['state']=='absent' and proof['guardian']
   assert set(proof['births'])=={'host','native','bridge'} and all(v['state']=='exited' for v in proof['exits'].values())
   assert session.id not in sessions.SESSIONS and not session.pending_stage()
  report['sessions'].append({'case_id':case_id,'active':active,'closed':closed,'adopted_retirement_refused':True})
 report['passed']=True
 (_base/'managed-session-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 return report
'''


def verify(api,**_):
    import httpx
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=240,trust_env=False) as client:
        response=client.post('/test/managed-session-normal',json={'case_id':api.case_id})
        assert response.status_code==200,(response.status_code,response.text[:2000])
        result=response.json();assert result['passed']
    print('MANAGED_CLI_APPLICATION_SESSION_PASS '+str(api.root),flush=True)
    return result


CANCELLATION_FIXTURE=r'''
@app.post('/api/v1/test/managed-session-cancel/{stage}')
async def cancelled_session(stage:str,user=Depends(get_current_user)):
 from app.services import cli_scope_lifecycle as lifecycle
 from app.services.owned_process import OwnedProcess
 await native_root()
 assert stage in {'allocation_prepared','allocated','session_created','guardian_prepared','guardian_registered','native_registered','root_registered','bridge_registered','activated'}
 session=sessions.Session(None,None,user.id,runtime='wsl')
 ready=asyncio.Event();release=asyncio.Event();patches=[]
 def patch(obj,name,matcher):
  original=getattr(obj,name)
  async def wrapped(*args,**kwargs):
   result=await original(*args,**kwargs)
   if matcher(args,kwargs):ready.set();await release.wait()
   return result
  setattr(obj,name,wrapped);patches.append((obj,name,original))
 if stage=='allocation_prepared':patch(allocations,'record',lambda a,k:a[0].id==session.id and a[1]=='prepared_json')
 elif stage=='allocated':patch(allocations,'allocate',lambda a,k:a[0].id==session.id)
 elif stage=='session_created':patch(records,'create',lambda a,k:a[0]==session.id)
 elif stage=='guardian_prepared':patch(OwnedProcess,'prepare_guardian',lambda a,k:session.client is not None and a[0] is session.client.transport.owned)
 elif stage in {'guardian_registered','native_registered'}:
  patch(lifecycle,'register',lambda a,k:a[1].id==session.id and a[2]==('guardian' if stage=='guardian_registered' else 'preflight'))
 elif stage=='root_registered':patch(records,'register_root',lambda a,k:a[1].id==session.id)
 elif stage=='bridge_registered':patch(records,'register_bridge',lambda a,k:a[1].id==session.id)
 else:patch(records,'activate',lambda a,k:a[1].id==session.id)
 start=asyncio.create_task(session.start());closing=None
 try:
  for _ in range(700):
   if ready.is_set() or start.done():break
   await asyncio.sleep(.05)
  if start.done():await start
  assert ready.is_set(),'Actual Session boundary was not reached'
  closing=asyncio.create_task(session.close())
  await asyncio.sleep(.1)
  release.set()
  await asyncio.wait_for(asyncio.gather(start,closing,return_exceptions=True),80)
  assert start.done() and closing.done()
  if session.client is not None and session.client.transport.owned.proc is not None:
   assert session.client.transport.owned.proc.returncode is not None
  async with SessionLocal() as db:
   allocation=await db.get(CliScopeAllocation,session.id)
   _,prepared,allocated,retired=allocations.document(allocation)
   row=await db.get(NativeCliSession,session.id)
   value=records.public(row) if row is not None else None
   if row is None:assert retired['kind'] in {'abandon','retire','unused'}
   else:
    assert retired=={'kind':'adopted','receipt':{'session_id':session.id}}
    assert value['phase'] in {'closed','failed'} and value['cleanup_verified']
    assert value['process']['management']['retirement']['scope']['result']['state']=='absent'
    from app.models import CliInitializationReservation
    from app.services.cli_initialization_capacity import settlement_ticket
    credit=await db.get(CliInitializationReservation,session.id)
    assert credit.state=='settled' and credit.remaining_bytes==0
    ticket=settlement_ticket(credit,row)
    from app.services.cli_node_capacity import outstanding
    assert ticket is None or not await asyncio.to_thread(outstanding,ticket)
   result={'stage':stage,'allocation_retirement':retired,'session':value,'start_error':type(start.exception()).__name__ if not start.cancelled() and start.exception() else 'CancelledError' if start.cancelled() else None,
    'close_error':type(closing.exception()).__name__ if not closing.cancelled() and closing.exception() else None,'cleanup_verified':session.cleanup_verified}
   assert session.cleanup_verified and session.id not in sessions.SESSIONS
  (_base/('managed-session-cancel-'+stage+'.json')).write_text(json.dumps(result,indent=2),encoding='utf8')
  return result
 finally:
  release.set()
  for obj,name,original in reversed(patches):setattr(obj,name,original)
  if not start.done():start.cancel()
  await asyncio.gather(start,return_exceptions=True)
  if closing is not None:await asyncio.gather(closing,return_exceptions=True)
'''


def verify_cancellation(api,**_):
    import httpx,json
    result={'passed':False,'mock_llm_calls':0,'stages':[]}
    path=api.root/'managed-session-cancellation-checks.json'
    try:
        for stage in ('allocation_prepared','allocated','session_created','guardian_prepared','guardian_registered','native_registered','root_registered','bridge_registered','activated'):
            with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=150,trust_env=False) as client:
                response=client.post('/test/managed-session-cancel/'+stage)
                assert response.status_code==200,(stage,response.status_code,response.text[:2000])
                result['stages'].append(response.json())
            path.write_text(json.dumps(result,indent=2),encoding='utf8');print('MANAGED_SESSION_CANCEL_PASS '+stage,flush=True)
        result['passed']=True;return result
    finally:path.write_text(json.dumps(result,indent=2),encoding='utf8')


UNKNOWN_FIXTURE=r'''
@app.post('/api/v1/test/managed-session-unknown-native')
async def unknown_native(user=Depends(get_current_user)):
 from app.services.owned_process import OwnedProcess,probe_native
 from app.services import native_scope_control as scopes
 from app.models import CliInitializationReservation
 await native_root()
 session=sessions.Session(None,None,user.id,runtime='wsl')
 original=OwnedProcess.hello;ready=asyncio.Event();release=asyncio.Event();observed=None
 async def held(owned,role,*args,**kwargs):
  nonlocal observed
  value=await original(owned,role,*args,**kwargs)
  if role=='wsl_prepared' and session.client is not None and owned is session.client.transport.owned:
   observed=value;ready.set();await release.wait()
  return value
 OwnedProcess.hello=held
 start=asyncio.create_task(session.start());closing=None
 try:
  for _ in range(700):
   if ready.is_set() or start.done():break
   await asyncio.sleep(.05)
  if start.done():await start
  assert ready.is_set()
  closing=asyncio.create_task(session.close());await asyncio.sleep(.1);release.set()
  await asyncio.wait_for(asyncio.gather(start,closing,return_exceptions=True),80)
  async with SessionLocal() as db:
   row=await db.get(NativeCliSession,session.id);value=records.public(row)
   credit=await db.get(CliInitializationReservation,session.id)
   assert value['phase']=='failed' and value['cleanup_verified'] is False and value['stop'] is None
   assert value['process']['management']['preflight'] is None and value['process']['management']['retirement'] is None
   assert credit.state=='active' and credit.remaining_bytes==credit.size
  absence=await scopes.control(session.distro,'observe',{'scope':session.native_scope})
  actual_exit=await probe_native(session.distro,observed)
  assert absence['result']['state']=='absent' and actual_exit['state']=='exited'
  result={'passed':True,'session':value,'actual_test_observed_birth':observed,'actual_exit':actual_exit,
   'scope_absence':absence,'credit_retained':True,'test_birth_not_injected_into_production_journal':True,'mock_llm_calls':0}
  (_base/'managed-session-unknown-native.json').write_text(json.dumps(result,indent=2),encoding='utf8')
  return result
 finally:
  release.set();OwnedProcess.hello=original
  if not start.done():start.cancel()
  await asyncio.gather(start,return_exceptions=True)
  if closing is not None:await asyncio.gather(closing,return_exceptions=True)
'''


def verify_unknown(api,**_):
    import httpx
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=150,trust_env=False) as client:
        response=client.post('/test/managed-session-unknown-native')
        assert response.status_code==200,(response.status_code,response.text[:2000])
        value=response.json();assert value['passed'];return value
