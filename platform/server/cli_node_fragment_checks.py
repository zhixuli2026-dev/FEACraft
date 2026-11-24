"""Actual mid-write API crashes and preserved native node metadata fragments."""
FIXTURE=r'''
import asyncio,hashlib,json,os,subprocess,threading,uuid
from pathlib import Path
from fastapi import Depends
from app.deps import get_current_user
from app.db import SessionLocal
from app.config import get_settings
from app.models import NativeCliSession,CliInitializationReservation
from app.services import codex_sessions as sessions,codex_records as records
from app.services import cli_initialization_capacity as capacity,cli_node_capacity as nodes
from app.services import node_storage_budget as budget,process_identity_native as identity,file_transactions
from app.services.case_write import CaseWriteConflict

root=Path(__file__).parent
native_config=root/'fragment-native-root.json'
if native_config.exists():get_settings().codex_wsl_state_root=json.loads(native_config.read_text(encoding='utf8'))['root']
operations=set()

@app.post('/api/v1/test/node-fragment-start/{stage}')
async def fragment_start(stage:str,user=Depends(get_current_user)):
 assert stage in {'empty','partial','flushed','published','legacy_partial'}
 if not native_config.exists():
  result=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','-u','nobody','--exec','/usr/bin/python3','-c',
   "import tempfile; print(tempfile.mkdtemp(prefix='vibeflow-cli-fragment-',dir='/tmp'))"],
   capture_output=True,text=True,encoding='utf8',check=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
  selected=result.stdout.strip();assert selected.startswith('/tmp/vibeflow-cli-fragment-') and '/' not in selected[5:]
  get_settings().codex_wsl_state_root=selected;native_config.write_text(json.dumps({'root':selected}),encoding='utf8')
 session=sessions.Session(None,None,user.id,runtime='wsl')
 ready=threading.Event();release=threading.Event();captured={}
 original_write=file_transactions._write_new;original_rename=nodes.os.rename
 def pause(path,data):
  captured.update(path=str(path),size=len(data),sha256=hashlib.sha256(data).hexdigest());ready.set()
  if not release.wait(40):raise TimeoutError('Actual node publication crash was not delivered')
 def write(path,data):
  if path.name.startswith(session.id.replace('-','')+'.') and path.name.endswith('.cli-pending'):
   if stage=='published':return original_write(path,data)
   count=0 if stage=='empty' else len(data) if stage=='flushed' else len(data)//2
   actual=path.with_name(session.id.replace('-','')+'.json') if stage=='legacy_partial' else path
   # Legacy placement is an explicit actual filesystem fault representing the
   # earlier direct-to-final writer. Current production always writes pending.
   with actual.open('xb') as stream:
    stream.write(data[:count]);stream.flush();os.fsync(stream.fileno())
   pause(actual,data[:count]);return
  return original_write(path,data)
 def rename(source,target,*args,**kwargs):
  result=original_rename(source,target,*args,**kwargs)
  if stage=='published' and Path(source).name.startswith(session.id.replace('-','')+'.') and str(source).endswith('.cli-pending'):
   pause(Path(target),Path(target).read_bytes())
  return result
 file_transactions._write_new=write;nodes.os.rename=rename
 async def start():
  try:await session.start()
  except BaseException as error:captured['failure_type']=type(error).__name__;ready.set()
 task=asyncio.create_task(start());operations.add(task);task.add_done_callback(operations.discard)
 try:
  async with asyncio.timeout(40):
   while not ready.is_set():await asyncio.sleep(.02)
  assert 'failure_type' not in captured,captured
  async with SessionLocal() as db:
   row=await db.get(NativeCliSession,session.id);credit=await db.get(CliInitializationReservation,session.id)
   ticket=capacity.ticket(credit,row)
   assert credit.state=='active' and credit.remaining_bytes==credit.size and row.process_json=='{}' and row.stop_json=='{}'
  assert session.client.transport.owned.proc is None
  report={'stage':stage,'session_id':session.id,'user_id':user.id,'ticket':ticket,'fragment':captured,
   'home':session.selected['auth_home'],'process_was_created':False,'mock_llm_calls':0,
   'legacy_placement_fault':stage=='legacy_partial'}
  assert not Path(report['home']).exists()
  (root/('node-fragment-'+stage+'.json')).write_text(json.dumps(report,indent=2),encoding='utf8')
  return report
 except BaseException:
  release.set();task.cancel();await asyncio.gather(task,return_exceptions=True);raise

def report_for(stage,user):
 report=json.loads((root/('node-fragment-'+stage+'.json')).read_text(encoding='utf8'))
 assert report['user_id']==user.id
 path=Path(report['fragment']['path'])
 assert path.parent==Path(report['ticket']['node_root'])/budget.DIRECTORY
 assert path.name.startswith(report['session_id'].replace('-','')+'.')
 return report,path

@app.post('/api/v1/test/node-fragment-observe/{stage}')
async def fragment_observe(stage:str,user=Depends(get_current_user)):
 report,path=report_for(stage,user)
 assert identity.probe(report['ticket']['owner'])['state']=='exited'
 assert hashlib.sha256(path.read_bytes()).hexdigest()==report['fragment']['sha256']
 async with SessionLocal() as db:
  row=await db.get(NativeCliSession,report['session_id']);credit=await db.get(CliInitializationReservation,report['session_id'])
  assert row.process_json=='{}' and row.stop_json=='{}' and credit.state=='active'
 try:observation=await asyncio.to_thread(budget.observe,private=True)
 except CaseWriteConflict as error:
  assert stage=='legacy_partial';observation={'available':False,'code':error.code}
 else:
  assert observation['reserved_bytes']==(report['ticket']['bytes'] if stage=='published' else 32768)
 return observation

@app.post('/api/v1/test/node-fragment-other-user')
async def other_user(user=Depends(get_current_user)):
 await capacity.closed(user.id)
 return {'ok':True}

@app.post('/api/v1/test/node-fragment-alter/{stage}/{action}')
async def alter_fragment(stage:str,action:str,user=Depends(get_current_user)):
 report,path=report_for(stage,user)
 _,_,expected=nodes.encoded(report['ticket']);original=expected[:report['fragment']['size']]
 assert hashlib.sha256(original).hexdigest()==report['fragment']['sha256']
 assert action in {'foreign','restore'}
 path.write_bytes(b'foreign-node-metadata' if action=='foreign' else original)
 return {'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'actual_filesystem_fault':True}

@app.get('/api/v1/test/node-fragment-final/{stage}')
async def fragment_final(stage:str,user=Depends(get_current_user)):
 report,original=report_for(stage,user)
 assert not original.exists()
 async with SessionLocal() as db:
  row=await db.get(NativeCliSession,report['session_id']);credit=await db.get(CliInitializationReservation,report['session_id'])
  assert credit.state=='settled' and credit.remaining_bytes==0
  assert json.loads(credit.receipt_json)['status']=='initializer_not_released'
  assert row.process_json=='{}' and row.stop_json=='{}'
  summary=records.public(row)
 parent,identifier,encoded=nodes.encoded(report['ticket'])
 if stage=='published':retired=None
 else:
  kind='published' if stage=='legacy_partial' else 'pending'
  path=parent/budget.DIRECTORY/(identifier+'.'+hashlib.sha256(encoded).hexdigest()+'.cli-retired-'+kind)
  assert path.read_bytes()==encoded[:report['fragment']['size']]
  assert hashlib.sha256(path.read_bytes()).hexdigest()==report['fragment']['sha256']
  retired={'path':str(path),'sha256':report['fragment']['sha256'],'size':path.stat().st_size}
 assert not Path(report['home']).exists()
 observed=await asyncio.to_thread(budget.observe,private=True)
 assert observed['reserved_bytes']==0
 assert not await asyncio.to_thread(nodes.outstanding,report['ticket'])
 return {'passed':True,'session':summary,'retired':retired,'node':observed}

@app.post('/api/v1/test/node-fragment-normal')
async def fragment_normal(user=Depends(get_current_user)):
 session=sessions.Session(None,None,user.id,runtime='wsl')
 try:
  await session.start();assert session.client.initialize_result['platformOs']=='linux'
 finally:await session.close()
 async with SessionLocal() as db:
  row=await db.get(NativeCliSession,session.id);credit=await db.get(CliInitializationReservation,session.id)
  result=records.public(row);assert result['cleanup_verified']
  assert credit.state=='settled' and json.loads(credit.receipt_json)['status']=='prepared'
 assert (await asyncio.to_thread(budget.observe,private=True))['reserved_bytes']==0
 return {'passed':True,'actual_cli':True,'session':result}
'''


def verify(api,**_):
    import concurrent.futures,contextlib,httpx,json,os,shutil,socket,subprocess,sys,time,uuid
    from testsupport import SERVER
    evidence={'passed':False,'mock_llm_calls':0,'checks':[],'stages':[],'peers':[],'filesystem_faults':[]}
    destination=api.root/'cli-node-fragment-checks.json'
    def save():destination.write_text(json.dumps(evidence,indent=2),encoding='utf8')
    def checked(name,stage):evidence['checks'].append({'name':name,'stage':stage,'kind':'actual_infrastructure'});save()
    @contextlib.contextmanager
    def peer(stage):
        with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        assert port!=8000;base='http://127.0.0.1:'+str(port)
        with (api.root/('peer-'+stage+'.log')).open('w',encoding='utf8') as log:
            process=subprocess.Popen([sys.executable,'-B','-m','uvicorn','live_fixture:app','--app-dir',str(SERVER),
                '--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=api.root,env=dict(api.env),
                stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:
                with httpx.Client(trust_env=False,timeout=1) as client:
                    for _ in range(150):
                        assert process.poll() is None
                        try:
                            if client.get(base+'/api/health').json().get('ok'):break
                        except (httpx.HTTPError,ValueError):pass
                        time.sleep(.1)
                    else:raise TimeoutError('Fragment recovery peer did not start')
                yield base
            finally:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                evidence['peers'].append({'api':base,'pid':process.pid,'exited':process.poll() is not None});save()
    for stage in ('empty','partial','flushed','published','legacy_partial'):
        api.create_case('Actual node fragment '+stage);user=api.call('GET','/auth/me')
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=60,trust_env=False) as client:
            response=client.post('/test/node-fragment-start/'+stage);response.raise_for_status();before=response.json()
        route='/codex/sessions/'+before['session_id']+':reconcile-initialization'
        api.call('POST',route,expect=409)
        api.__exit__(RuntimeError,None,None);assert api.proc.poll() is not None
        shutil.copyfile(api.root/'api.log',api.root/('api-crash-'+stage+'.log'))
        api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
        observation=api.call('POST','/test/node-fragment-observe/'+stage)
        checked('actual_partial_publication_survives_owner_exit_and_preserves_credit',stage)
        token=api.token
        other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'Other node user','password':'isolated-test-123'})
        api.token=other['token'];api.call('POST',route,expect=404)
        if stage!='legacy_partial':api.call('POST','/test/node-fragment-other-user')
        api.token=token
        assert api.call('POST','/test/node-fragment-observe/'+stage)==observation
        checked('other_user_cannot_recover_or_reconcile_foreign_fragment',stage)
        if stage=='partial':
            foreign=api.call('POST','/test/node-fragment-alter/partial/foreign')
            api.call('POST',route,expect=502)
            assert hashlib_sha(before['fragment']['path'])==foreign['sha256']
            api.call('POST','/test/node-fragment-alter/partial/restore')
            evidence['filesystem_faults'].append({'stage':stage,'name':'foreign_content_preserved_and_refused','kind':'actual_filesystem_fault'})
        def recover(base):
            with httpx.Client(base_url=base+'/api/v1',headers={'Authorization':'Bearer '+token},timeout=90,trust_env=False) as client:
                for _ in range(5):
                    result=client.post(route)
                    if result.status_code==409 and result.json().get('detail',{}).get('code')=='storage_quota_busy':time.sleep(.1);continue
                    result.raise_for_status();return result.json()
                raise AssertionError('Quota recovery did not drain')
        with peer(stage) as second:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                jobs=[pool.submit(recover,base) for base in (api.base,second)];first,repeated=[job.result() for job in jobs]
        assert first['receipt']==repeated['receipt'] and first['state']=='settled' and not first['node_claim_pending']
        assert first['session_cleanup_verified'] is None
        assert api.call('POST',route)['receipt']==first['receipt']
        final=api.call('GET','/test/node-fragment-final/'+stage)
        evidence['stages'].append({'before':before,'observation':observation,'recovered':first,'final':final})
        if stage=='legacy_partial':evidence['filesystem_faults'].append({'stage':stage,'name':'actual_legacy_final_file_partial_write','kind':'actual_filesystem_fault'})
        checked('two_workers_retire_only_bound_fragments_with_original_bytes_retained',stage)
        print('CLI_NODE_FRAGMENT_PASS '+stage,flush=True)
    api.create_case('Actual CLI after node fragment recovery')
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=90,trust_env=False) as client:
        response=client.post('/test/node-fragment-normal');response.raise_for_status();evidence['normal']=response.json()
    checked('actual_linux_cli_initializes_and_closes_with_retired_fragment_inventory','normal')
    evidence['passed']=True;save();return evidence


def hashlib_sha(path):
    import hashlib
    from pathlib import Path
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
