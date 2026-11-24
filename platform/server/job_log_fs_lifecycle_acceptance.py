"""Actual FS jobs, live/after-restart logs and real cancellation; no model calls."""
import argparse,asyncio,hashlib,json,sqlite3,subprocess,sys
from pathlib import Path
import httpx
import testsupport
from testsupport import IsolatedServer,SERVER
from job_log_range_capacity_acceptance import FIXTURE,stream_bytes
from terminal_shutdown_acceptance import RUNNER
from app.services import process_identity_native as identity

ROOT=SERVER.parents[1]


async def verify():
 api=IsolatedServer('job-log-fs-lifecycle',app='fs_log_fixture:app')
 result={'passed':False,'model_calls':0,'mock_llm_calls':0,'jobs':[],'api_exits':[]};original=subprocess.Popen
 def save():(api.root/'fs-log-result.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
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
  (api.root/'graceful_server.py').write_text(RUNNER,'utf8');(api.root/'fs_log_fixture.py').write_text(FIXTURE,'utf8')
  await start();case=await asyncio.to_thread(api.create_case,'Real FS log lifecycle')
  with sqlite3.connect(api.root/'test.sqlite') as db:
   prefix=db.execute('SELECT s3_prefix FROM cases WHERE id=?',(case['id'],)).fetchone()[0]
   email=db.execute('SELECT email FROM users ORDER BY id LIMIT 1').fetchone()[0]
  outputs={}
  for cancel in (False,True):
   script=("import sys,time\nsys.stdout.buffer.write(('ab汉🙂\\n'*10000).encode('utf8'));sys.stdout.buffer.flush()\ntime.sleep(3)\nsys.stdout.buffer.write(('第二段🙂\\n'*10000).encode('utf8'));sys.stdout.buffer.flush()\n" if not cancel else
    "import sys,time\nfor i in range(1000):\n sys.stdout.buffer.write((str(i)+' 实际取消🙂\\n').encode('utf8'));sys.stdout.buffer.flush();time.sleep(.2)\n")
   await call('PUT',api.prefix+'/file',{'path':'Ref/fs-log.py','content':script})
   job=await call('POST',api.prefix+'/jobs',{'command':'"'+sys.executable+'" -X utf8 -B Ref/fs-log.py','runtime':'windows','timeout_seconds':120})
   job_id=job['id'];collected=bytearray();events=[];cancelled=False;done=None
   async with httpx.AsyncClient(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=60) as client:
    async with client.stream('GET','/jobs/'+str(job_id)+'/log-stream') as stream:
     stream.raise_for_status()
     async for line in stream.aiter_lines():
      if not line.startswith('data: '):continue
      event=json.loads(line[6:]);assert event.get('type')!='error',event
      if event['type']=='log':
       collected.extend(event['text'].encode('utf8'));events.append({k:v for k,v in event.items() if k!='text'})
       if cancel and not cancelled:
        reply=await client.post('/jobs/'+str(job_id)+':cancel');reply.raise_for_status();assert reply.json()['ok'];cancelled=True
      if event['type']=='done':done=event;break
   terminal=await asyncio.to_thread(api.wait_job,job_id,180,expected='cancelled' if cancel else 'succeeded')
   assert done and terminal['status']==('cancelled' if cancel else 'succeeded') and terminal['attempt']['process_stop']['verified']
   raw=(Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/prefix/terminal['log_path']).read_bytes()
   assert bytes(collected)==raw and any(e['status']=='running' for e in events)
   stats=await call('GET','/fixture/log-io/'+str(job_id),api_prefix=False)
   assert stats['local_owner'] and stats['subscribers']==0
   outputs[job_id]=raw
   result['jobs'].append({'terminal':terminal,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'done':done,'live_events':events,'stats':stats});save()
  await stop();await start();api.token=(await call('POST','/auth/login',{'email':email,'password':'isolated-test-123'}))['token']
  for record in result['jobs']:
   job_id=record['terminal']['id'];raw=outputs[job_id]
   first=await call('GET','/jobs/'+str(job_id)+'/log?limit=65536')
   assert first['chunk'].encode('utf8')==raw[:first['offset']] and first['offset']<=65536
   full=await call('GET','/jobs/'+str(job_id)+'/log');assert full['chunk'].encode('utf8')==raw
   detail=await call('GET','/jobs/'+str(job_id));assert detail['log_backlog']==raw[-4000:].decode('utf8','ignore')
   for suffix in ('','?offset=0'):
    data,events=await stream_bytes(api,job_id,suffix);assert data==raw
   stats=await call('GET','/fixture/log-io/'+str(job_id),api_prefix=False)
   record['restart_observation']=stats;save()
   assert not stats['local_owner'] and not stats['reads'] and stats['log_db_checked_out']==0
   record['after_restart']={'first_offset':first['offset'],'full_get_bytes':len(raw),'legacy_and_cursor_sse':True,'detail_tail':True,'stats':stats};save()
  await stop();result['passed']=True
 except BaseException as error:result['failure_type']=type(error).__name__;raise
 finally:
  testsupport.subprocess.Popen=original
  if api.proc is not None and api.proc.poll() is None:
   try:await stop()
   finally:await asyncio.to_thread(api.__exit__,None,None,None)
  elif api.proc is not None:await asyncio.to_thread(api.__exit__,None,None,None)
  save()
 print(json.dumps({'passed':result['passed'],'evidence':str(api.root),'actual_jobs':len(result['jobs']),'model_calls':0}),flush=True)


if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--review',type=Path,required=True);a=p.parse_args()
 review=json.loads(a.review.read_text('utf8'));assert review['entry']=='platform/server/job_log_fs_lifecycle_acceptance.py'
 for name,digest in review['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
 asyncio.run(verify())
