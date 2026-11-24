"""Actual API death and independent workers recovering real managed CLI scopes."""
from cli_managed_session_checks import FIXTURE as BASE
from cli_managed_session_checks import UNKNOWN_FIXTURE

EXTENSION=r'''
from app.models import CliInitializationReservation
recovery_tasks=set()

async def session_snapshot(identifier,user):
 async with SessionLocal() as db:
  row=await db.get(NativeCliSession,identifier)
  if row is None or row.created_by!=user.id:
   from fastapi import HTTPException
   raise HTTPException(404)
  credit=await db.get(CliInitializationReservation,identifier)
  return {'session_id':row.id,'phase':row.phase,'version':row.version,'owner':json.loads(row.owner_identity_json),
   'profile_sha256':records.digest(json.loads(row.profile_json)),'journal':json.loads(row.process_json),'stop':json.loads(row.stop_json),
   'credit_state':credit.state,'credit_receipt':json.loads(credit.receipt_json),'public':records.public(row)}

@app.get('/api/v1/test/managed-recovery-state/{identifier}')
async def managed_state(identifier:str,user=Depends(get_current_user)):
 return await session_snapshot(identifier,user)

@app.post('/api/v1/test/managed-recovery-crash/{stage}')
async def managed_crash(stage:str,user=Depends(get_current_user)):
 await native_root()
 assert stage in {'preflight','root','bridge','private_prepared','active','stopped'}
 session=sessions.Session(None,None,user.id,runtime='wsl');ready=asyncio.Event();release=asyncio.Event()
 originals=[]
 if stage in {'preflight','root','bridge','private_prepared'}:
  name={'preflight':'register_native_preflight','root':'register_root','bridge':'register_bridge','private_prepared':'state_prepared'}[stage]
  original=getattr(sessions.Session,name)
  async def held(self,*args,**kwargs):
   result=await original(self,*args,**kwargs)
   if self is session:ready.set();await release.wait()
   return result
  setattr(sessions.Session,name,held);originals.append((sessions.Session,name,original))
 if stage=='stopped':
  original_complete=records.complete
  async def held_complete(owner,*args,**kwargs):
   if owner.id==session.id:ready.set();await release.wait()
   return await original_complete(owner,*args,**kwargs)
  records.complete=held_complete;originals.append((records,'complete',original_complete))
 async def work():
  await session.start()
  if stage=='active':ready.set();await release.wait()
  await session.close()
 task=asyncio.create_task(work());recovery_tasks.add(task);task.add_done_callback(recovery_tasks.discard)
 try:
  for _ in range(1200):
   if ready.is_set() or task.done():break
   await asyncio.sleep(.05)
  if task.done():await task
  assert ready.is_set(),'Managed Session did not reach real crash boundary'
  return await session_snapshot(session.id,user)
 except BaseException:
  release.set();task.cancel();await asyncio.gather(task,return_exceptions=True);raise
 finally:
  for obj,name,original in reversed(originals):setattr(obj,name,original)
'''
FIXTURE=BASE+EXTENSION
STAGES=('preflight','root','bridge','private_prepared','active','stopped')

RECOVERY_CRASH_FIXTURE=r'''
@app.post('/api/v1/test/managed-recovery-pause/{stage}')
async def pause_recovery(stage:str,body:dict,user=Depends(get_current_user)):
 from app.services import cli_session_recovery as recovery
 from sqlalchemy.ext.asyncio import AsyncSession
 assert stage in {'observed','before_commit','committed','finished'}
 identifier=body['session_id'];ready=asyncio.Event();release=asyncio.Event();patches=[]
 def patch(obj,name):
  original=getattr(obj,name)
  async def held(*args,**kwargs):
   value=await original(*args,**kwargs)
   ready.set();await release.wait();return value
  setattr(obj,name,held);patches.append((obj,name,original))
 if stage=='observed':patch(recovery,'observe')
 elif stage=='committed':patch(recovery,'publish')
 elif stage=='finished':patch(recovery,'finish')
 else:
  original_commit=AsyncSession.commit
  async def held_commit(db):
   row=await db.get(NativeCliSession,identifier,populate_existing=True)
   if row is not None and row.phase=='failed' and json.loads(row.process_json).get('management',{}).get('recovery'):
    ready.set();await release.wait()
   return await original_commit(db)
  AsyncSession.commit=held_commit;patches.append((AsyncSession,'commit',original_commit))
 task=asyncio.create_task(recovery.recover(identifier,user.id));recovery_tasks.add(task);task.add_done_callback(recovery_tasks.discard)
 try:
  for _ in range(1500):
   if ready.is_set() or task.done():break
   await asyncio.sleep(.05)
  if task.done():await task
  assert ready.is_set(),'Actual recovery did not reach its durable boundary'
  snapshot=await session_snapshot(identifier,user)
  from app.services import cli_initialization_capacity as capacity,cli_node_capacity as node
  async with SessionLocal() as db:
   row=await db.get(NativeCliSession,identifier);credit=await db.get(CliInitializationReservation,identifier)
   ticket=capacity.ticket(credit,row)
  snapshot['node_claim_pending']=ticket is not None and await asyncio.to_thread(node.outstanding,ticket)
  return snapshot
 except BaseException:
  release.set();task.cancel();await asyncio.gather(task,return_exceptions=True);raise
 finally:
  for obj,name,original in reversed(patches):setattr(obj,name,original)
'''

REFUSAL_FIXTURE=r'''
@app.post('/api/v1/test/recovery-host-session')
async def host_session(user=Depends(get_current_user)):
 session=sessions.Session(None,None,user.id,runtime='windows')
 try:await session.start()
 finally:await session.close()
 async with SessionLocal() as db:
  row=await db.get(NativeCliSession,session.id);value=records.public(row)
  assert value['phase']=='closed' and value['cleanup_verified'] and 'management' not in value['process']
  return value
'''

QUOTA_FIXTURE=r'''
@app.post('/api/v1/test/managed-recovery-quota')
async def quota_contention(body:dict,user=Depends(get_current_user)):
 from app.services import cli_session_recovery as recovery
 from app.services.storage_admission import lock_owner
 from fastapi import HTTPException
 identifier=body['session_id'];hold=body['hold'];assert hold in {'brief','exhaust'}
 blocked=asyncio.Event();attempts=0;observations=0;original_lock=recovery.lock_owner;original_observe=recovery.observe
 async def observed(*args,**kwargs):
  nonlocal observations
  observations+=1;return await original_observe(*args,**kwargs)
 async def locking(*args,**kwargs):
  nonlocal attempts
  try:return await original_lock(*args,**kwargs)
  except HTTPException as error:
   if error.status_code==409 and error.detail.get('code')=='storage_quota_busy':attempts+=1;blocked.set()
   raise
 recovery.lock_owner=locking;recovery.observe=observed;task=None
 try:
  async with SessionLocal() as holder:
   assert holder.get_bind().dialect.name=='postgresql'
   await lock_owner(holder,user.id)
   task=asyncio.create_task(recovery.recover(identifier,user.id))
   await asyncio.wait_for(blocked.wait(),60)
   if hold=='exhaust':
    try:await task
    except HTTPException as error:
     assert error.status_code==409 and error.detail['code']=='storage_quota_busy'
    else:raise AssertionError('Persistent real quota lock was ignored')
   await holder.rollback()
  if hold=='brief':
   value=await task;assert value['session']['cleanup_verified'] and attempts>=1
  else:assert attempts==6
  assert observations==1,'Quota retries repeated actual scope observation/retirement'
  recovery.lock_owner=original_lock;recovery.observe=original_observe
  final=await recovery.recover(identifier,user.id)
  assert final['session']['cleanup_verified'] and not final['initialization']['node_claim_pending']
  return {'hold':hold,'actual_quota_rejections':attempts,'scope_observations_during_retry':observations,
   'recovered_after_release':final,'persistent_conflict_reported':hold=='exhaust'}
 finally:
  recovery.lock_owner=original_lock;recovery.observe=original_observe
  if task is not None and not task.done():task.cancel()
  if task is not None:await asyncio.gather(task,return_exceptions=True)
'''


def verify_quota(api,**_):
    import httpx,json,shutil
    user=api.call('GET','/auth/me');result={'passed':False,'checks':[],'mock_llm_calls':0}
    def call(route,payload=None):
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=150,trust_env=False) as client:
            response=client.post(route,json=payload)
            assert response.status_code==200,(route,response.status_code,response.text[:1500]);return response.json()
    try:
        for hold in ('brief','exhaust'):
            started=call('/test/managed-recovery-crash/root')
            api.__exit__(RuntimeError,None,None);assert api.proc.poll() is not None
            shutil.copyfile(api.root/'api.log',api.root/('quota-owner-'+hold+'.log'))
            api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
            result['checks'].append(call('/test/managed-recovery-quota',{'session_id':started['session_id'],'hold':hold}))
        result['passed']=True;return result
    finally:(api.root/'managed-recovery-quota.json').write_text(json.dumps(result,indent=2),encoding='utf8')


def verify_refusals(api,**_):
    import httpx,json,shutil
    user=api.call('GET','/auth/me')
    def call(method,path,payload=None,expected=200):
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=150,trust_env=False) as client:
            response=client.request(method,path,json=payload)
            assert response.status_code==expected,(path,response.status_code,response.text[:1500])
            return response.json()
    host=call('POST','/test/recovery-host-session')
    unknown=call('POST','/test/managed-session-unknown-native')
    identifier=unknown['session']['id'];before=call('GET','/test/managed-recovery-state/'+identifier)
    api.__exit__(RuntimeError,None,None);assert api.proc.poll() is not None
    shutil.copyfile(api.root/'api.log',api.root/'managed-recovery-refusal-owner.log')
    api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
    incomplete=call('POST','/codex/sessions/'+identifier+':reconcile-scope',expected=409)
    legacy=call('POST','/codex/sessions/'+host['id']+':reconcile-scope',expected=409)
    assert incomplete['detail']['code']==legacy['detail']['code']=='recovery_required'
    after=call('GET','/test/managed-recovery-state/'+identifier)
    assert before==after and after['credit_state']=='active' and after['public']['cleanup_verified'] is False
    assert call('GET','/codex/sessions/'+host['id'])==host
    result={'passed':True,'unknown_before':before,'unknown_after':after,'unknown_refusal':incomplete,
        'actual_host_session':host,'host_refusal':legacy,'actual_unknown_exit':unknown['actual_exit'],
        'mock_llm_calls':0,'cli_model_inference_verified':False}
    (api.root/'managed-recovery-refusals.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


def verify_recovery_crashes(api,**_):
    import httpx,json,shutil
    result={'passed':False,'stages':[],'mock_llm_calls':0,'cli_model_inference_verified':False}
    path=api.root/'managed-recovery-interruptions.json'
    def save():path.write_text(json.dumps(result,indent=2),encoding='utf8')
    def call(method,route,payload=None):
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=150,trust_env=False) as client:
            response=client.request(method,route,json=payload)
            assert response.status_code==200,(route,response.status_code,response.text[:1500])
            return response.json()
    user=api.call('GET','/auth/me')
    def restart(label):
        api.__exit__(RuntimeError,None,None);assert api.proc.poll() is not None
        shutil.copyfile(api.root/'api.log',api.root/('recovery-interruption-'+label+'.log'))
        api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
    try:
        for stage in ('observed','before_commit','committed','finished'):
            before=call('POST','/test/managed-recovery-crash/root');restart(stage+'-original-owner')
            identifier=before['session_id'];route='/codex/sessions/'+identifier+':reconcile-scope'
            paused=call('POST','/test/managed-recovery-pause/'+stage,{'session_id':identifier})
            committed=stage in {'committed','finished'}
            assert (paused['journal']['management'].get('recovery') is not None)==committed
            assert paused['node_claim_pending']==(stage!='finished')
            assert (paused['credit_state']=='settled')==committed
            restart(stage+'-recovery-owner')
            recovered=call('POST',route);repeated=call('POST',route);assert recovered==repeated
            assert recovered['session']['cleanup_verified'] and recovered['session']['stop'] is None
            assert recovered['initialization']['state']=='settled' and not recovered['initialization']['node_claim_pending']
            if committed:assert recovered['session']['process']==paused['journal']
            assert recovered['session']['process']['root']==before['journal']['root']
            result['stages'].append({'stage':stage,'before':before,'paused':paused,'recovered':recovered,
                'original_owner_and_recovery_owner_actually_terminated':True})
            save();print('MANAGED_RECOVERY_INTERRUPTION_PASS '+stage,flush=True)
        result['passed']=True;return result
    finally:save()


def verify(api,**_):
    import concurrent.futures,contextlib,httpx,json,os,shutil,socket,subprocess,sys,time,uuid
    from testsupport import SERVER
    result={'passed':False,'stages':[],'peers':[],'mock_llm_calls':0,'cli_model_inference_verified':False,
        'execution_lock_recovery_verified':False}
    def save():(api.root/'managed-session-recovery-checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    def call(method,path,payload=None,*,expected=200,base=None):
        with httpx.Client(base_url=(base or api.base)+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=150,trust_env=False) as client:
            response=client.request(method,path,json=payload)
            assert response.status_code==expected,(path,response.status_code,response.text[:1500])
            return response.json()
    @contextlib.contextmanager
    def peer(stage):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        assert port!=8000;base='http://127.0.0.1:'+str(port)
        with (api.root/('managed-recovery-peer-'+stage+'.log')).open('w',encoding='utf8') as log:
            proc=subprocess.Popen([sys.executable,'-B','-m','uvicorn','live_fixture:app','--app-dir',str(SERVER),
                '--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=api.root,env=dict(api.env),
                stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                with httpx.Client(trust_env=False,timeout=1) as client:
                    for _ in range(150):
                        assert proc.poll() is None,'Recovery worker exited before readiness'
                        try:
                            if client.get(base+'/api/health').json().get('ok'):break
                        except (httpx.HTTPError,ValueError):pass
                        time.sleep(.1)
                    else:raise TimeoutError('Recovery worker readiness timed out')
                yield base
            finally:
                proc.terminate()
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
                result['peers'].append({'stage':stage,'pid':proc.pid,'api':base,'exited':proc.poll() is not None});save()
    user=api.call('GET','/auth/me')
    try:
        for stage in STAGES:
            before=call('POST','/test/managed-recovery-crash/'+stage)
            route='/codex/sessions/'+before['session_id']+':reconcile-scope'
            call('POST',route,expected=409)
            api.__exit__(RuntimeError,None,None);assert api.proc.poll() is not None
            shutil.copyfile(api.root/'api.log',api.root/('managed-recovery-crash-'+stage+'.log'))
            api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
            after=call('GET','/test/managed-recovery-state/'+before['session_id'])
            assert before['journal']==after['journal'] and before['stop']==after['stop']=={}
            original_token=api.token
            auth=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'other','password':'isolated-test-123'})
            api.token=auth['token'];call('POST',route,expected=404);api.token=original_token
            initializer=None
            if stage=='bridge':
                initializer=call('POST','/codex/sessions/'+before['session_id']+':reconcile-initialization')
                assert initializer['receipt']['status']=='initializer_exited'
                assert initializer['receipt']['proof']['binding']['writer_binding_schema']=='vibeflow.cli-initializer-writers.v1'
            with peer(stage) as other,concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(call,'POST',route,base=base) for base in (api.base,other)]
                recovered=[future.result() for future in futures]
            assert recovered[0]==recovered[1]
            repeated=call('POST',route);assert repeated==recovered[0]
            final=call('GET','/test/managed-recovery-state/'+before['session_id'])
            value=repeated['session'];assert value['phase']=='failed' and value['cleanup_verified'] and value['stop'] is None
            assert repeated['recovery']['owner_exit']['state']=='exited'
            assert repeated['initialization']['state']=='settled' and not repeated['initialization']['node_claim_pending']
            assert not repeated['execution_lock_recovered'] and not repeated['staging_reconciled']
            assert final['stop']==before['stop'] and final['profile_sha256']==before['profile_sha256']
            assert all(final['journal'][k]==before['journal'][k] for k in ('root','bridge'))
            if initializer is not None:
                again=call('POST','/codex/sessions/'+before['session_id']+':reconcile-initialization')
                assert again['receipt']==initializer['receipt'] and again['session_cleanup_verified']
            result['stages'].append({'stage':stage,'before':before,'after_owner_death':after,'final':final,
                'recovered':repeated,'concurrent_receipts_equal':True,'other_user_refused':True,
                'initializer_receipt_before_scope_recovery':initializer})
            save();print('MANAGED_SESSION_RECOVERY_PASS '+stage,flush=True)
        result['passed']=True;return result
    finally:save()
