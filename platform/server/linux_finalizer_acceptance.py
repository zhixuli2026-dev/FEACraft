"""Actual document compilation and API death during independent finalization; no LLM."""
import argparse,concurrent.futures,hashlib,io,json,os,secrets,shutil,sqlite3,sys,time
from pathlib import Path
import httpx
from pypdf import PdfReader
from testsupport import IsolatedServer

FIXTURE='''
import asyncio,json,os
from pathlib import Path
root=Path.cwd()
os.environ.update(json.loads((root/'private/storage.json').read_text('utf8')))
os.environ['NO_PROXY']=os.environ['no_proxy']='127.0.0.1,localhost,vf-finalizer-store-20261118'
from app.main import app
from app.services import job_execution
root=Path.cwd()
original=job_execution.finish
async def finish(*args,**kwargs):
 if (root/'hold-original').exists():
  (root/'original-finish-held.json').write_text(json.dumps({'job_id':args[0].job_id,'actual_original_finish_not_called':True}))
  await asyncio.Event().wait()
 return await original(*args,**kwargs)
job_execution.finish=finish
if os.name!='nt':
 from app.services import finalizing_linux_scope
 original_scope_write=finalizing_linux_scope.write
 async def scope_write(owner,child_id,stage,value,**kwargs):
  await original_scope_write(owner,child_id,stage,value,**kwargs)
  if stage=='namespace' and (root/'hold-namespace').exists():
   (root/'namespace-held.json').write_text(json.dumps({'request_id':owner.id,'child_id':child_id,'namespace':value}))
   await asyncio.Event().wait()
 finalizing_linux_scope.write=scope_write
'''

def read(path):return json.loads(path.read_text('utf8'))
def write(path,value):path.write_text(json.dumps(value,indent=2)+'\n','utf8')
def sha(data):return hashlib.sha256(data).hexdigest()
def wait_for(function,timeout=180):
 until=time.monotonic()+timeout
 while True:
  value=function()
  if value:return value
  if time.monotonic()>until:raise TimeoutError('Actual finalizer checkpoint did not arrive')
  time.sleep(.02)

class Run:
 def __init__(self,windows,endpoint,boundary):
  self.api=IsolatedServer('linux-finalizer' if not windows else 'windows-finalizer',app='native_finalizer_app:app')
  self.root=self.api.root;self.windows=windows;self.result={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'windows':windows,'checks':[],'API_births':[]}
  self.boundary=boundary;self.result['interruption_boundary']=boundary
  from urllib.parse import urlsplit
  selected=urlsplit(endpoint)
  assert selected.scheme=='http' and selected.hostname in {'127.0.0.1','vf-finalizer-store-20261118'},'Dedicated local test store required'
  bucket='test-finalizer-'+secrets.token_hex(16)
  import boto3
  from botocore.config import Config
  store=boto3.client('s3',endpoint_url=endpoint,aws_access_key_id='test',aws_secret_access_key='test',region_name='us-east-1',
   config=Config(connect_timeout=5,read_timeout=30,retries={'max_attempts':0},s3={'addressing_style':'path'},proxies={}))
  store.create_bucket(Bucket=bucket)
  (self.root/'private').mkdir()
  write(self.root/'private/storage.json',{'VIBEFLOW_STORAGE_BACKEND':'s3','VIBEFLOW_ALLOW_PARTIAL_S3':'true',
   'VIBEFLOW_S3_ENDPOINT':endpoint,'VIBEFLOW_S3_BUCKET':bucket,'VIBEFLOW_S3_ACCESS_KEY':'test','VIBEFLOW_S3_SECRET_KEY':'test'})
  self.result['storage']={'backend':'s3','endpoint':endpoint,'bucket':bucket,'dedicated_test_store':True}
  (self.root/'native_finalizer_app.py').write_text(FIXTURE,'utf8');self.client=None
  self.result_path=self.root/'finalizer-result.json';self.save();print('FINALIZER_EVIDENCE '+str(self.root),flush=True)
 def save(self):write(self.result_path,self.result)
 def db(self,query,args=()):
  with sqlite3.connect((self.root/'test.sqlite').as_uri()+'?mode=ro',uri=True) as db:
   db.row_factory=sqlite3.Row;return [dict(row) for row in db.execute(query,args)]
 def start(self):
  if (self.root/'api.log').exists():shutil.copyfile(self.root/'api.log',self.root/f"api-{len(self.result['API_births'])}.log")
  self.api.__enter__()
  from app.services.process_identity_native import observe
  self.result['API_births'].append(observe(self.api.proc.pid));self.save()
  self.client=httpx.Client(base_url=self.api.base+'/api/v1',trust_env=False,timeout=300)
  if hasattr(self,'account'):self.client.headers['Authorization']='Bearer '+self.call('POST','/auth/login',json=self.account)['token']
 def call(self,method,path,**kwargs):
  r=self.client.request(method,path,**kwargs);assert r.status_code in (200,201,202),(path,r.status_code,r.text[:1000]);return r.json()
 def kill(self):
  self.api.proc.kill();self.api.proc.wait(timeout=15);self.api.log.close();self.client.close()
  from app.services.process_identity_native import probe
  assert probe(self.result['API_births'][-1])=={'state':'exited','reason':'process_exited'}
 def attempt(self):return self.db('SELECT * FROM job_attempts WHERE job_id=? ORDER BY number DESC LIMIT 1',(self.jid,))[0]
 def active_compiler(self):
  if self.boundary=='namespace':
   if not (self.root/'namespace-held.json').exists():return None
   observed=read(self.root/'namespace-held.json')
   row=self.db('SELECT children_json FROM native_requests WHERE id=?',(observed['request_id'],))[0]
   observed['child']=json.loads(row['children_json'])['children'][observed.pop('child_id')]
   assert observed['child']['identity'] is None
   return observed
  rows=self.db("SELECT * FROM native_requests WHERE kind='job.finalize' AND phase='active'")
  for row in rows:
   children=json.loads(row['children_json']).get('children',{})
   for child in children.values():
    if child['purpose']!='latex_compile_linux' or child['phase']!='running':continue
    events=self.db("SELECT payload_json FROM durable_events WHERE type='job.finalizer_linux_namespace'")
    for event in events:
     value=json.loads(event['payload_json'])
     if value['binding']['child_id']!=child['id']:continue
     namespace=value['value']['init']['pid_namespace']
     for p in Path('/proc').iterdir():
      if not p.name.isdigit():continue
      try:
       if p.joinpath('comm').read_text().strip() not in ('pdflatex','pdftex'):continue
       if os.readlink(p/'ns/pid')!='pid:['+namespace+']':continue
       from app.services.process_identity_native import observe
       return {'request_id':row['id'],'child':child,'namespace':value['value'],'actual_compiler':observe(int(p.name))}
      except (OSError,ValueError):continue
  return None
 def run(self):
  try:
   (self.root/'hold-original').write_text('Interrupt after actual compilation and archive, before original completion.','utf8')
   self.start();self.account={'email':'finalizer-'+secrets.token_hex(6)+'@example.com','password':secrets.token_hex(24)}
   self.client.headers['Authorization']='Bearer '+self.call('POST','/auth/register',json={**self.account,'name':'Native finalizer acceptance'})['token']
   write(self.root/'private/account.json',self.account)
   workspace=self.call('GET','/workspaces')[0]['id'];case=self.call('POST',f'/workspaces/{workspace}/cases',json={'name':'Actual recovery document'})
   self.prefix='/cases/'+str(case['id']);marker='FINALIZER'+secrets.token_hex(6)
   main='E-report/recovery/main.tex';content='\\documentclass{article}\n\\begin{document}\n'+marker+'\n'+('\\newpage Actual independently compiled document.\n'*39)+'\\end{document}\n'
   self.call('PUT',self.prefix+'/file',json={'path':main,'content':content})
   built=self.call('POST',self.prefix+'/latex:build',json={'main':main,'recipe':'pdflatex'});self.jid=built['job_id'];self.result['job_id']=self.jid;self.result['publication_id']=built['publication_id'];self.save()
   wait_for(lambda:(self.root/'original-finish-held.json').exists());before=self.attempt();write(self.root/'original-attempt.json',before)
   assert before['phase']=='finalizing';self.result['original_input_sha256']=sha(before['input_manifest_json'].encode())
   self.kill();(self.root/'hold-original').unlink();self.start()
   options=self.call('GET',f'/jobs/{self.jid}/output-recovery');payload={k:options[k] for k in ('attempt_id','inventory_sha256')}
   self.call('POST',f'/jobs/{self.jid}:recover-outputs',json=payload)
   if not self.windows:
    if self.boundary=='namespace':(self.root/'hold-namespace').write_text('Pause after actual namespace observation, before business registration.','utf8')
    with concurrent.futures.ThreadPoolExecutor() as pool:
     future=pool.submit(self.client.post,f'/jobs/{self.jid}:finalize-outputs',json=payload)
     def compiling():
      found=self.active_compiler()
      if found:return found
      if future.done():
       response=future.result();raise AssertionError(('Finalization completed before compiler observation',response.status_code,response.text[:2000]))
     observed=wait_for(compiling);write(self.root/'killed-validator.json',observed);self.result['killed_validator']=observed['request_id'];self.save()
     self.kill()
     try:future.result(timeout=20)
     except httpx.HTTPError:pass
    if self.boundary=='namespace':(self.root/'hold-namespace').unlink()
    self.start()
   done=self.call('POST',f'/jobs/{self.jid}:finalize-outputs',json=payload);self.result['finalization_response']=done
   assert done['state']=='succeeded',done
   after=self.attempt();write(self.root/'final-attempt.json',after)
   for key in ('id','input_manifest_json','process_identity_json','child_processes_json'):
    assert after[key]==before[key],key
   assert after['phase']=='succeeded' and self.db('SELECT count(*) AS n FROM job_attempts WHERE job_id=?',(self.jid,))[0]['n']==1
   rows=self.db("SELECT id,phase,error,children_json FROM native_requests WHERE kind='job.finalize' ORDER BY created_at")
   write(self.root/'finalizer-requests.json',rows)
   assert rows[-1]['phase']=='succeeded'
   for child in json.loads(rows[-1]['children_json'])['children'].values():assert child['stop']['verified'] and child['stop']['scope_empty']
   if not self.windows:
    assert len(rows)==2 and rows[0]['error']=='OwnerExited'
    old=json.loads(rows[0]['children_json'])['children'][observed['child']['id']]
    assert old['identity']==observed['child']['identity'] and old['stop']==observed['child']['stop'] is None
    from app.services.process_identity_native import probe
    births=[observed['namespace']['init'],observed['namespace']['process']['host']]
    if self.boundary=='running':births.append(observed['actual_compiler'])
    for birth in births:assert probe(birth)=={'state':'exited','reason':'process_exited'}
    closures=self.db("SELECT payload_json FROM durable_events WHERE type='job.finalizer_linux_closed'")
    proofs=[json.loads(r['payload_json']) for r in closures]
    write(self.root/'linux-closures.json',proofs);assert proofs
    if self.boundary=='namespace':
     retired=next(p['value'] for p in proofs if p['binding']['child_id']==observed['child']['id'])
     assert retired['business_not_released'] and retired['authority']=='owner_exit' and retired['unknown_bootstrap_exit_inferred'] is False
   document=self.call('GET',self.prefix+'/latex/results',params={'publication_id':built['publication_id']})
   assert not document['publication']['stale'] and document['publication']['aliases_applied']
   pdf=self.client.get(self.prefix+'/latex/artifact',params={'publication_id':built['publication_id'],'path':built['pdf']});pdf.raise_for_status()
   assert sha(pdf.content)==document['manifest']['artifacts'][built['pdf']]['sha256']
   reader=PdfReader(io.BytesIO(pdf.content));assert len(reader.pages)==40 and marker in reader.pages[0].extract_text()
   (self.root/'actual-recovered.pdf').write_bytes(pdf.content);write(self.root/'document.json',document)
   replay=self.call('POST',f'/jobs/{self.jid}:finalize-outputs',json=payload);assert replay==done
   assert self.db("SELECT count(*) AS n FROM native_requests WHERE kind='job.finalize'")[0]['n']==len(rows)
   self.result.update(passed=True,checks=['actual_original_API_death',f'independent_validator_{self.boundary}_API_death_and_namespace_retirement' if not self.windows else 'Windows_WSL_independent_finalization',
    'same_attempt_and_original_process_journals_preserved','actual_40_page_PDF_and_idempotent_recovery'])
  except BaseException as error:self.result['failure']={'type':type(error).__name__,'message':str(error)[:2000]};raise
  finally:
   if self.client:self.client.close()
   if self.api.proc is not None and self.api.proc.poll() is None:self.api.__exit__(None,None,None)
   self.result['last_API_exited']=self.api.proc is None or self.api.proc.poll() is not None;self.save()

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('review',type=Path);p.add_argument('--windows',action='store_true');p.add_argument('--s3-endpoint',required=True);p.add_argument('--boundary',choices=['running','namespace'],default='running');args=p.parse_args()
 review=read(args.review);assert review['classification']=='no_llm'
 for name,digest in review['sources'].items():
  assert name.startswith('platform/server/')
  source=Path(__file__).resolve().parents[2]/name if os.name=='nt' else Path(__file__).resolve().parent/name.removeprefix('platform/server/')
  assert sha(source.read_bytes())==digest,name
 assert args.windows==(os.name=='nt');assert not args.windows or args.boundary=='running';Run(args.windows,args.s3_endpoint,args.boundary).run()
