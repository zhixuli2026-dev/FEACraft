"""Real S3 reads, concurrent SQL writers and installed CLI startup."""
FIXTURE=r'''
import asyncio,threading,time,uuid
from pathlib import Path
from fastapi import Depends
from app.deps import get_current_user
from app.db import SessionLocal
from app.models import Case,Conversation,NativeCliSession
from app import storage
from app.services import codex_sessions as sessions,codex_records as records,request_deadline,staging_download as download
from app.services import process_identity_native as identity
from app.services.case_write import coordinate
from app.services.durable_events import lock_case_execution
from app.services.s3_live_store import Client

@app.post('/api/v1/test/cli-staging/{mode}')
async def cli_staging(mode:str,body:dict,user=Depends(get_current_user)):
 assert mode in {'inventory','get','read','manifest','returned','source_change','permission_change','cancel','normal'}
 session=sessions.Session(body['case_id'],body['conversation_id'],user.id)
 ready=threading.Event();release=threading.Event();paused=False
 observations={'mode':mode,'mock_llm_calls':0,'read_calls':0,'body_closed':False}
 old_inventory=download.inventory;old_get=Client.get_object;old_write=Path.write_bytes
 old_download=download.prepare;old_prepare=sessions.Session.prepare;old_seconds=request_deadline.CLI_STARTUP_SECONDS
 def pause():
  nonlocal paused
  paused=True;ready.set()
  if not release.wait(30):raise TimeoutError('Actual S3 fixture was not released')
 def inventory(*args,**kwargs):
  value=old_inventory(*args,**kwargs)
  if mode=='inventory' and not paused:pause()
  return value
 class Body:
  def __init__(self,value):self.value=value
  def read(self,*args,**kwargs):
   value=self.value.read(*args,**kwargs);observations['read_calls']+=1
   if mode=='read' and not paused:pause()
   return value
  def close(self):
   self.value.close();observations['body_closed']=True
 def get(client,**kwargs):
  result=old_get(client,**kwargs)
  if (mode in {'get','read'} and kwargs['Key'].endswith('/Ref/large.bin')
      and request_deadline.current() is not None):
   result['Body']=Body(result['Body'])
   if mode=='get' and not paused:pause()
  return result
 def write(path,data):
  value=old_write(path,data)
  if mode=='manifest' and str(path).endswith('.manifest.json') and not paused:pause()
  return value
 def prepare(prefix,stage):
  observations['stage']=stage
  value=old_download(prefix,stage)
  if mode in {'returned','source_change','permission_change','cancel'} and not paused:pause()
  return value
 async def session_prepare(value):
  budget=request_deadline.current();observations['deadline']=budget.deadline
  return await old_prepare(value)
 async def compete():
  began=time.monotonic()
  async with SessionLocal() as db:
   await lock_case_execution(db,body['case_id'])
   case=await db.get(Case,body['case_id'])
   if mode=='source_change':
    async with coordinate(db,case):
     await asyncio.to_thread(storage.put_text,case.s3_prefix,'Ref/source.txt',body['changed'])
   if mode=='permission_change':
    conv=await db.get(Conversation,body['conversation_id']);conv.permission_mode='plan'
   await db.commit()
  observations['competing_write_seconds']=time.monotonic()-began
 async def read_row():
  async with SessionLocal() as db:
   row=await db.get(NativeCliSession,session.id)
   return records.public(row) if row else None
 task=None
 try:
  request_deadline.CLI_STARTUP_SECONDS=30 if mode in {'source_change','permission_change','cancel','normal'} else 8
  download.inventory=inventory;download.prepare=prepare;Client.get_object=get;Path.write_bytes=write;sessions.Session.prepare=session_prepare
  task=asyncio.create_task(session.start())
  if mode!='normal':
   until=time.monotonic()+15
   while not ready.is_set() and not task.done() and time.monotonic()<until:await asyncio.sleep(.02)
   assert ready.is_set(),'CLI preparation failed before actual S3 pause'
   await asyncio.wait_for(compete(),3)
   if mode=='cancel':
    for _ in range(3):task.cancel();await asyncio.sleep(.03)
   elif mode not in {'source_change','permission_change'}:
    await asyncio.sleep(max(0,observations['deadline']-time.monotonic())+.1)
   observations['pending_before_release']=not task.done()
   release.set()
  result=(await asyncio.wait_for(asyncio.gather(task,return_exceptions=True),20))[0]
  observations['error']=type(result).__name__ if isinstance(result,BaseException) else None
  observations['error_code']=getattr(result,'code',None)
  if mode=='normal':
   observations['initialize_result']=session.client.initialize_result
   observations['active']=await read_row()
   await session.close()
  observations['record']=await read_row()
  observations['cleanup_verified']=session.cleanup_verified
  observations['registered_in_memory']=session.id in sessions.SESSIONS
  if 'stage' in observations:
   stage=observations['stage']
   observations['stage_removed']=not any(p.exists() for p in (storage._fs_path(stage,'.'),
       storage._staging_manifest_path(stage),storage._terminal_cleanup_marker(stage)))
  from app.services.node_storage_budget import DIRECTORY
  claims=storage._fs_root()/DIRECTORY
  observations['remaining_claim_files']=[p.name for p in claims.glob('*.json')]
  if observations['record'] and observations['record']['process']['root']:
   observations['actual_host_exit']=identity.probe(observations['record']['process']['root']['host'])
  return observations
 finally:
  release.set()
  if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
  if not session.stopping:
   try:await session.close(error='AcceptanceCleanup')
   except Exception:pass
  request_deadline.CLI_STARTUP_SECONDS=old_seconds
  download.inventory=old_inventory;download.prepare=old_download;Client.get_object=old_get;Path.write_bytes=old_write;sessions.Session.prepare=old_prepare
'''


def verify(api,**_):
    import hashlib,json,uuid
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0,'cases':[]}
    path=api.root/'cli-staging-checks.json'
    def save():path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    try:
        large=('ACTUAL_S3_'+uuid.uuid4().hex+'\n')*60000
        api.put('Ref/large.bin',large)
        for mode in ('inventory','get','read','manifest','returned','source_change','permission_change','cancel','normal'):
            before='SOURCE_'+uuid.uuid4().hex;changed='CHANGED_'+uuid.uuid4().hex
            api.put('Ref/source.txt',before)
            conv=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual CLI S3 '+mode,'permission_mode':'ask'})
            value=api.call('POST','/test/cli-staging/'+mode,{'case_id':api.case_id,'conversation_id':conv['id'],'changed':changed})
            report['cases'].append(value);save()
            assert value['cleanup_verified'] and value['stage_removed'] and not value['registered_in_memory'],value
            assert not value['remaining_claim_files'],value
            if mode=='normal':
                assert value['error'] is None and value['active']['phase']=='active' and value['record']['phase']=='closed'
                assert value['record']['cleanup_verified'] and value['actual_host_exit']['state']=='exited'
            else:
                assert value['pending_before_release'] and value['competing_write_seconds']<3 and value['record'] is None,value
                if mode in {'source_change','permission_change'}:assert value['error_code']=='input_stale',value
                elif mode=='cancel':assert value['error']=='CancelledError',value
                else:assert value['error']=='TimeoutError',value
            if mode in {'get','read'}:
                assert value['body_closed'] and value['read_calls']==(0 if mode=='get' else 1),value
            assert api.call('GET',api.prefix+'/raw?path=Ref/source.txt',raw=True)==(changed if mode=='source_change' else before).encode()
            assert hashlib.sha256(api.call('GET',api.prefix+'/raw?path=Ref/large.bin',raw=True)).digest()==hashlib.sha256(large.encode()).digest()
            print('CLI_STAGING_PASS '+mode,flush=True)
        report['passed']=True;save();return report
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1200]};save();raise
