"""Actual installed CLI startup; no inference replies or RPC results are fabricated."""
FIXTURE=r'''
import asyncio,threading,time
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.deps import get_current_user
from app.db import SessionLocal
from app.models import NativeCliSession
from app.services import codex_sessions as sessions,codex_records as records,request_deadline
from app.services import process_identity_native as identity
from app.services.codex_stdio import CodexStdioClient

@app.post('/api/v1/test/cli-startup/{stage}')
async def cli_startup(stage:str,body:dict,user=Depends(get_current_user)):
 assert stage in {'discovery','private','hash','owner_commit','initialized','activate_commit','parent','normal'}
 session=sessions.Session(body['case_id'],None,user.id)
 ready=threading.Event();release=threading.Event();paused=False
 observations={'stage':stage,'model_rounds':0,'actual_cli':True}
 original_command=sessions.command;original_private=sessions._private_environment;original_hash=sessions.sha_file
 original_commit=AsyncSession.commit;original_start=CodexStdioClient.start;original_prepare=sessions.Session.prepare
 seconds=request_deadline.CLI_STARTUP_SECONDS
 async def pause_async():
  nonlocal paused
  paused=True;ready.set()
  while not release.is_set():await asyncio.sleep(.01)
 def pause_sync():
  nonlocal paused
  paused=True;ready.set()
  if not release.wait(20):raise TimeoutError('CLI test release was not delivered')
 def command(*args,**kwargs):
  value=original_command(*args,**kwargs)
  observations['program']=value[0]
  if stage in {'discovery','parent'} and not paused:pause_sync()
  return value
 def private(*args,**kwargs):
  value=original_private(*args,**kwargs)
  if stage=='private' and not paused:pause_sync()
  return value
 def digest(*args,**kwargs):
  value=original_hash(*args,**kwargs)
  if stage=='hash' and not paused:pause_sync()
  return value
 async def commit(db):
  await original_commit(db)
  if session.owner is not None and not paused:
   if stage=='owner_commit' and not session.client.initialized:await pause_async()
   elif stage=='activate_commit' and session.client.initialized:await pause_async()
 async def start(client):
  result=await original_start(client)
  observations['initialize_result']=client.initialize_result
  if stage=='initialized':await pause_async()
  return result
 async def prepare(value):
  budget=request_deadline.current()
  observations['budget_seconds']=budget.remaining() if budget else None
  observations['deadline']=budget.deadline if budget else None
  return await original_prepare(value)
 async def row():
  async with SessionLocal() as db:
   value=await db.get(NativeCliSession,session.id)
   return records.public(value) if value else None
 async def work():
  if stage=='parent':
   async with request_deadline.scope(1.5):return await session.start()
  return await session.start()
 task=None
 try:
  request_deadline.CLI_STARTUP_SECONDS=8 if stage=='normal' else 5
  sessions.command=command;sessions._private_environment=private;sessions.sha_file=digest
  AsyncSession.commit=commit;CodexStdioClient.start=start;sessions.Session.prepare=prepare
  task=asyncio.create_task(work())
  if stage!='normal':
   until=time.monotonic()+12
   while not ready.is_set() and not task.done() and time.monotonic()<until:await asyncio.sleep(.02)
   assert ready.is_set(),'Actual startup failed before selected boundary'
   await asyncio.sleep(max(0,observations['deadline']-time.monotonic())+.15)
   observations['pending_after_deadline']=not task.done()
   observations['before_release']=await row()
   release.set()
  result=(await asyncio.wait_for(asyncio.gather(task,return_exceptions=True),20))[0]
  observations['error']=type(result).__name__ if isinstance(result,BaseException) else None
  observations['record_before_close']=await row()
  if stage=='normal':await session.close()
  observations['record']=await row()
  observations['cleanup_verified']=session.cleanup_verified
  observations['registered_in_memory']=session.id in sessions.SESSIONS
  if observations['record'] is not None:
   process=observations['record']['process']
   root_birth=process.get('root') if process else None
   if root_birth is not None:
    observations['actual_host_exit']=identity.probe(root_birth['host'])
  return observations
 finally:
  release.set()
  if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
  if not session.stopping:
   try:await session.close(error='AcceptanceCleanup')
   except Exception:pass
  request_deadline.CLI_STARTUP_SECONDS=seconds
  sessions.command=original_command;sessions._private_environment=original_private;sessions.sha_file=original_hash
  AsyncSession.commit=original_commit;CodexStdioClient.start=original_start;sessions.Session.prepare=original_prepare

@app.post('/api/v1/test/cli-durable/{stage}')
async def cli_durable(stage:str,body:dict,user=Depends(get_current_user)):
 from app.services import codex_session_requests as startups
 import uuid
 assert stage in {'normal','deadline'}
 original_command=sessions.command;original_prepare=sessions.Session.prepare;original_guard=startups.Execution.guard
 original_seconds=startups.STARTUP_SECONDS
 ready=threading.Event();release=threading.Event();delayed=False
 observations={'stage':stage,'model_rounds':0,'actual_cli':True}
 requested={'provider_profile_id':None,'provider_wire_api':None,'runtime':'windows','purpose':'inspect'}
 request_id=uuid.uuid4().hex;execution=None;session=None
 def command(*args,**kwargs):
  value=original_command(*args,**kwargs)
  if stage=='deadline':
   ready.set()
   if not release.wait(20):raise TimeoutError('Durable startup test release missing')
  return value
 async def guard(self,db):
  nonlocal delayed
  await original_guard(self,db)
  if stage=='deadline' and not delayed:
   delayed=True;await asyncio.sleep(1)
 async def prepare(self):
  nonlocal session
  session=self
  budget=request_deadline.current()
  observations['remaining_at_prepare']=budget.remaining() if budget else None
  observations['deadline']=budget.deadline if budget else None
  return await original_prepare(self)
 try:
  startups.STARTUP_SECONDS=4 if stage=='deadline' else 45
  sessions.command=command;sessions.Session.prepare=prepare;startups.Execution.guard=guard
  admitted=await startups.submit(body['conversation_id'],user.id,request_id,requested)
  observations['admitted_id']=admitted['id'];execution=startups.RUNS.get(admitted['id'])
  assert execution is not None
  if stage=='deadline':
   until=time.monotonic()+12
   while not ready.is_set() and not execution.task.done() and time.monotonic()<until:await asyncio.sleep(.02)
   assert ready.is_set(),'Durable CLI failed before actual command discovery'
   await asyncio.sleep(max(0,observations['deadline']-time.monotonic())+.15)
   observations['pending_after_deadline']=not execution.task.done()
   observations['before_release']=await startups.get(admitted['id'],user.id)
   release.set()
  await asyncio.wait_for(asyncio.shield(execution.task),20)
  observations['result']=await startups.get(admitted['id'],user.id)
  observations['replay']=await startups.submit(body['conversation_id'],user.id,request_id,requested)
  if stage=='normal':
   assert session is not None
   observations['initialize_result']=session.client.initialize_result
   await session.close()
   observations['after_close']=await startups.get(admitted['id'],user.id)
   observations['actual_host_exit']=identity.probe(observations['after_close']['session']['process']['root']['host'])
  observations['active_run_remaining']=admitted['id'] in startups.RUNS
  return observations
 finally:
  release.set()
  if execution is not None and execution.task is not None and not execution.task.done():await execution.stop(error='AcceptanceCleanup')
  if session is not None and not session.stopping:
   try:await session.close(error='AcceptanceCleanup')
   except Exception:pass
  startups.STARTUP_SECONDS=original_seconds
  sessions.command=original_command;sessions.Session.prepare=original_prepare;startups.Execution.guard=original_guard
'''


def verify(api,*,durable_only=False):
    import json
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0,'cases':[]}
    path=api.root/'cli-startup-checks.json'
    def save():path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    try:
        for stage in (() if durable_only else ('discovery','private','hash','owner_commit','initialized','activate_commit','parent','normal')):
            value=api.call('POST','/test/cli-startup/'+stage,{'case_id':api.case_id})
            report['cases'].append(value);save()
            assert value['cleanup_verified'] is True and not value['registered_in_memory'],value
            if stage=='normal':
                assert value['error'] is None and value['record_before_close']['phase']=='active'
                assert value['record']['phase']=='closed' and value['initialize_result']['userAgent']
            else:
                assert value['error']=='TimeoutError',value
                assert value['budget_seconds']<=5 and (stage!='parent' or value['budget_seconds']<=1.5)
                if stage!='initialized':assert value['pending_after_deadline'],value
                if stage in {'discovery','private','hash','parent'}:assert value['record'] is None,value
                else:assert value['record']['phase']=='failed' and value['record']['error']=='TimeoutError',value
            if value['record'] is not None:assert value['record']['cleanup_verified'] and value['record']['thread_id'] is None
            if stage in {'initialized','activate_commit','normal'}:
                assert value['actual_host_exit']['state']=='exited' and value['initialize_result']['userAgent'],value
            print('CLI_STARTUP_DEADLINE_PASS '+stage,flush=True)
        conversation=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual durable CLI startup','permission_mode':'ask'})
        report['durable_cases']=[]
        for stage in ('deadline','normal'):
            value=api.call('POST','/test/cli-durable/'+stage,{'conversation_id':conversation['id']})
            report['durable_cases'].append(value);save()
            assert value['result']['id']==value['replay']['id'] and value['result']['phase']==value['replay']['phase']
            assert not value['active_run_remaining']
            if stage=='deadline':
                assert value['remaining_at_prepare']<3.2 and value['pending_after_deadline']
                assert value['result']['phase']=='failed' and value['result']['error']=='TimeoutError'
                assert value['result']['cleanup_verified'] is True and value['result']['session'] is None
            else:
                assert value['result']['phase']=='completed' and value['result']['session']['phase']=='active'
                assert value['after_close']['cleanup_verified'] is True and value['actual_host_exit']['state']=='exited'
                assert value['initialize_result']['userAgent']
            print('CLI_DURABLE_STARTUP_PASS '+stage,flush=True)
        report['passed']=True;save();return report
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1200]};save();raise
