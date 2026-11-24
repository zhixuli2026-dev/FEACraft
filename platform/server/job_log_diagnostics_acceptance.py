"""Actual command timeout/output limit and controlled finalization failure; no LLM."""
import argparse,asyncio,base64,hashlib,json,sqlite3,subprocess,sys,time
from pathlib import Path
import httpx
import testsupport
from testsupport import IsolatedServer,SERVER
from s3testsupport import ObjectStore
from job_log_range_capacity_acceptance import FIXTURE
from terminal_shutdown_acceptance import RUNNER
from app.services import process_identity_native as identity

ROOT=SERVER.parents[1]
FAULT='''
import hashlib,base64
from app.main import app as api_app
original_summary=jobrunner._persist_summary
async def summary(job_id):
 if Path('fail-summary').exists():
  proc=jobrunner.get_proc(job_id)
  Path('fault-log.json').write_text(json.dumps({'job_id':job_id,'sha256':hashlib.sha256(proc.log_path.read_bytes()).hexdigest(),'bytes':proc.log_path.stat().st_size}))
  raise OSError('Isolated finalization fault after actual log close')
 return await original_summary(job_id)
jobrunner._persist_summary=summary
@api_app.get('/fixture/log-process/{job_id}')
async def process(job_id:int,user=Depends(get_current_user)):
 proc=jobrunner.get_proc(job_id);owned=proc._owned_process
 return {'phase':proc.phase,'pump_done':proc._pump_task.done(),'error':proc.completion_error,
  'record':owned.record,'stop':owned.stop_result,'closed':owned.closed,
  'log_sha256':hashlib.sha256(proc.log_path.read_bytes()).hexdigest(),
  'log_base64':base64.b64encode(proc.log_path.read_bytes()).decode('ascii'),
  'diagnostics':proc._diagnostics,'diagnostic_bytes':proc._diagnostic_bytes}
'''


async def consume(api,job_id,*,cursor=False):
 text=[];diagnostics=[];terminal=None
 async with httpx.AsyncClient(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=80) as client:
  async with client.stream('GET','/jobs/'+str(job_id)+'/log-stream'+('?offset=0' if cursor else '')) as stream:
   stream.raise_for_status()
   async for line in stream.aiter_lines():
    if not line.startswith('data: '):continue
    event=json.loads(line[6:])
    if event['type']=='log':
     (diagnostics if event.get('diagnostic') else text).append(event['text'])
    if event['type'] in {'done','error'}:terminal=event;break
 assert terminal is not None
 return {'text':''.join(text),'diagnostics':''.join(diagnostics),'terminal':terminal}


async def verify(backend):
 api=IsolatedServer('job-log-diagnostics-'+backend,app='diagnostic_fixture:app')
 store=ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed',volume_max=8) if backend=='s3' else None
 result={'passed':False,'backend':backend,'model_calls':0,'mock_llm_calls':0,'jobs':[],'api_exits':[],'setup_refusals':[]};original=subprocess.Popen
 def save():(api.root/'diagnostics-result.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
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
 async def submit(script,seconds,limit):
  await call('PUT',api.prefix+'/file',{'path':'Ref/diagnostic.py','content':script})
  until=time.monotonic()+30
  async with httpx.AsyncClient(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=60) as client:
   while True:
    r=await client.post(api.prefix+'/jobs',json={'command':'"'+sys.executable+'" -X utf8 -B Ref/diagnostic.py','runtime':'windows','timeout_seconds':seconds,'max_log_bytes':limit})
    v=r.json()
    if r.status_code==409 and isinstance(v.get('detail'),dict) and v['detail'].get('code')=='storage_quota_busy' and time.monotonic()<until:
     result['setup_refusals'].append(v);save();await asyncio.sleep(.25);continue
    if r.status_code!=200:result['submission_failure']={'status':r.status_code,'body':v};save()
    r.raise_for_status();return v
 async def observed(job_id):
  until=time.monotonic()+30
  while True:
   value=await call('GET','/fixture/log-process/'+str(job_id),api_prefix=False)
   if value['pump_done'] and value['closed']:break
   assert time.monotonic()<until,value['phase'];await asyncio.sleep(.05)
  assert value['stop']['verified'] and identity.probe(value['record']['host'])['state']=='exited'
  value['independent_root']=identity.probe(value['record']['host']);return value
 try:
  env=''
  if store:
   await asyncio.to_thread(store.__enter__)
   await asyncio.to_thread(store.raw_client.put_bucket_versioning,Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
   env='import os\nos.environ.update('+repr(store.settings())+')\n'
  (api.root/'graceful_server.py').write_text(RUNNER,'utf8');(api.root/'diagnostic_fixture.py').write_text(env+FIXTURE+FAULT,'utf8')
  await start();case=await asyncio.to_thread(api.create_case,'Actual diagnostic compatibility')
  with sqlite3.connect(api.root/'test.sqlite') as db:
   prefix=db.execute('SELECT s3_prefix FROM cases WHERE id=?',(case['id'],)).fetchone()[0]
   email=db.execute('SELECT email FROM users ORDER BY id LIMIT 1').fetchone()[0]
  async def recover_archive(row):
   job_id=row['terminal']['id']
   before=await call('GET','/jobs/'+str(job_id)+'/log?limit=65536')
   assert not before['available'],'Exhausted original budget must not silently start another upload'
   options=await call('GET','/jobs/'+str(job_id)+'/output-recovery')
   assert options['state']=='recoverable' and options['native_execution_started'] is False
   payload={'attempt_id':row['terminal']['attempt']['id'],'inventory_sha256':options['inventory_sha256']}
   with sqlite3.connect(api.root/'test.sqlite') as db:
    previous_checkpoint=json.loads(db.execute('SELECT write_checkpoint_json FROM job_attempts WHERE id=?',(payload['attempt_id'],)).fetchone()[0])
   recovered=await call('POST','/jobs/'+str(job_id)+':recover-outputs',payload)
   assert recovered['state']=='archived' and recovered['native_execution_started'] is False
   after=await call('GET','/jobs/'+str(job_id));assert after['status']=='failed' and after['log_path']!=row['terminal']['log_path']
   response=await asyncio.to_thread(store.client.get_object,Bucket=store.bucket,Key=prefix.rstrip('/')+'/'+after['log_path'])
   try:archived=response['Body'].read()
   finally:response['Body'].close()
   assert hashlib.sha256(archived).hexdigest()==row['captured_sha256']
   # Actual isolated historical-row compatibility: completed archive, old
   # source pointer. A repeated explicit recovery must repair it idempotently.
   with sqlite3.connect(api.root/'test.sqlite') as db:
    changed=db.execute('UPDATE jobs SET log_s3_key=? WHERE id=? AND log_s3_key=? AND status=?',(row['terminal']['log_path'],job_id,after['log_path'],'failed'))
    assert changed.rowcount==1;db.commit()
   repeated=await call('POST','/jobs/'+str(job_id)+':recover-outputs',payload);assert repeated==recovered
   repaired=await call('GET','/jobs/'+str(job_id));assert repaired['log_path']==after['log_path'] and repaired['status']=='failed'
   with sqlite3.connect(api.root/'test.sqlite') as db:
    checkpoint=json.loads(db.execute('SELECT write_checkpoint_json FROM job_attempts WHERE id=?',(payload['attempt_id'],)).fetchone()[0])
    rebound=[json.loads(v[0]) for v in db.execute('SELECT payload_json FROM durable_events WHERE attempt_id=? AND type=?',(payload['attempt_id'],'attempt.archive_drain_rebound'))]
   assert previous_checkpoint['schema']==checkpoint['schema']=='vibeflow.job-write-checkpoint.v2' and len(rebound)==1
   assert {k:v for k,v in previous_checkpoint.items() if k not in {'recorded_at','recovery'}}=={k:v for k,v in checkpoint.items() if k not in {'recorded_at','recovery'}}
   assert {k:v for k,v in previous_checkpoint['recovery'].items() if k!='snapshot_sha256'}=={k:v for k,v in checkpoint['recovery'].items() if k!='snapshot_sha256'}
   assert previous_checkpoint['recovery']['snapshot_sha256']!=checkpoint['recovery']['snapshot_sha256']
   row['archive_recovery']={'before_available':before['available'],'options':options,'recovered':recovered,'log_path':after['log_path'],'legacy_pointer_repaired':True,'native_execution_started':False,'original_drain_proof_preserved':True,'drain_rebinding':rebound};save()
  for mode in ('timeout','log_limit'):
   script=("import time\nprint('实际超时开始🙂',flush=True)\ntime.sleep(120)\n" if mode=='timeout' else "import sys\nsys.stdout.buffer.write(('ab汉🙂\\n'*20000).encode('utf8'));sys.stdout.buffer.flush()\n")
   maximum=16384 if mode=='timeout' else 1024
   job=await submit(script,10 if mode=='timeout' else 120,maximum)
   streamed=await consume(api,job['id']);assert streamed['terminal']['type']=='done',streamed['terminal']
   terminal=await asyncio.to_thread(api.wait_job,job['id'],180,expected='failed')
   assert terminal['attempt']['process_stop']['verified']
   process=await observed(job['id'])
   raw=base64.b64decode(process.pop('log_base64'));assert hashlib.sha256(raw).hexdigest()==process['log_sha256']
   row={'mode':mode,'terminal':terminal,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'stream':streamed,'process':process};result['jobs'].append(row);save()
   assert '[resource limit]' in raw.decode('utf8','replace') and len(raw)<=maximum+65536
   assert streamed['text']==raw.decode('utf8','replace') and not streamed['diagnostics']
   assert process['diagnostics'] and all(v['persisted'] for v in process['diagnostics']) and process['diagnostic_bytes']<=65536
   if store:
    output=terminal['attempt']['outputs'];captured=output['finalizing_source']
    capture_path=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/prefix/output['run_path']/captured['log_path']
    assert capture_path.resolve().is_relative_to(api.root.resolve())
    captured_bytes=capture_path.read_bytes()
    assert {'size':len(captured_bytes),'sha256':hashlib.sha256(captured_bytes).hexdigest()}==captured['log']
    assert '[resource limit]' in captured_bytes.decode('utf8','replace')
    row.update(captured_sha256=captured['log']['sha256'],captured_bytes=len(captured_bytes),captured_text=captured_bytes.decode('utf8','replace'))
    options=await call('GET','/jobs/'+str(job['id'])+'/output-recovery')
    assert options['state']=='owner_unavailable_for_recovery'
    refusal=await call('POST','/jobs/'+str(job['id'])+':recover-outputs',{'attempt_id':terminal['attempt']['id'],'inventory_sha256':options['inventory_sha256']},expect=409)
    assert refusal['detail']['code']=='recovery_required';row['live_owner_recovery_refused']=refusal;save()
    await stop();await start();api.token=(await call('POST','/auth/login',{'email':email,'password':'isolated-test-123'}))['token']
    await recover_archive(row)
  if not store:
   await stop();await start();api.token=(await call('POST','/auth/login',{'email':email,'password':'isolated-test-123'}))['token']
  for row in result['jobs']:
   job_id=row['terminal']['id']
   for cursor in (False,True):
    streamed=await consume(api,job_id,cursor=cursor)
    assert streamed['terminal']['type']=='done' and streamed['text']==row.get('captured_text',row['stream']['text']) and not streamed['diagnostics']
   row['restart_parity']=True;save()
  (api.root/'fail-summary').touch()
  job=await submit("print('实际收尾失败之前的原始输出🙂',flush=True)\n",120,16384)
  legacy=await consume(api,job['id']);cursor=await consume(api,job['id'],cursor=True);process=await observed(job['id'])
  process.pop('log_base64')
  before=json.loads((api.root/'fault-log.json').read_text('utf8'))
  result['finalization_fault']={'job_id':job['id'],'legacy':legacy,'cursor':cursor,'process':process,'log_before_fault':before};save()
  assert legacy['terminal']['type']==cursor['terminal']['type']=='error' and legacy['terminal']['code']==cursor['terminal']['code']=='persistence_failed'
  assert '[finalization error]' in legacy['diagnostics'] and 'OSError' in legacy['diagnostics'] and not cursor['diagnostics']
  assert legacy['text']==cursor['text'] and before['sha256']==process['log_sha256']
  assert process['diagnostics'] and not any(v['persisted'] for v in process['diagnostics'])
  (api.root/'fail-summary').unlink();await stop();result['passed']=True
 except BaseException as error:result['failure_type']=type(error).__name__;raise
 finally:
  testsupport.subprocess.Popen=original;(api.root/'fail-summary').unlink(missing_ok=True)
  if api.proc is not None and api.proc.poll() is None:
   try:await stop()
   finally:await asyncio.to_thread(api.__exit__,None,None,None)
  elif api.proc is not None:await asyncio.to_thread(api.__exit__,None,None,None)
  if store:
   await asyncio.to_thread(store.__exit__,None,None,None);result['store_stopped']=store.proc is None or store.proc.poll() is not None
  save()
 print(json.dumps({'passed':result['passed'],'backend':backend,'evidence':str(api.root),'model_calls':0}),flush=True)


if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--review',type=Path,required=True);p.add_argument('--backend',choices=['fs','s3'],required=True);a=p.parse_args()
 review=json.loads(a.review.read_text('utf8'));assert review['entry']=='platform/server/job_log_diagnostics_acceptance.py'
 for name,digest in review['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
 asyncio.run(verify(a.backend))
