"""Real API termination while its registered native bridge writes private state."""
STAGES=('pending_created','owner_partial','owner_ready','config_partial','config_ready','owner_published','home_published')

# Appended to an isolated copy of the production initializer, after its
# definitions. Only real I/O boundaries pause; no RPC/model output is replaced.
NATIVE_HOOK=r'''
_control=Path(__file__).with_name('crash-control.json')
if _control.exists():
    _selected=json.loads(_control.read_text(encoding='utf8'))
    _stage=_selected['stage']
    def _pause_native():
        report={'stage':_stage,'pid':os.getpid(),'uid':os.getuid()}
        Path(__file__).with_name('ready-'+_stage+'.json').write_text(json.dumps(report),encoding='utf8')
        time.sleep(60)
        raise TimeoutError('Isolated API crash was not delivered')
    if _stage in {'owner_partial','config_partial'}:
        _original_write=os.write
        def _held_write(fd,data):
            name=os.readlink('/proc/self/fd/'+str(fd))
            if '.initialize-' in name and name.endswith('/'+('owner.json' if _stage=='owner_partial' else 'config.toml')):
                cut=max(1,len(data)//2)
                assert _original_write(fd,data[:cut])==cut
                os.fsync(fd);_pause_native()
            return _original_write(fd,data)
        os.write=_held_write
    elif _stage in {'owner_ready','config_ready'}:
        _original_resume=_resume_file
        def _held_resume(path,data):
            _original_resume(path,data)
            if path.name==('owner.json' if _stage=='owner_ready' else 'config.toml'):_pause_native()
        _resume_file=_held_resume
    elif _stage in {'owner_published','home_published'}:
        _original_publish=_publish
        def _held_publish(source,target):
            _original_publish(source,target)
            if target.name==('owner.json' if _stage=='owner_published' else 'home'):_pause_native()
        _publish=_held_publish
    elif _stage=='pending_created':
        _original_sync=_sync
        def _held_sync(directory):
            _original_sync(directory)
            if not directory.name.startswith('.initialize-') and list(directory.glob('.initialize-*')):_pause_native()
        _sync=_held_sync
    else:raise ValueError('Unknown isolated crash boundary')
'''

from cli_initialization_recovery_checks import FIXTURE as RECOVERY

EXTENSION=r'''
from app.services import owned_stdio
from app.services.codex_wsl_state import initialization_payload
from cli_native_api_crash_checks import NATIVE_HOOK,STAGES

# Keep production source untouched. Both the actual launch and later recovery
# freeze/check these same copies, including the explicit pause instrumentation.
transport_root=test_root/'native-crash-transport'
transport_root.mkdir(exist_ok=True)
for label in ('BRIDGE','IDENTITY','NATIVE_PROBE','NATIVE_STATE'):
 original=getattr(owned_stdio,label)
 expected=original.read_bytes()+(NATIVE_HOOK.encode('utf8') if label=='NATIVE_STATE' else b'')
 destination=transport_root/original.name
 if destination.exists():assert destination.read_bytes()==expected
 else:
  with destination.open('xb') as stream:stream.write(expected)
 setattr(owned_stdio,label,destination);setattr(sessions,label,destination)

def native_snapshot(home):
 account=Path(home).parent;result={}
 if not account.exists():return result
 for index,path in enumerate(account.rglob('*')):
  assert index<64 and not path.is_symlink()
  if path.is_file():
   assert path.stat().st_size<=32768
   data=path.read_bytes()
   result[path.relative_to(account).as_posix()]={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
 return result

@app.post('/api/v1/test/native-api-crash/{stage}')
async def native_api_crash(stage:str,user=Depends(get_current_user)):
 assert stage in STAGES
 if not native_config.exists():
  result=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','-u','nobody','--exec','/usr/bin/python3','-c',
   "import tempfile; print(tempfile.mkdtemp(prefix='vibeflow-cli-native-api-',dir='/tmp'))"],
   capture_output=True,text=True,encoding='utf8',check=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
  selected=result.stdout.strip();assert selected.startswith('/tmp/vibeflow-cli-native-api-') and '/' not in selected[5:]
  get_settings().codex_wsl_state_root=selected
  native_config.write_text(json.dumps({'root':selected}),encoding='utf8')
 control=transport_root/'crash-control.json'
 control.write_text(json.dumps({'stage':stage}),encoding='utf8')
 marker=transport_root/('ready-'+stage+'.json');assert not marker.exists()
 session=sessions.Session(None,None,user.id,runtime='wsl')
 task=asyncio.create_task(session.start());startup_tasks.add(task);task.add_done_callback(startup_tasks.discard)
 try:
  until=time.monotonic()+35
  while not marker.exists() and not task.done() and time.monotonic()<until:await asyncio.sleep(.02)
  assert marker.exists(),{'boundary':stage,'task_done':task.done()}
  # A completed marker write is read after the native writer has entered its
  # bounded pause. No SQL transaction remains held by the fixture.
  for attempt in range(20):
   try:native=json.loads(marker.read_text(encoding='utf8'));break
   except ValueError:await asyncio.sleep(.01)
  else:raise AssertionError('Incomplete native boundary marker')
  report=await capture(session,'prepared' if stage=='home_published' else 'bridge')
  report.update(stage=stage,native_boundary=native,fragments=native_snapshot(report['home']),native_selection=session.native_state)
  assert native['uid']==65534 and report['journal']['bridge'] is not None
  assert not report['transport_started'] and report['credit_state']=='active'
  assert sum(item['size'] for item in report['fragments'].values())<=report['ticket']['bytes']
  (test_root/('crash-'+stage+'.json')).write_text(json.dumps(report,indent=2),encoding='utf8')
  return report
 except BaseException:
  task.cancel();await asyncio.gather(task,return_exceptions=True);raise

@app.post('/api/v1/test/native-api-preserved/{stage}')
async def native_api_preserved(stage:str,user=Depends(get_current_user)):
 report=await after_crash(stage,user)
 assert native_snapshot(report['home'])==report['fragments']
 return report

@app.post('/api/v1/test/native-api-restart/{stage}')
async def native_api_restart(stage:str,user=Depends(get_current_user)):
 report=json.loads((test_root/('crash-'+stage+'.json')).read_text(encoding='utf8'))
 assert report['user_id']==user.id
 assert native_snapshot(report['home'])==report['fragments']
 control=transport_root/'crash-control.json'
 assert json.loads(control.read_text(encoding='utf8'))=={'stage':stage}
 control.unlink() # Only this isolated fixture's control; native data is kept.
 session=sessions.Session(None,None,user.id,runtime='wsl')
 try:
  await session.start()
  assert session.native_state==report['native_selection']
  assert session.client.initialize_result['platformOs']=='linux'
 finally:await session.close()
 account=Path(report['home']).parent
 configuration,owner=initialization_payload(report['native_selection'])
 assert (account/'home/config.toml').read_bytes()==configuration
 assert (account/'owner.json').read_bytes()==owner
 assert not list(account.glob('.initialize-*'))
 async with SessionLocal() as db:
  row=await db.get(NativeCliSession,session.id);credit=await db.get(CliInitializationReservation,session.id)
  result={'session':records.public(row),'receipt':json.loads(credit.receipt_json),'initialization_bytes':credit.size}
  assert credit.state=='settled' and credit.remaining_bytes==0 and result['receipt']['status']=='prepared'
  assert result['session']['cleanup_verified'] and result['session']['stop']['native_state']=='exited'
 result.update(same_account_restarted=True,exact_configuration_preserved=True,pending_removed=True)
 return result
'''
FIXTURE=RECOVERY+EXTENSION


def verify(api,**_):
    import concurrent.futures,contextlib,httpx,json,os,shutil,socket,subprocess,sys,time,uuid
    from testsupport import SERVER
    result={'passed':False,'stages':[],'peers':[],'mock_llm_calls':0,'cli_model_inference_verified':False,
        'fault_injection':'isolated hashed source copy; actual file writes paused; actual API termination'}
    def save():
        (api.root/'native-api-crash-checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    original_call=api.call
    def traced_call(method,path,*args,**kwargs):
        started=time.monotonic()
        record={'method':method,'path':path,'started_at':time.time()}
        def trace():
            with (api.root/'native-api-http.jsonl').open('a',encoding='utf8') as stream:
                stream.write(json.dumps(record)+'\n')
        record['state']='started';trace()
        try:
            value=original_call(method,path,*args,**kwargs)
        except BaseException as error:
            record.update(state='failed',error=type(error).__name__,elapsed_seconds=time.monotonic()-started);trace();raise
        record.update(state='completed',elapsed_seconds=time.monotonic()-started);trace();return value
    api.call=traced_call
    def account(name):
        # These sessions intentionally have case_id=None. Create only the real
        # account they require; the final real Agent proof still creates a case.
        auth=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
            'password':'isolated-test-123','name':name})
        api.token=auth['token']
    @contextlib.contextmanager
    def peer(stage):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        assert port!=8000
        base='http://127.0.0.1:'+str(port)
        with (api.root/('native-peer-'+stage+'.log')).open('w',encoding='utf8') as log:
            proc=subprocess.Popen([sys.executable,'-B','-m','uvicorn','live_fixture:app','--app-dir',str(SERVER),
                '--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=api.root,env=dict(api.env),
                stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:
                with httpx.Client(trust_env=False,timeout=1) as client:
                    for _ in range(150):
                        assert proc.poll() is None,'Independent native recovery API exited'
                        try:
                            if client.get(base+'/api/health').json().get('ok'):break
                        except (httpx.HTTPError,ValueError):pass
                        time.sleep(.1)
                    else:raise TimeoutError('Independent native recovery API unavailable')
                yield base
            finally:
                proc.terminate()
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
                result['peers'].append({'stage':stage,'pid':proc.pid,'api':base,'exited':proc.poll() is not None});save()
    for stage in STAGES:
        account('Native API partial-write crash '+stage)
        user=api.call('GET','/auth/me')
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=60,trust_env=False) as client:
            response=client.post('/test/native-api-crash/'+stage);response.raise_for_status();before=response.json()
        route='/codex/sessions/'+before['session_id']+':reconcile-initialization'
        assert api.call('POST',route,expect=409)['detail']['code']=='recovery_required'
        killed_pid=api.proc.pid
        api.__exit__(RuntimeError,None,None);assert api.proc.poll() is not None
        shutil.copyfile(api.root/'api.log',api.root/('native-api-crash-'+stage+'.log'))
        api.__enter__()
        api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
        after=api.call('POST','/test/native-api-preserved/'+stage)
        token=api.token
        account('Unauthorized native recovery '+stage);api.call('POST',route,expect=404);api.token=token
        def recover(base):
            with httpx.Client(base_url=base+'/api/v1',headers={'Authorization':'Bearer '+token},timeout=90,trust_env=False) as client:
                statuses=[]
                for attempt in range(5):
                    response=client.post(route);statuses.append(response.status_code)
                    if response.status_code==409 and response.json().get('detail',{}).get('code')=='storage_quota_busy':
                        time.sleep(.1);continue
                    response.raise_for_status();return {'value':response.json(),'statuses':statuses,'api':base}
                raise AssertionError('Native recovery owner lock remained busy')
        with peer(stage) as other:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                requests=[pool.submit(recover,base) for base in (api.base,other)]
                workers=[future.result() for future in requests]
        recovered=workers[0]['value']
        assert recovered['receipt']==workers[1]['value']['receipt']
        assert recovered['receipt']['status']=='initializer_exited' and recovered['state']=='settled'
        assert recovered['remaining_bytes']==0 and not recovered['node_claim_pending']
        assert recovered['session_cleanup_verified'] is None
        assert api.call('POST',route)['receipt']==recovered['receipt']
        final=api.call('GET','/test/initializer-final/'+stage)
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+token},timeout=90,trust_env=False) as client:
            response=client.post('/test/native-api-restart/'+stage);response.raise_for_status();restarted=response.json()
        # The old session's absence of a whole-scope stop is not overwritten by
        # the new session's actual verified normal stop.
        again=api.call('GET','/test/initializer-final/'+stage)
        assert again['receipt']==final['receipt'] and again['session']['cleanup_verified'] is None
        result['stages'].append({'stage':stage,'killed_api_pid':killed_pid,'before':before,'after':after,
            'workers':workers,'final':final,'restarted':restarted})
        save();print('NATIVE_API_CRASH_PASS '+stage,flush=True)
    api.create_case('Native API recovery actual model proof')
    result['passed']=True;save();return result
