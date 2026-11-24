"""Actual SQL/native allocation crash boundaries; never synthesize inference."""
FIXTURE=r'''
import asyncio,json,uuid
from fastapi import Depends,HTTPException
from app.deps import get_current_user
from app.db import SessionLocal
from app.models import CliScopeAllocation
from app.services import cli_scope_allocation as allocations
from app.services.workflow_state import ExecutionConflict
scope_tasks=set()

async def new_scope(user):
 async with SessionLocal() as db:
  owner=await allocations.create(db,str(uuid.uuid4()),user.id,'vibeflow-linux');await db.commit()
 return owner

@app.post('/api/v1/test/cli-scope-normal')
async def scope_normal(user=Depends(get_current_user)):
 owner=await new_scope(user)
 scope=await allocations.allocate(owner)
 try:
  await allocations.allocate(owner)
 except ExecutionConflict:duplicate_refused=True
 else:raise AssertionError('Duplicate allocation accepted')
 try:
  await allocations.retire(allocations.Owner(owner.id,'incorrect-token',owner.binding_sha256))
 except ExecutionConflict:wrong_token_refused=True
 else:raise AssertionError('Incorrect owner accepted')
 first=await allocations.retire(owner);second=await allocations.retire(owner)
 assert first==second and first['kind']=='retire' and first['receipt']['result']['state']=='absent'
 return {'id':owner.id,'scope':scope,'retirement':first,'duplicate_refused':duplicate_refused,'wrong_token_refused':wrong_token_refused}

@app.post('/api/v1/test/cli-scope-crash/{stage}')
async def scope_crash(stage:str,user=Depends(get_current_user)):
 assert stage in {'intent','prepared','allocated_uncommitted','allocated','retiring'}
 owner=await new_scope(user);ready=asyncio.Event();release=asyncio.Event()
 original_record=allocations.record;original_control=allocations.control.control
 async def held_record(current,field,value):
  if current.id==owner.id and stage=='allocated_uncommitted' and field=='allocated_json':
   ready.set();await release.wait()
  result=await original_record(current,field,value)
  if current.id==owner.id and stage=='prepared' and field=='prepared_json':
   ready.set();await release.wait()
  return result
 async def held_control(distribution,operation,arguments,**kwargs):
  if stage=='retiring' and operation=='retire':ready.set();await release.wait()
  return await original_control(distribution,operation,arguments,**kwargs)
 allocations.record=held_record;allocations.control.control=held_control
 async def work():
  if stage=='intent':ready.set();await release.wait()
  await allocations.allocate(owner)
  if stage=='allocated':ready.set();await release.wait()
  await allocations.retire(owner)
 task=asyncio.create_task(work());scope_tasks.add(task);task.add_done_callback(scope_tasks.discard)
 try:
  for _ in range(800):
   if ready.is_set() or task.done():break
   await asyncio.sleep(.1)
  if task.done():await task
  assert ready.is_set(),'Actual allocation boundary was not reached'
  async with SessionLocal() as db:
   row=await db.get(CliScopeAllocation,owner.id)
   binding,prepared,allocated,retired=allocations.document(row)
   return {'id':owner.id,'stage':stage,'binding':binding,'prepared':prepared,'allocated':allocated,'retired':retired}
 except BaseException:
  release.set();task.cancel();await asyncio.gather(task,return_exceptions=True);raise
 finally:allocations.record=original_record;allocations.control.control=original_control

@app.post('/api/v1/test/cli-scope-recover/{identifier}')
async def scope_recover(identifier:str,user=Depends(get_current_user)):
 return await allocations.recover(identifier,user.id)
'''


def verify(api,**_):
    import concurrent.futures,contextlib,json,os,shutil,socket,subprocess,sys,time
    import httpx
    from testsupport import SERVER
    report={'passed':False,'mock_llm_calls':0,'scope_only':True,'application_session_journal_integrated':False,'checks':[],'peers':[]}
    path=api.root/'scope-allocation-checks.json'
    def save():path.write_text(json.dumps(report,indent=2),encoding='utf8')
    def call(method,url,payload=None,expected=200,base=None):
        with httpx.Client(base_url=(base or api.base)+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=120,trust_env=False) as client:
            response=client.request(method,url,json=payload)
            assert response.status_code==expected,(url,response.status_code,response.text[:1000])
            return response.json()
    @contextlib.contextmanager
    def peer(stage):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        assert port!=8000
        base='http://127.0.0.1:'+str(port)
        with (api.root/('scope-peer-'+stage+'.log')).open('w',encoding='utf8') as log:
            proc=subprocess.Popen([sys.executable,'-B','-m','uvicorn','live_fixture:app','--app-dir',str(SERVER),
                '--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=api.root,env=dict(api.env),
                stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                with httpx.Client(trust_env=False,timeout=1) as client:
                    for _ in range(150):
                        assert proc.poll() is None,'Independent allocation recovery API exited'
                        try:
                            if client.get(base+'/api/health').json().get('ok'):break
                        except (httpx.HTTPError,ValueError):pass
                        time.sleep(.1)
                    else:raise TimeoutError('Independent allocation recovery API unavailable')
                yield base
            finally:
                proc.terminate()
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
                report['peers'].append({'stage':stage,'pid':proc.pid,'api':base,'exited':proc.poll() is not None});save()
    user=api.call('GET','/auth/me')
    try:
        report['normal']=call('POST','/test/cli-scope-normal');save()
        for stage in ('intent','prepared','allocated_uncommitted','allocated','retiring'):
            started=call('POST','/test/cli-scope-crash/'+stage)
            identifier=started['id']
            call('POST','/test/cli-scope-recover/'+identifier,expected=409)
            api.__exit__(RuntimeError,None,None);assert api.proc.poll() is not None
            shutil.copyfile(api.root/'api.log',api.root/('scope-crash-'+stage+'.log'))
            api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
            # Two independent API processes share only the actual database.
            with peer(stage) as other,concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(call,'POST','/test/cli-scope-recover/'+identifier,base=base) for base in (api.base,other)]
                results=[f.result() for f in futures]
            assert results[0]==results[1]
            expected='unused' if stage=='intent' else 'abandon' if stage in {'prepared','allocated_uncommitted'} else 'retire'
            assert results[0]['kind']==expected
            repeated=call('POST','/test/cli-scope-recover/'+identifier)
            assert repeated==results[0]
            # A different real registered account cannot inspect recovery.
            original_token=api.token
            other=api.call('POST','/auth/register',{'email':identifier+'@example.com','name':'other','password':'isolated-test-123'})
            api.token=other['token'];call('POST','/test/cli-scope-recover/'+identifier,expected=404);api.token=original_token
            report['checks'].append({'stage':stage,'started':started,'retired':repeated,'concurrent_equal':True,'other_user_refused':True})
            save();print('CLI_SCOPE_ALLOCATION_BOUNDARY '+stage,flush=True)
        report['passed']=True;save();return report
    finally:save()
