"""Real Redis, two real workers, durable batches and actual DeepSeek tool readback."""
import argparse,asyncio,hashlib,json,os,re,socket,subprocess,sys,tempfile,time,uuid
from pathlib import Path
import httpx
from concurrent.futures import ThreadPoolExecutor
from redis import Redis
from testsupport import IsolatedServer,SERVER
from live_llm_acceptance import source,FIXTURE as LIVE
from app.services.owned_process import OwnedProcess,wsl_path

FIXTURE=r'''
from fastapi import Depends
from app.deps import get_current_user
from app.db import SessionLocal
from app.models import Job,JobAttempt,Plan
from app.services import job_delivery
from sqlalchemy import select
@app.get('/api/v1/test/fair-pending')
async def fair_pending(user=Depends(get_current_user)):
 async with SessionLocal() as db:
  rows=(await db.execute(select(Job.id,Job.case_id,Job.status,JobAttempt.phase,JobAttempt.claim_token)
   .join(JobAttempt,JobAttempt.job_id==Job.id).order_by(Job.id))).all()
 return {'pending':await job_delivery.pending(limit=256),
  'rows':[{'id':r[0],'case_id':r[1],'status':r[2],'phase':r[3],'claimed':r[4] is not None} for r in rows]}
@app.post('/api/v1/test/fair-quota')
async def fair_quota(body:dict,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  plan=await db.scalar(select(Plan).where(Plan.code=='free'));values=json.loads(plan.quotas)
  amount=body.get('job_concurrent',1);assert amount in {1,2}
  values['job_concurrent']=amount;plan.quotas=json.dumps(values);await db.commit()
 return {'job_concurrent':amount}
@app.get('/api/v1/test/fair-head/{identifier}')
async def fair_head(identifier:int,user=Depends(get_current_user)):
 from app.models import Case
 from app.services.durable_events import lock_case_execution
 async with SessionLocal() as db:
  job=await db.get(Job,identifier);attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==identifier).order_by(JobAttempt.number.desc()).limit(1))
  value=await job_delivery.intent(db,job,attempt)
  try:
   await lock_case_execution(db,job.case_id)
   await job_delivery.require_capacity(db,await db.get(Case,job.case_id),job,attempt,json.loads(attempt.input_manifest_json)['execution_spec'])
   outcome='eligible'
  except Exception as error:outcome=type(error).__name__+':'+str(error)
 return {'job_id':identifier,'capacity':outcome,'redis_head':await job_delivery.redis_head(identifier,value)}
'''

class Broker:
 def __init__(self,root):
  self.root=root;self.owned=None;self.drain=None
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
 async def start(self):
  base=wsl_path(SERVER.parents[1]/'tmp/delivery-redis-packages/root');self.owned=OwnedProcess()
  argv=['wsl.exe','-d','vibeflow-linux','--exec','env','LD_LIBRARY_PATH='+base+'/usr/lib/x86_64-linux-gnu',
   base+'/usr/bin/redis-server','--bind','127.0.0.1','--port',str(self.port),'--protected-mode','yes','--save','',
   '--appendonly','no','--daemonize','no','--maxmemory','64mb','--maxmemory-policy','noeviction','--dir',wsl_path(self.root)]
  await self.owned.prepare(argv,cwd=self.root,env=dict(os.environ),runtime='wsl')
  await self.owned.verify_registration();await self.owned.release()
  async def drain():
   with (self.root/'redis.log').open('wb') as stream:
    while data:=await self.owned.proc.stdout.read(8192):stream.write(data)
  self.drain=asyncio.create_task(drain())
  from redis.asyncio import Redis as AsyncRedis
  client=AsyncRedis(host='127.0.0.1',port=self.port,socket_connect_timeout=1,socket_timeout=1)
  try:
   deadline=time.monotonic()+20
   while time.monotonic()<deadline:
    try:
     self.run_id=(await client.info('server'))['run_id'];return
    except (OSError,__import__('redis').exceptions.RedisError):await asyncio.sleep(.1)
   raise TimeoutError('Owned Redis did not become ready')
  finally:await client.aclose()
 async def stop(self):
  if self.owned is not None:
   try:
    proof=await self.owned.stop(reason='cancelled',grace_ms=2000)
    (self.root/'redis-stop.json').write_text(json.dumps(proof,indent=2),encoding='utf8');assert proof['verified']
    if self.drain is not None:await self.drain
   finally:self.owned.close()

def verify(configuration,broker,*,database_url=None,flood_count=50,flood_batches_per_case=1):
 api=IsolatedServer('live-fair-queue',app='live_fixture:app',database_url=database_url)
 settings={'VIBEFLOW_DISTRIBUTED_JOBS':'true','VIBEFLOW_REDIS_HOST':'127.0.0.1','VIBEFLOW_REDIS_PORT':str(broker.port)}
 prefix='import os\nos.environ.update('+repr(settings)+')\n'
 (api.root/'live_fixture.py').write_text(prefix+LIVE+FIXTURE,encoding='utf8')
 report={'passed':False,'mock_llm_calls':0,'simulated_llm':False,'host':'api.deepseek.com','model':configuration['model'],
  'checks':[],'workers':[],'developer_database_modified':False,'developer_redis_used':False}
 workers=[];batches=[]
 def save():
  trace=api.root/'upstream.jsonl';report['upstream']=[json.loads(v) for v in trace.read_text().splitlines()] if trace.exists() else []
  value=json.dumps(report,ensure_ascii=False,indent=2);assert configuration['key'] not in value
  (api.root/'live-result.json').write_text(value+'\n',encoding='utf8')
 def call(method,path,payload=None,*,token=None,base=None,expected=200):
  batch_submission=method=='POST' and path.endswith('/jobs:batch') and expected==202
  headers={'Authorization':'Bearer '+(token or api.token)}
  if batch_submission:headers['Idempotency-Key']=uuid.uuid4().hex
  deadline=time.monotonic()+240
  with httpx.Client(base_url=(base or api.base)+'/api/v1',headers=headers,timeout=180,trust_env=False) as client:
   while True:
    started=time.perf_counter()
    try:
     r=client.request(method,path,json=payload)
    except httpx.ReadTimeout:
     if not batch_submission or time.monotonic()>=deadline:
      raise
     report.setdefault('HTTP_timings',[]).append({'method':method,'path':path,'status':'read_timeout',
      'seconds':time.perf_counter()-started,'peer':base is not None})
     continue
    report.setdefault('HTTP_timings',[]).append({'method':method,'path':path,'status':r.status_code,
     'seconds':time.perf_counter()-started,'peer':base is not None})
    if batch_submission and r.status_code==409 and time.monotonic()<deadline:
     detail=r.json().get('detail',{})
     if isinstance(detail,dict) and detail.get('code')=='storage_quota_busy':
      time.sleep(.25)
      continue
    assert r.status_code==expected,(method,path,r.status_code,r.text.replace(configuration['key'],'[REDACTED]')[:600])
    return r.json()
 def wait(predicate,label,seconds=60):
  deadline=time.monotonic()+seconds
  while not predicate():
   if time.monotonic()>=deadline:raise TimeoutError(label)
   time.sleep(.1)
 def wait_stopped(job_id,token,status):
  # Cancellation acknowledgement precedes native shutdown and finalization.
  # Wait for the actual worker's terminal attempt and validated stop receipt.
  def stopped():
   detail=call('GET','/jobs/'+str(job_id),token=token)
   attempt=detail.get('attempt') or {}
   return (detail['status']==status and attempt.get('phase')==status
    and (attempt.get('process_stop') or {}).get('verified') is True
    and not detail.get('completion_pending') and not detail.get('record_issues'))
  wait(stopped,'Actual process cleanup did not settle for job '+str(job_id))
 def script(case_id,token,role):
  code=("from pathlib import Path\nimport time\np=Path("+repr(str(api.root/(role+'-started')))+")\n"
   "with p.open('a') as f:f.write('started'+chr(10))\n"+
   ("deadline=time.monotonic()+60\nwhile not Path("+repr(str(api.root/'allow-a'))+").exists():\n if time.monotonic()>deadline:raise TimeoutError('fixture release missing')\n time.sleep(.05)\n" if role in {'A','C'} else "print('actual queued worker completed')\n"))
  call('PUT',f'/cases/{case_id}/file',{'path':'Ref/fair_queue.py','content':code},token=token)
 def start_workers():
  for n in range(2):
   stop=api.root/('stop-worker-'+str(n));path=api.root/('fair-worker-'+str(n)+'.py')
   path.write_text('''import asyncio,json\nfrom pathlib import Path\nimport live_fixture\nfrom app.services.distributed_worker import create_worker\nasync def main():\n worker=create_worker(max_jobs=1,poll_delay=.1,handle_signals=False)\n task=asyncio.create_task(worker.async_run())\n try:\n  while not Path(STOP).exists() and not task.done():await asyncio.sleep(.1)\n finally:\n  await worker.close();await asyncio.gather(task,return_exceptions=True)\nasyncio.run(main())\n'''.replace('STOP',repr(str(stop))),encoding='utf8')
   log=(api.root/('worker-'+str(n)+'.log')).open('w',encoding='utf8')
   env=dict(api.env,PYTHONPATH=str(api.root)+os.pathsep+str(SERVER),**settings)
   process=subprocess.Popen([sys.executable,'-X','utf8','-B',str(path)],cwd=SERVER,env=env,stdout=log,stderr=subprocess.STDOUT,
    creationflags=subprocess.CREATE_NO_WINDOW)
   workers.append((process,stop,log))
 print('LIVE_FAIR_QUEUE '+str(api.root),flush=True)
 try:
  with api:
   try:
    api.create_case('Flood owner, first case');a1=api.case_id;token_a=api.token
    workspace=api.call('GET','/workspaces')[0]['id'];a2=api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Flood owner, second case'})['id']
    api.create_case('Other owner');b=api.case_id;token_b=api.token
    script(a1,token_a,'A');script(a2,token_a,'A2');script(b,token_b,'B')
    call('POST','/test/fair-quota',{},token=token_a)
    flood1=call('POST',f'/cases/{a1}/jobs:batch',{'commands':['python Ref/fair_queue.py']*flood_count,'max_concurrent':1},token=token_a,expected=202)
    batches.append((a1,flood1['id'],token_a))
    with ThreadPoolExecutor(max_workers=2) as pool:
     backlog=pool.submit(call,'POST',f'/cases/{a2}/jobs:batch',{'commands':['python Ref/fair_queue.py']*flood_count,'max_concurrent':1},token=token_a,expected=202)
     newcomer=pool.submit(call,'POST',f'/cases/{b}/jobs:batch',{'commands':['python Ref/fair_queue.py'],'max_concurrent':1},token=token_b,expected=202)
     flood2,other=backlog.result(),newcomer.result()
    batches.append((a2,flood2['id'],token_a))
    batches.append((b,other['id'],token_b))
    extra_flood_batches=[]
    for _ in range(flood_batches_per_case-1):
     for case in (a1,a2):
      added=call('POST',f'/cases/{case}/jobs:batch',{'commands':['python Ref/fair_queue.py']*flood_count,
       'max_concurrent':1},token=token_a,expected=202)
      extra_flood_batches.append((case,added['id']))
      batches.append((case,added['id'],token_a))
    flood_jobs_per_case=flood_count*flood_batches_per_case
    queued=call('GET','/test/fair-pending',token=token_a);by_id={v['id']:v for v in queued['rows']}
    first=queued['pending'][:3]
    assert {by_id[v]['case_id'] for v in first}=={a1,a2,b},first
    assert len(queued['pending'])==flood_jobs_per_case*2+1 and all(not r['claimed'] for r in queued['rows'])
    # Compare ordinary authenticated readers in both directions. The /test
    # scheduler diagnostics above are available only inside this disposable API.
    for reader,foreign_case,batch in ((token_a,b,other),(token_b,a1,flood1)):
     foreign_job=batch['jobs'][0]['id']
     for route in (f'/cases/{foreign_case}/jobs',f'/cases/{foreign_case}/job-batches/'+batch['id'],
                   f'/jobs/{foreign_job}',f'/jobs/{foreign_job}/log',f'/cases/{foreign_case}/raw?path=Ref/fair_queue.py'):
      hidden=call('GET',route,token=reader,expected=404)
      assert set(hidden)=={'detail'} and 'fair_queue.py' not in str(hidden) and 'started' not in str(hidden),hidden
      if route.startswith('/jobs/'):
       missing=route.replace(f'/jobs/{foreign_job}', '/jobs/2147483647', 1)
       assert hidden==call('GET',missing,token=reader,expected=404)
    report['checks'].append({'name':'cross_tenant_jobs_batches_logs_and_files_hidden','denied_reads':10,
     'simultaneous_batch_submissions':True})
    report['candidate_diagnostics']=[call('GET','/test/fair-head/'+str(v),token=token_a) for v in first];save()
    client=Redis(host='127.0.0.1',port=broker.port);size=client.zcard('vibeflow:jobs');client.close()
    assert size==2,size
    report['checks'].append({'name':'flood_keeps_other_user_and_case_in_first_three',
     'pending_count':flood_jobs_per_case*2+1,'first_three':first,'redis_messages':size});save()
    start_workers()
    wait(lambda:(api.root/'A-started').exists() and (api.root/'B-started').exists(),'Both users did not obtain real workers')
    assert not (api.root/'allow-a').exists()
    other_jobs=call('GET',f'/cases/{b}/job-batches/'+other['id'],token=token_b)['jobs']
    for job in other_jobs:
     wait(lambda:call('GET','/jobs/'+str(job['id']),token=token_b)['status']=='succeeded','Other owner did not finish')
    flood_running=call('GET','/jobs/'+str(flood1['jobs'][0]['id']),token=token_a)
    assert flood_running['status']=='running' and flood_running['attempt']['phase']=='running'
    report['checks'].append({'name':'other_owner_finishes_while_flood_owner_remains_running'});save()
    # A second API owns no in-memory BatchState or worker process.
    peer=IsolatedServer('fair-queue-peer',app='peer_fixture:app')
    shared={k:v for k,v in api.env.items() if k in {'VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_INDEX_ROOT',
     'VIBEFLOW_AUDIT_DIR','VIBEFLOW_RUNTIME_CONFIG','VIBEFLOW_CODEX_STATE_ROOT','VIBEFLOW_JWT_SECRET'}}
    (peer.root/'peer_fixture.py').write_text('import os\nos.environ.update('+repr({**shared,**settings})+')\n'+LIVE+FIXTURE,encoding='utf8')
    with peer:
     for case,batch in extra_flood_batches:
      cancelled_extra=call('POST',f'/cases/{case}/job-batches/{batch}:cancel',token=token_a,base=peer.base)
      assert all(v['status']=='cancelled' for v in cancelled_extra['jobs'])
     call('POST',f'/cases/{a2}/job-batches/'+flood2['id']+':cancel',token=token_b,base=peer.base,expected=404)
     cancelled=call('POST',f'/cases/{a2}/job-batches/'+flood2['id']+':cancel',token=token_a,base=peer.base)
     assert all(v['status']=='cancelled' for v in cancelled['jobs'])
     repeated=call('POST',f'/cases/{a2}/job-batches/'+flood2['id']+':cancel',token=token_a,base=peer.base)
     assert not repeated['ok']
     call('POST',f'/cases/{a1}/job-batches/'+flood1['id']+':cancel',token=token_a,base=peer.base)
    report['peer']={'root':str(peer.root),'exited':peer.proc.poll() is not None}
    wait(lambda:all(v['status']=='cancelled' for v in call('GET',f'/cases/{a1}/job-batches/'+flood1['id'],token=token_a)['jobs']),
     'Running batch cancellation did not drain')
    wait_stopped(flood1['jobs'][0]['id'],token_a,'cancelled')
    assert not (api.root/'A2-started').exists()
    after=call('GET','/test/fair-pending',token=token_a)
    a2_rows=[r for r in after['rows'] if r['case_id']==a2];assert len(a2_rows)==flood_jobs_per_case and not any(r['claimed'] for r in a2_rows)
    report['checks'].append({'name':'peer_cancels_queued_jobs_without_any_claim_or_process',
     'count':flood_jobs_per_case,'wrong_user_refused':True,'repeat_idempotent':True});save()
    # The same user's only capacity slot must become usable after cancellation
    # and after an actual process exits with a nonzero code.
    failed=call('POST',f'/cases/{a1}/jobs:batch',{'commands':['python -c "raise SystemExit(7)"','python -c "print(1234567)"'],
     'max_concurrent':1},token=token_a,expected=202);batches.append((a1,failed['id'],token_a))
    wait(lambda:all(v['status'] in {'failed','succeeded'} for v in call('GET',f'/cases/{a1}/job-batches/'+failed['id'],token=token_a)['jobs']),
     'Capacity was not released after cancelled/failed work')
    finished=call('GET',f'/cases/{a1}/job-batches/'+failed['id'],token=token_a)
    assert [v['status'] for v in finished['jobs']]==['failed','succeeded']
    for row in finished['jobs']:
     detail=call('GET','/jobs/'+str(row['id']),token=token_a)
     assert detail['attempt']['process_stop']['verified']
    report['checks'].append({'name':'one_slot_reused_after_actual_cancel_failure_and_success','statuses':['failed','succeeded']});save()
    call('POST','/test/fair-quota',{'job_concurrent':2},token=token_a)
    script(a1,token_a,'C');script(a2,token_a,'D')
    limited=call('POST',f'/cases/{a1}/jobs:batch',{'commands':['python Ref/fair_queue.py']*2,'max_concurrent':1},token=token_a,expected=202)
    batches.append((a1,limited['id'],token_a));wait(lambda:(api.root/'C-started').exists(),'Limited batch did not start')
    separate=call('POST',f'/cases/{a2}/jobs:batch',{'commands':['python Ref/fair_queue.py'],'max_concurrent':1},token=token_a,expected=202)
    batches.append((a2,separate['id'],token_a))
    wait(lambda:call('GET',f'/cases/{a2}/job-batches/'+separate['id'],token=token_a)['status']=='succeeded',
     'Full batch prevented another ready case from using free owner capacity')
    assert (api.root/'C-started').read_text().splitlines()==['started']
    limited_rows=call('GET',f'/cases/{a1}/job-batches/'+limited['id'],token=token_a)['jobs']
    assert [r['status'] for r in limited_rows]==['running','queued']
    call('POST',f'/cases/{a1}/job-batches/'+limited['id']+':cancel',token=token_a)
    wait_stopped(limited['jobs'][0]['id'],token_a,'cancelled')
    report['checks'].append({'name':'batch_limit_one_does_not_block_another_case_with_owner_limit_two'});save()
    api.token=token_a;api.case_id=a1;api.prefix=f'/cases/{a1}'
    proof='FAIR_QUEUE_'+hashlib.sha256(json.dumps(report['checks'],sort_keys=True).encode()).hexdigest();api.put('Ref/fair-proof.txt',proof)
    profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual queue proof','base_url':configuration['base_url'],
     'default_model':configuration['model'],'api_key':configuration['key']})
    conv=api.call('POST','/conversations',{'case_id':a1,'title':'Queue evidence','permission_mode':'plan',
     'provider_profile_id':profile['id'],'model':configuration['model']})
    events=[]
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+token_a},timeout=180,trust_env=False) as client:
     with client.stream('POST',f'/conversations/{conv["id"]}/messages',json={'content':'请仅调用一次 read_text_file 读取 Ref/fair-proof.txt，原样回复文件内字符串。',
      'provider_profile_id':profile['id'],'model':configuration['model']}) as response:
      response.raise_for_status()
      for line in response.iter_lines():
       if line.startswith('data:'):events.append(json.loads(line[5:]))
    assert ''.join(v.get('text','') for v in events if v.get('type')=='text.delta')==proof
    tools=[v for v in events if v.get('type')=='tool.result'];assert len(tools)==1 and tools[0]['ok'] and proof in tools[0]['result']
    assert any(v.get('type')=='run.completed' for v in events)
    (api.root/'model-events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf8')
    report['model_readback_verified']=True;report['passed']=True
   finally:
    for case_id,batch,token in reversed(batches):
     try:call('POST',f'/cases/{case_id}/job-batches/{batch}:cancel',token=token)
     except Exception as error:report.setdefault('cleanup_errors',[]).append(type(error).__name__)
    for process,stop,log in workers:
     stop.touch()
     try:process.wait(timeout=20)
     except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=10)
     log.close();report['workers'].append({'pid':process.pid,'exit_code':process.returncode})
 except BaseException as error:
  report['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1200]}
 finally:report['api_exited']=api.proc is not None and api.proc.poll() is not None;save()
 print('LIVE_FAIR_QUEUE_RESULT '+str(api.root/'live-result.json'),flush=True)
 return report['passed'],api.root

async def main():
 parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True);parser.add_argument('--postgres',action='store_true');parser.add_argument('--flood-count',type=int,choices=range(1,51),default=50)
 parser.add_argument('--flood-batches-per-case',type=int,choices=(1,2),default=1)
 args=parser.parse_args()
 flood={'flood_count':args.flood_count,'flood_batches_per_case':args.flood_batches_per_case}
 configuration=await source(args.source_profile_id)
 root=Path(tempfile.mkdtemp(prefix='fair-queue-broker-',dir=SERVER.parents[1]/'tmp'));broker=Broker(root)
 report={'passed':False,'developer_database_modified':False}
 try:
  await broker.start();report['redis_run_id']=broker.run_id
  if not args.postgres:
   passed,result=await asyncio.to_thread(verify,configuration,broker,**flood);report.update(passed=passed,root=str(result));return 0 if passed else 1
  import asyncpg
  from sqlalchemy.engine import make_url
  from app.config import Settings
  from workflow_migration_smoke import alembic
  selected=make_url(Settings(_env_file=SERVER/'.env').database_url)
  assert selected.drivername=='postgresql+asyncpg' and selected.host in {'127.0.0.1','localhost','::1'}
  name='tmp_vf_fairqueue_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_fairqueue_[0-9a-f]{32}',name)
  admin=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,password=selected.password,database='postgres',timeout=10)
  created=False
  try:
   await admin.execute('CREATE DATABASE "'+name+'"');created=True
   url=selected.set(database=name).render_as_string(hide_password=False)
   await asyncio.to_thread(alembic,url,root,'upgrade','head')
   passed,result=await asyncio.to_thread(verify,configuration,broker,database_url=url,**flood);report.update(passed=passed,root=str(result));return 0 if passed else 1
  finally:
   if created:
    assert await admin.fetchval('SELECT current_database()')=='postgres';await admin.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
   await admin.close()
 finally:
  try:await broker.stop()
  finally:(root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8');print('FAIR_QUEUE_BROKER '+str(root),flush=True)

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
