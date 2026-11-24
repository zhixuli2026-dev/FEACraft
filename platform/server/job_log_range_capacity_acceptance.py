"""Actual job, restarted API and real S3 response-body measurements; no LLM."""
import argparse,asyncio,hashlib,json,sqlite3,subprocess,sys,time,uuid
from pathlib import Path
import httpx
import testsupport
from testsupport import IsolatedServer,SERVER
from s3testsupport import ObjectStore
from terminal_shutdown_acceptance import RUNNER
from app.services import process_identity_native as identity

ROOT=SERVER.parents[1]
FIXTURE='''
import json,os,time
from contextvars import ContextVar
from pathlib import Path
from fastapi import Depends
from sqlalchemy import event
from app.main import app
from app import storage
from app.deps import get_current_user
from app.db import get_db,engine
from app.services import jobrunner
from app.services.process_identity_native import observe
from app.services.s3_live_store import Client
READS=[];original_get=Client.get_object
REQUEST_PATH=ContextVar('log_fixture_request_path',default='');CHECKED_OUT={}
@event.listens_for(engine.sync_engine,'checkout')
def checkout(connection,record,proxy):CHECKED_OUT[id(record)]=REQUEST_PATH.get()
@event.listens_for(engine.sync_engine,'checkin')
def checkin(connection,record):CHECKED_OUT.pop(id(record),None)
class Body:
 def __init__(self,real,row):self.real=real;self.row=row
 def read(self,*args,**kwargs):
  if Path('hold-log-reads').exists():
   self.row['paused']=True;until=time.monotonic()+60
   while Path('hold-log-reads').exists():
    if time.monotonic()>until:raise TimeoutError('Fixture log-read hold exceeded 60 seconds')
    time.sleep(.025)
  value=self.real.read(*args,**kwargs);self.row['bytes']+=len(value);self.row['reads']+=1;return value
 def close(self):self.row['closed']=True;return self.real.close()
 def __getattr__(self,name):return getattr(self.real,name)
def measured_get(self,**args):
 result=original_get(self,**args)
 if '/.vibeflow/job-logs/' in args['Key'] and args['Key'].endswith('.log'):
  row={'range':args.get('Range'),'if_match':args.get('IfMatch'),'content_length':result['ContentLength'],'bytes':0,'reads':0,'closed':False}
  READS.append(row);result={**result,'Body':Body(result['Body'],row)}
 return result
Client.get_object=measured_get
@app.on_event('startup')
async def birth():Path('api-birth.json').write_text(json.dumps(observe(os.getpid())))
@app.post('/fixture/log-io/reset')
async def reset(user=Depends(get_current_user)):READS.clear();return {'ok':True}
@app.get('/fixture/log-io/{job_id}')
async def reads(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 await db.rollback()
 proc=jobrunner.get_proc(job_id)
 return {'reads':READS,'local_owner':proc is not None,'subscribers':len(proc._subscribers) if proc else 0,'db_checked_out':engine.sync_engine.pool.checkedout(),'log_db_checked_out':sum(path.startswith('/api/v1/jobs/') for path in CHECKED_OUT.values())}
class RequestObserver:
 def __init__(self,app):self.app=app
 async def __call__(self,scope,receive,send):
  token=REQUEST_PATH.set(scope.get('path',''))
  try:await self.app(scope,receive,send)
  finally:REQUEST_PATH.reset(token)
app=RequestObserver(app)
'''

async def stream_bytes(api,job_id,suffix=''):
 collected=bytearray();events=[];done=False
 async with httpx.AsyncClient(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=60) as client:
  async with client.stream('GET','/jobs/'+str(job_id)+'/log-stream'+suffix) as stream:
   stream.raise_for_status()
   async for line in stream.aiter_lines():
    if not line.startswith('data: '):continue
    event=json.loads(line[6:]);assert event.get('type')!='error',event
    if event['type']=='log':
     collected.extend(event['text'].encode('utf8'));events.append({k:v for k,v in event.items() if k!='text'})
    if event['type']=='done':done=True;break
 assert done
 return bytes(collected),events


async def lifecycle(api,job_id,raw,result,save):
 path='/jobs/'+str(job_id)+'/log?limit=65536';hold=api.root/'hold-log-reads'
 async with httpx.AsyncClient(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=65) as client:
  async def stats():
   r=await client.get(api.base+'/fixture/log-io/'+str(job_id));r.raise_for_status();return r.json()
  async def reset():
   r=await client.post(api.base+'/fixture/log-io/reset');r.raise_for_status()
  await reset()
  detail=await client.get('/jobs/'+str(job_id));detail.raise_for_status()
  assert detail.json()['log_backlog']==raw[-4000:].decode('utf8','ignore')
  detail_stats=await stats();assert sum(r['bytes'] for r in detail_stats['reads'])==4000
  result['detail_tail_io']=detail_stats;save()
  await reset();legacy,events=await stream_bytes(api,job_id)
  assert legacy==raw;legacy_stats=await stats()
  assert all(v['range'] and v['if_match'] and v['closed'] and v['bytes']<=65536 for v in legacy_stats['reads'])
  result['legacy_sse']={'complete_bytes':len(legacy),'events':len(events),'io':legacy_stats};save()
  await reset();hold.touch();pending=[];stream=None
  try:
   stream=client.stream('GET','/jobs/'+str(job_id)+'/log-stream')
   opened=await stream.__aenter__();opened.raise_for_status()
   pending=[asyncio.create_task(client.get(path)) for _ in range(3)]
   until=time.monotonic()+10
   while True:
    measured=await stats()
    if len([r for r in measured['reads'] if r.get('paused')])==4:break
    assert time.monotonic()<until,measured
    await asyncio.sleep(.05)
   result['active_io_observations']=[measured];save()
   assert measured['log_db_checked_out']==0,measured
   started=time.monotonic();health=await client.get(api.base+'/api/health');health.raise_for_status();latency=time.monotonic()-started;assert latency<1
   refused=await client.get(path);assert refused.status_code==429 and refused.json()['detail']['code']=='log_read_busy'
   await stream.__aexit__(None,None,None);stream=None;await asyncio.sleep(.2)
   still_busy=await client.get(path);assert still_busy.status_code==429
   held=await stats();result['active_io_observations'].append(held);save()
   assert len(held['reads'])==4 and not any(r['closed'] for r in held['reads']) and held['log_db_checked_out']==0
   hold.unlink();responses=await asyncio.gather(*pending)
   assert all(r.status_code==200 and r.json()['chunk'].encode('utf8')==raw[:r.json()['offset']] for r in responses)
   until=time.monotonic()+5
   while True:
    drained=await stats()
    if all(r['closed'] for r in drained['reads']):break
    assert time.monotonic()<until
    await asyncio.sleep(.05)
   again=await client.get(path);assert again.status_code==200
   result['slow_io']={'held':held,'health_seconds':latency,'excess_status':refused.status_code,'after_disconnect_status':still_busy.status_code,'drained':drained,'recovered_status':again.status_code};save()
  finally:
   hold.unlink(missing_ok=True)
   if stream is not None:await stream.__aexit__(None,None,None)
   if pending:await asyncio.gather(*pending,return_exceptions=True)
  await reset();hold.touch();started=time.monotonic();pending=[asyncio.create_task(client.get(path))]
  try:
   until=started+10
   while not any(r.get('paused') for r in (await stats())['reads']):
    assert time.monotonic()<until
    await asyncio.sleep(.05)
   while time.monotonic()-started<31:
    health=await client.get(api.base+'/api/health');health.raise_for_status();await asyncio.sleep(.5)
   assert not pending[0].done(),'Timed-out real I/O must retain its slot until drained'
   held=await stats();result['active_io_observations'].append(held);save()
   assert held['log_db_checked_out']==0 and not held['reads'][0]['closed']
   hold.unlink();response=await pending[0]
   assert response.status_code==504 and response.json()['detail']['code']=='log_read_timeout'
   drained=await stats();assert all(r['closed'] for r in drained['reads'])
   again=await client.get(path);assert again.status_code==200
   result['read_deadline']={'elapsed':time.monotonic()-started,'status':response.status_code,'held':held,'drained':drained,'recovered_status':again.status_code};save()
  finally:
   hold.unlink(missing_ok=True);await asyncio.gather(*pending,return_exceptions=True)


async def verify(require_ranges,require_lifecycle=False):
 api=IsolatedServer('job-log-range-capacity',app='range_fixture:app')
 store=ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed',volume_max=8)
 result={'passed':False,'model_calls':0,'mock_llm_calls':0,'checks':[],'api_exits':[]};original=subprocess.Popen
 def save():(api.root/'range-result.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
 def launch(argv,**kwargs):
  assert argv[1:3]==['-m','uvicorn']
  return original([argv[0],'-X','utf8','-B',str(api.root/'graceful_server.py'),*argv[3:]],**kwargs)
 async def start():
  testsupport.subprocess.Popen=launch
  try:await asyncio.to_thread(api.__enter__)
  finally:testsupport.subprocess.Popen=original
 async def stop():
  birth=json.loads((api.root/'api-birth.json').read_text('utf8'))
  (api.root/'graceful-stop').touch();await asyncio.to_thread(api.proc.wait,90)
  observed=identity.probe(birth);assert api.proc.returncode==0 and observed['state']=='exited'
  result['api_exits'].append({'identity':birth,'observed':observed});save();api.log.close();(api.root/'graceful-stop').unlink()
 async def call(method,path,*args,**kwargs):return await asyncio.to_thread(api.call,method,path,*args,**kwargs)
 try:
  await asyncio.to_thread(store.__enter__)
  await asyncio.to_thread(store.raw_client.put_bucket_versioning,Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
  (api.root/'graceful_server.py').write_text(RUNNER,'utf8')
  (api.root/'range_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE,'utf8')
  await start();case=await asyncio.to_thread(api.create_case,'Real S3 log byte ranges')
  nonce=uuid.uuid4().hex;payload=('ab汉🙂\n'*230000+'LOG_END_'+nonce+'\n').encode('utf8')
  script="import sys\nsys.stdout.buffer.write(('ab汉🙂\\n'*230000+'LOG_END_"+nonce+"\\n').encode('utf8'));sys.stdout.buffer.flush()\n"
  if require_lifecycle:
   script="import sys,time\ndata=('ab汉🙂\\n'*230000+'LOG_END_"+nonce+"\\n').encode('utf8')\nsys.stdout.buffer.write(data[:100000]);sys.stdout.buffer.flush();time.sleep(3)\nsys.stdout.buffer.write(data[100000:]);sys.stdout.buffer.flush()\n"
  await call('PUT',api.prefix+'/file',{'path':'Ref/log-output.py','content':script})
  # The real upload may still be settling its storage reservation. Retry only
  # the explicit pre-admission refusal, never an ambiguous accepted request.
  result['setup_refusals']=[];until=time.monotonic()+30
  async with httpx.AsyncClient(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=60) as client:
   while True:
    response=await client.post(api.prefix+'/jobs',json={'command':'"'+sys.executable+'" -X utf8 -B Ref/log-output.py','runtime':'windows','timeout_seconds':120,'max_log_bytes':8*1024*1024})
    value=response.json()
    if response.status_code==409 and isinstance(value.get('detail'),dict) and value['detail'].get('code')=='storage_quota_busy' and time.monotonic()<until:
     result['setup_refusals'].append(value);save();await asyncio.sleep(.25);continue
    response.raise_for_status();job=value;break
  live=None
  if require_lifecycle:
   live,events=await stream_bytes(api,job['id'])
   live_stats=await call('GET','/fixture/log-io/'+str(job['id']),api_prefix=False)
   assert any(e['status']=='running' for e in events) and live_stats['local_owner'] and live_stats['subscribers']==0
   result['live_legacy_sse']={'bytes':len(live),'events':events,'stats':live_stats};save()
  terminal=await asyncio.to_thread(api.wait_job,job['id'],180)
  assert terminal['attempt']['process_stop']['verified'] and terminal['status']=='succeeded'
  result['job']=terminal;result['job_id']=job['id'];save()
  with sqlite3.connect(api.root/'test.sqlite') as db:
   prefix=db.execute('SELECT s3_prefix FROM cases WHERE id=?',(case['id'],)).fetchone()[0]
   email=db.execute('SELECT email FROM users ORDER BY id LIMIT 1').fetchone()[0]
  relative=terminal['log_path']
  response=await asyncio.to_thread(store.client.get_object,Bucket=store.bucket,Key=prefix.rstrip('/')+'/'+relative)
  try:raw=response['Body'].read()
  finally:response['Body'].close()
  assert payload in raw;result['log_bytes']=len(raw);result['log_sha256']=hashlib.sha256(raw).hexdigest();save()
  if live is not None:assert live==raw
  await stop();await start();api.token=(await call('POST','/auth/login',{'email':email,'password':'isolated-test-123'}))['token']
  async def page(label,query,maximum,*,empty=False):
   await call('POST','/fixture/log-io/reset',api_prefix=False)
   value=await call('GET','/jobs/'+str(job['id'])+'/log'+query)
   stats=await call('GET','/fixture/log-io/'+str(job['id']),api_prefix=False)
   assert not stats['local_owner'] and value['available']
   actual=value['chunk'].encode('utf8');assert actual==raw[value['start_offset']:value['offset']]
   row={'label':label,'page':{k:v for k,v in value.items() if k!='chunk'},'returned_utf8_bytes':len(actual),'io':stats};result['checks'].append(row);save()
   if require_ranges:
    assert sum(v['bytes'] for v in stats['reads'])<=maximum
    assert all(v['range'] and v['if_match'] and v['closed'] for v in stats['reads'])
    if empty:assert not stats['reads']
   return value,stats
  first,stats=await page('64 KiB first page','?limit=65536',65536)
  if not require_ranges:
   assert sum(v['bytes'] for v in stats['reads'])==len(raw)>65536
   result['reproduced']=True
  else:
   await page('UTF-8 continuation','?offset='+str(first['offset'])+'&limit=65536',65536)
   await page('end offset bound','?offset=10&limit=65536&end_offset=110',100)
   await page('past end resets cursor','?offset='+str(len(raw)+20)+'&limit=65536',65536)
   await page('exact end has no body read','?offset='+str(len(raw))+'&limit=65536',0,empty=True)
   await page('empty selected interval','?offset=10&limit=65536&end_offset=10',0,empty=True)
   await call('POST','/fixture/log-io/reset',api_prefix=False)
   collected=bytearray();done=False
   async with httpx.AsyncClient(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=60) as client:
    async with client.stream('GET','/jobs/'+str(job['id'])+'/log-stream?offset=0') as stream:
     stream.raise_for_status()
     async for line in stream.aiter_lines():
      if line.startswith('data: '):
       event=json.loads(line[6:]);assert event.get('type')!='error',event
       if event['type']=='log':collected.extend(event['text'].encode('utf8'))
       if event['type']=='done':done=True;break
   stats=await call('GET','/fixture/log-io/'+str(job['id']),api_prefix=False)
   assert done and bytes(collected)==raw
   assert all(v['range'] and v['if_match'] and v['closed'] and v['bytes']<=65536 for v in stats['reads'])
   assert sum(v['bytes'] for v in stats['reads'])<=len(raw)+4*len(stats['reads'])
   result['sse']={'complete_bytes':len(collected),'reads':stats};save()
   full=await call('GET','/jobs/'+str(job['id'])+'/log');assert full['chunk'].encode('utf8')==raw
   result['legacy_full_get_preserved']=True
   if require_lifecycle:await lifecycle(api,job['id'],raw,result,save)
  await stop();result['passed']=require_ranges
 except BaseException as error:result['failure_type']=type(error).__name__;raise
 finally:
  (api.root/'hold-log-reads').unlink(missing_ok=True)
  testsupport.subprocess.Popen=original
  if api.proc is not None and api.proc.poll() is None:
   try:await stop()
   finally:await asyncio.to_thread(api.__exit__,None,None,None)
  elif api.proc is not None:await asyncio.to_thread(api.__exit__,None,None,None)
  await asyncio.to_thread(store.__exit__,None,None,None);result['store_stopped']=store.proc is None or store.proc.poll() is not None;save()
 print(json.dumps({'passed':result['passed'],'reproduced':result.get('reproduced',False),'model_calls':0,'evidence':str(api.root)}),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--review',type=Path,required=True);p.add_argument('--require-ranges',action='store_true');p.add_argument('--require-lifecycle',action='store_true');a=p.parse_args()
 assert not a.require_lifecycle or a.require_ranges
 review=json.loads(a.review.read_text('utf8'));assert review['entry']=='platform/server/job_log_range_capacity_acceptance.py'
 for name,digest in review['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
 asyncio.run(verify(a.require_ranges,a.require_lifecycle))
