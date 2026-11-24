"""Actual root-managed scopes and non-root installed CLI; no inference synthesis."""
FIXTURE=r'''
import asyncio,hashlib,json,os,subprocess,uuid
from pathlib import Path
from fastapi import Depends,HTTPException
from app.deps import get_current_user
from app.config import get_settings
from app.services import codex_sessions as sessions,native_scope_control as scopes
from app.services.codex_stdio import CodexStdioClient
from app.services.owned_process import wsl_path,probe_native
from app.services import process_identity_native as identity
_base=Path(__file__).parent
if (_base/'managed-native-root.json').exists():
 get_settings().codex_wsl_state_root=json.loads((_base/'managed-native-root.json').read_text())['root']

def durable(path,value):
 with path.open('x',encoding='utf8') as stream:
  json.dump(value,stream,indent=2);stream.flush();os.fsync(stream.fileno())

async def native_read(code,*arguments):
 result=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--exec','/usr/bin/python3','-I','-S','-c',code,*arguments],
  capture_output=True,text=True,encoding='utf8',timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
 assert result.returncode==0,{'returncode':result.returncode,'stderr':result.stderr[-2000:]}
 return json.loads(result.stdout)

@app.post('/api/v1/test/managed-cli-handoff')
async def managed_handoff(user=Depends(get_current_user)):
 root=Path(__file__).parent/'managed-handoff';root.mkdir()
 selected=await native_read("import json,tempfile,os; p=tempfile.mkdtemp(prefix='vibeflow-managed-cli-',dir='/tmp'); os.chown(p,65534,65534); print(json.dumps(p))")
 get_settings().codex_wsl_state_root=selected
 durable(_base/'managed-native-root.json',{'root':selected})
 report={'passed':False,'mock_llm_calls':0,'cli_model_inference_verified':False,
  'application_session_journal_integrated':False,'native_root':selected}
 client=None;allocation=None;prepared=None
 async def allocating(value):
  nonlocal prepared
  prepared=value;durable(root/'allocation-prepared.json',value)
 try:
  allocation=await scopes.control('vibeflow-linux','allocate',{'seed':uuid.uuid4().hex+uuid.uuid4().hex},on_prepared=allocating)
  durable(root/'allocated.json',allocation)
  environment,workspace,native_state=await asyncio.to_thread(sessions.private_environment,user.id,str(uuid.uuid4()),runtime='wsl',distro='vibeflow-linux')
  _,argv=await asyncio.to_thread(sessions.native_command,distro='vibeflow-linux')
  client=CodexStdioClient(argv,environment,cwd=workspace,distro='vibeflow-linux',native_state=native_state,native_scope=allocation['result'])
  owned=client.transport.owned
  await owned.prepare_guardian(root/'windows')
  async def admitted(value):
   assert value['native']['process']['uid']==0
   durable(root/'admitted.json',{'native_preflight':value,'guardian_request':owned.guardian.request,'guardian_preparation':owned.guardian.prepared})
  owned.on_native_preflight=admitted
  async def registered(value):durable(root/'root.json',value)
  async def bridge(value):
   assert value['uid']==65534;durable(root/'bridge.json',value)
  async def initialized():durable(root/'private-prepared.json',{'native_state':native_state})
  client.before_release=registered;client.before_start=bridge;client.after_prepare=initialized
  await client.start()
  assert client.initialize_result['platformOs']=='linux'
  assert owned.record['native']['process']['uid']==0 and client.transport.bridge_birth['uid']==65534
  bridge_pid=client.transport.bridge_birth['pid']
  status=await native_read("import json,pathlib,sys; p=pathlib.Path('/proc')/sys.argv[1]; v=dict(x.split(':',1) for x in (p/'status').read_text().splitlines() if ':' in x); print(json.dumps({'status':{k:v[k].strip() for k in ('Uid','Gid','Groups','NoNewPrivs','CapInh','CapPrm','CapEff','CapBnd','CapAmb')},'membership':(p/'cgroup').read_text()}))",str(bridge_pid))
  await client.transport.verify_bridge()
  state=status['status'];assert state['Uid'].split()==['65534']*4 and state['Gid'].split()==['65534']*4
  assert not state['Groups'] and state['NoNewPrivs']=='1'
  assert all(int(state[k],16)==0 for k in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb'))
  from app.services.process_cgroup_native import membership_path
  assert '0::'+membership_path(allocation['result']) in status['membership'].splitlines()
  report.update(initialize=client.initialize_result,root=owned.record,bridge=client.transport.bridge_birth,kernel=status)
  # Independently exercise the exact production privilege drop with real
  # kernel calls, then attempt to regain UID 0 in that same process.
  from app.services import cli_native_handoff
  regain=await native_read("import importlib.util,json,os,sys; s=importlib.util.spec_from_file_location('handoff',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.drop_privileges(); code=None\ntry: os.setuid(0)\nexcept PermissionError as e: code=e.errno\nprint(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'regain_errno':code}))",wsl_path(Path(cli_native_handoff.__file__).resolve()))
  assert regain=={'uid':65534,'gid':65534,'regain_errno':1};report['actual_regain_refusal']=regain
 finally:
  try:
   if client is not None:
    await client.close()
    report['stop']=client.stop_result;report['guardian']=client.transport.owned.guardian_proof
    if report['stop'] is not None:assert report['stop']['verified'] and report['guardian'] is not None
    if client.transport.bridge_birth is not None:
     report['bridge_exit']=await probe_native('vibeflow-linux',client.transport.bridge_birth)
     assert report['bridge_exit']['state']=='exited'
  finally:
   if allocation is not None:
    report['retired']=await scopes.control('vibeflow-linux','retire',{'scope':allocation['result']})
    assert report['retired']['result']['state']=='absent'
   elif prepared is not None:
    report['abandoned']=await scopes.control('vibeflow-linux','abandon',{'allocation_request':prepared['request'],'preparation':prepared['native']})
   durable(root/'observations.json',report)
 # The existing application Session path remains non-managed until its durable
 # scope lifecycle is integrated. Exercise it against the same actual home.
 ordinary=sessions.Session(None,None,user.id,runtime='wsl')
 try:await ordinary.start()
 finally:await ordinary.close()
 from app.db import SessionLocal
 from app.models import NativeCliSession
 from app.services import codex_records
 async with SessionLocal() as db:
  row=await db.get(NativeCliSession,ordinary.id);report['ordinary_session']=codex_records.public(row)
 assert report['ordinary_session']['cleanup_verified'] and report['ordinary_session']['process']['root']['native']['process']['uid']==65534
 report['passed']=True
 durable(root/'result.json',report)
 return report

handoff_tasks=set()
@app.post('/api/v1/test/managed-cli-crash')
async def managed_crash(user=Depends(get_current_user)):
 ready=asyncio.Event();release=asyncio.Event();original_close=CodexStdioClient.close
 async def held_close(client):
  if client.initialized:
   durable(_base/'managed-crash-owner.json',{'user_id':user.id,'owner':identity.observe(os.getpid())})
   ready.set();await release.wait()
  return await original_close(client)
 CodexStdioClient.close=held_close
 task=asyncio.create_task(managed_handoff(user));handoff_tasks.add(task);task.add_done_callback(handoff_tasks.discard)
 try:
  for _ in range(600):
   if ready.is_set() or task.done():break
   await asyncio.sleep(.1)
  assert ready.is_set(),'Managed CLI did not reach actual initialized boundary'
  return {'ready':True,'owner':identity.observe(os.getpid()),'user_id':user.id}
 except BaseException:
  release.set();task.cancel();await asyncio.gather(task,return_exceptions=True);raise
 finally:CodexStdioClient.close=original_close

@app.post('/api/v1/test/managed-cli-recover')
async def managed_recover(user=Depends(get_current_user)):
 root=_base/'managed-handoff'
 authority=json.loads((_base/'managed-crash-owner.json').read_text())
 if authority['user_id']!=user.id:raise HTTPException(404)
 owner_exit=identity.probe(authority['owner'])
 if owner_exit['state']!='exited':raise HTTPException(409,'Actual owner remains live')
 from app.services.sandbox_image_guardian import recover
 admission=json.loads((root/'admitted.json').read_text())
 selected={'request':admission['guardian_request'],'preparation':admission['guardian_preparation']}
 assert selected['request']['owner']==authority['owner']
 guardian=await recover(selected)
 allocation=json.loads((root/'allocated.json').read_text())
 retired=await scopes.control('vibeflow-linux','retire',{'scope':allocation['result']})
 assert retired['result']['state']=='absent'
 original=json.loads((root/'root.json').read_text());bridge=json.loads((root/'bridge.json').read_text())
 observations={'owner':owner_exit,'host':identity.probe(original['host']),
  'native':await probe_native('vibeflow-linux',original['native']['process']),
  'bridge':await probe_native('vibeflow-linux',bridge)}
 assert all(v['state']=='exited' for v in observations.values())
 report={'passed':True,'guardian':guardian,'retired':retired,'exits':observations,
  'original_stop_receipt_created':False,'application_session_journal_integrated':False,'mock_llm_calls':0}
 # Repeated recovery may observe an already absent scope; retain each real
 # controller receipt rather than inventing a normal CLI stop result.
 durable(root/('recovered-'+uuid.uuid4().hex+'.json'),report)
 return report
'''


def verify(api,**_):
    import httpx
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=240,trust_env=False) as client:
        response=client.post('/test/managed-cli-handoff');response.raise_for_status();result=response.json()
    assert result['passed'];print('MANAGED_CLI_HANDOFF_PASS '+str(api.root),flush=True);return result


def verify_crash(api,**_):
    import httpx,json,shutil
    user=api.call('GET','/auth/me')
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=90,trust_env=False) as client:
        response=client.post('/test/managed-cli-crash');response.raise_for_status();started=response.json()
    api.call('POST','/test/managed-cli-recover',expect=409)
    api.__exit__(RuntimeError,None,None);assert api.proc.poll() is not None
    shutil.copyfile(api.root/'api.log',api.root/'managed-api-crash.log')
    api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=120,trust_env=False) as client:
        response=client.post('/test/managed-cli-recover');response.raise_for_status();first=response.json()
        response=client.post('/test/managed-cli-recover');response.raise_for_status();second=response.json()
    assert first['passed'] and second['passed'] and first['guardian']==second['guardian']
    assert not second['retired']['result']['removed_this_call']
    result={'passed':True,'started':started,'first':first,'repeated':second,'mock_llm_calls':0}
    durable_path=api.root/'managed-api-crash-result.json'
    durable_path.write_text(json.dumps(result,indent=2),encoding='utf8')
    print('MANAGED_CLI_API_CRASH_PASS '+str(api.root),flush=True);return result
