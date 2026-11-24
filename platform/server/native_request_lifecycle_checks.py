"""Actual owner commits and sockets; fault pauses never replace model inference."""
FIXTURE = r'''
import asyncio,sys,time
from fastapi import Depends
from sqlalchemy import select
from app.deps import get_current_user
from app.db import SessionLocal
from app.models import NativeRequest
from app.services import native_request_execution as execution, native_request_records as records
from app.services import request_deadline, runtime_native

@app.post('/api/v1/test/native-request-lifecycle/{scenario}')
async def native_lifecycle(scenario:str,user=Depends(get_current_user)):
 assert scenario in {'create_cancel','create_timeout','constructor_failure','start_cancel','heartbeat_cancel','expired_new','expired_supplied'}
 ready=asyncio.Event();release=asyncio.Event();heartbeat_draining=asyncio.Event()
 observations={'scenario':scenario,'body_entered':False,'starts':0,'kind':'actual_infrastructure'}
 selected={'python':sys.executable,'packages':list(runtime_native.PACKAGES)}
 original_create=records.create;original_executor=execution.RequestExecutor;original_heartbeat=records.heartbeat
 owner=None;task=None;executor=None
 async def create(*args,**kwargs):
  nonlocal owner
  owner=await original_create(*args,**kwargs)
  if scenario in {'create_cancel','create_timeout'}:
   ready.set();await release.wait()
  return owner
 class ObservedExecutor(original_executor):
  def __init__(self,*args,**kwargs):
   nonlocal executor
   super().__init__(*args,**kwargs);executor=self
   if scenario=='constructor_failure':raise OSError('Injected constructor failure after actual initialization')
  async def start(self):
   value=await super().start();observations['starts']+=1
   observations['port']=int(self.address.rsplit(':',1)[1])
   if scenario=='start_cancel':ready.set();await release.wait()
   return value
 async def heartbeat(*args,**kwargs):
  await original_heartbeat(*args,**kwargs)
  if scenario=='heartbeat_cancel':
   ready.set()
   try:await asyncio.Event().wait()
   except asyncio.CancelledError:
    heartbeat_draining.set();await release.wait();raise
 async def inspect():
  if owner is None:return None
  async with SessionLocal() as db:
   row=await db.get(NativeRequest,owner.id)
   return {'id':row.id,'phase':row.phase,'error':row.error,'children':records.document(row)['children'],'ended':row.ended_at is not None}
 async def operation():
  async with request_deadline.scope(2 if scenario=='create_timeout' else .01 if scenario.startswith('expired_') else 30):
   if scenario.startswith('expired_'):time.sleep(.025)
   async with execution.scope(None,user.id,root,'runtime.python_probe',selected,
                              owner=owner if scenario=='expired_supplied' else None):
    observations['body_entered']=True
    if scenario=='heartbeat_cancel':await ready.wait()
 try:
  async with SessionLocal() as db:
   before=set(await db.scalars(select(NativeRequest.id).where(NativeRequest.created_by==user.id)))
  if scenario=='expired_supplied':owner=await original_create(None,user.id,'runtime.python_probe',selected)
  records.create=create;execution.RequestExecutor=ObservedExecutor;records.heartbeat=heartbeat
  task=asyncio.create_task(operation())
  if scenario in {'create_cancel','create_timeout','start_cancel','heartbeat_cancel'}:
   await asyncio.wait_for(ready.wait(),10)
   if scenario=='create_timeout':await asyncio.sleep(2.1)
   elif scenario=='heartbeat_cancel':
    await asyncio.wait_for(heartbeat_draining.wait(),10)
    for _ in range(3):task.cancel();await asyncio.sleep(.03)
   else:
    for _ in range(3):task.cancel();await asyncio.sleep(.03)
   observations['pending_before_release']=not task.done()
   observations['before_release']=await inspect()
  release.set()
  values=await asyncio.wait_for(asyncio.gather(task,return_exceptions=True),15)
  observations['error']=type(values[0]).__name__ if isinstance(values[0],BaseException) else None
  observations['after']=await inspect()
  if executor is not None:
   observations['listener_closed']=executor.server is None or not executor.server.is_serving()
   observations['no_executor_work']=not executor.entries and not executor.tasks and not executor.handlers
  async with SessionLocal() as db:
   after=set(await db.scalars(select(NativeRequest.id).where(NativeRequest.created_by==user.id)))
  observations['new_records']=len(after-before)
  if scenario=='constructor_failure':observations['kind']='protocol_fault'
  return observations
 finally:
  release.set()
  if task is not None and not task.done():
   task.cancel();await asyncio.gather(task,return_exceptions=True)
  records.create=original_create;execution.RequestExecutor=original_executor;records.heartbeat=original_heartbeat
  # Preserve a failed reproduction, but do not leave an active empty owner in
  # the disposable API. No native command was admitted by these scenarios.
  state=await inspect()
  if state is not None and state['phase']=='active':
   assert not state['children']
   await records.complete(owner,'CancelledError',cancelled=True)
'''


def verify(api, *, reproduce=False):
    import json,socket
    result={'passed':False,'mock_llm_calls':0,'llm_calls':0,'cases':[]}
    path=api.root/'native-lifecycle-checks.json'
    def save():path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    try:
        scenarios=['create_cancel'] if reproduce else ['create_cancel','create_timeout','constructor_failure',
            'start_cancel','heartbeat_cancel','expired_new','expired_supplied']
        for scenario in scenarios:
            value=api.call('POST','/test/native-request-lifecycle/'+scenario,{})
            result['cases'].append(value);save()
            if reproduce:
                assert value['after']['phase']=='active' and not value['after']['children']
                result['reproduced_active_owner_after_cancel']=True
                continue
            if scenario=='expired_new':
                assert value['new_records']==0 and value['after'] is None
            else:
                assert value['new_records']==1 and value['after']['ended'] and value['after']['children']=={}
                expected=('cancelled','CancelledError') if scenario in {'create_cancel','start_cancel','heartbeat_cancel'} else (
                    ('failed','OSError') if scenario=='constructor_failure' else ('failed','TimeoutError'))
                assert (value['after']['phase'],value['after']['error'])==expected,value
            if scenario in {'create_cancel','create_timeout','heartbeat_cancel'}:
                assert value['pending_before_release'] and value['before_release']['phase']=='active',value
            if scenario not in {'heartbeat_cancel'}:assert not value['body_entered'],value
            if scenario in {'create_cancel','create_timeout','constructor_failure','expired_new','expired_supplied'}:
                assert value['starts']==0,value
            if 'listener_closed' in value:assert value['listener_closed'] and value['no_executor_work'],value
            if 'port' in value:
                with socket.socket() as sock:
                    sock.settimeout(1);assert sock.connect_ex(('127.0.0.1',value['port']))!=0
            print('NATIVE_LIFECYCLE_PASS '+scenario,flush=True)
        result['passed']=not reproduce;save()
        return result
    except BaseException as error:
        result['failure']={'type':type(error).__name__,'detail':str(error)[:1000]};save();raise
