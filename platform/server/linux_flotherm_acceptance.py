"""Actual wmake/CDLL, published library execution and retained failure outputs; no LLM."""
import argparse,hashlib,json,os,secrets,shlex,sqlite3,time
from pathlib import Path
from urllib.parse import urlsplit
import boto3,httpx
from botocore.config import Config
from testsupport import IsolatedServer

FIXTURE='''import json,os
from pathlib import Path
os.environ.update(json.loads((Path.cwd()/'private/storage.json').read_text('utf8')))
os.environ['NO_PROXY']=os.environ['no_proxy']='127.0.0.1,localhost,vf-finalizer-store-20261118'
from app.main import app
'''
SOURCE='extern "C" double vfFlothermPowerScale() { return VALUE; }\n'
DEST='UDF/flotherm-helper';LIB=DEST+'/libflothermHelper.so'
def read(p):return json.loads(p.read_text('utf8'))
def write(p,v):p.write_text(json.dumps(v,indent=2)+'\n','utf8')
def sha(data):return hashlib.sha256(data).hexdigest()

class Run:
 def __init__(self,windows,endpoint,cancel_only=False):
  assert urlsplit(endpoint).hostname in {'127.0.0.1','vf-finalizer-store-20261118'}
  self.windows=windows;self.api=IsolatedServer(('windows-' if windows else 'linux-')+getattr(self,'label','flotherm'),app='flotherm_acceptance_app:app')
  self.cancel_only=cancel_only
  self.root=self.api.root;self.result={'passed':False,'windows':windows,'real_model_calls':0,'mock_llm_calls':0,'jobs':[],'checks':[]}
  self.bucket='test-flotherm-'+secrets.token_hex(16)
  self.store=boto3.client('s3',endpoint_url=endpoint,aws_access_key_id='test',aws_secret_access_key='test',region_name='us-east-1',
   config=Config(connect_timeout=5,read_timeout=30,retries={'max_attempts':0},s3={'addressing_style':'path'},proxies={}))
  self.store.create_bucket(Bucket=self.bucket)
  (self.root/'private').mkdir()
  write(self.root/'private/storage.json',{'VIBEFLOW_STORAGE_BACKEND':'s3','VIBEFLOW_ALLOW_PARTIAL_S3':'true','VIBEFLOW_S3_ENDPOINT':endpoint,
   'VIBEFLOW_S3_BUCKET':self.bucket,'VIBEFLOW_S3_ACCESS_KEY':'test','VIBEFLOW_S3_SECRET_KEY':'test'})
  self.result['storage']={'endpoint':endpoint,'bucket':self.bucket,'database':'isolated SQLite'}
  (self.root/'flotherm_acceptance_app.py').write_text(FIXTURE,'utf8');self.save()
  print('FLOTHERM_EVIDENCE '+str(self.root),flush=True)
 def cancel_compiler(self,original):
  text=SOURCE.replace('VALUE','2.5')+''.join('extern "C" double f%d(double x) { return x*%d.0; }\n'%(i,i+1) for i in range(12000))
  self.put(DEST+'/flothermHelper.C',text)
  jid=self.call('POST',self.prefix+'/flotherm:build-library',json={'source_dir':DEST,'destination_dir':DEST})['job_id']
  until=time.monotonic()+120;observed=None
  from app.services.process_identity_native import observe,probe
  while observed is None:
   job=self.call('GET',f'/jobs/{jid}')
   assert job['status'] in {'queued','running'},('Compiler ended before cancellation',job['status'])
   for child in job.get('attempt',{}).get('child_processes',{}).get('children',{}).values():
    record=child.get('identity')
    if record is None or child['phase']!='running':continue
    namespace=record.get('namespace_init',{}).get('pid_namespace')
    for path in Path('/proc').iterdir():
     if not path.name.isdigit():continue
     try:
      if path.joinpath('comm').read_text().strip()!='cc1plus' or os.readlink(path/'ns/pid')!='pid:['+str(namespace)+']':continue
      observed=observe(int(path.name));break
     except (OSError,ValueError):continue
   assert time.monotonic()<until,'Actual C++ compiler not observed'
   if observed is None:time.sleep(.02)
  self.result['cancelled_compiler_birth']=observed;self.save()
  self.call('POST',f'/jobs/{jid}:cancel');self.settled(jid,'cancelled')
  assert probe(observed)=={'state':'exited','reason':'process_exited'}
  assert self.live(LIB)==original['library'] and self.remote(original['base']+'/files/'+LIB)==original['library']
  self.result['checks'].append('actual_cc1plus_cancel_scope_stopped_previous_library_preserved')
 def save(self):write(self.root/'result.json',self.result)
 def call(self,method,path,**kwargs):
  until=time.monotonic()+30
  while True:
   response=self.client.request(method,path,**kwargs)
   detail=response.json().get('detail') if response.status_code==409 else None
   if not isinstance(detail,dict) or detail.get('code')!='storage_quota_busy':break
   assert time.monotonic()<until,('S3 settlement remained busy',path)
   time.sleep(.2)
  assert response.status_code in (200,201,202),(path,response.status_code,response.text[:1500]);return response.json()
 def db(self,query,args):
  with sqlite3.connect((self.root/'test.sqlite').as_uri()+'?mode=ro',uri=True) as db:
   db.row_factory=sqlite3.Row;return [dict(r) for r in db.execute(query,args)]
 def case(self,label):
  case=self.call('POST',f'/workspaces/{self.workspace}/cases',json={'name':label});self.case_id=case['id'];self.prefix=f'/cases/{self.case_id}'
  self.s3prefix=self.db('SELECT s3_prefix FROM cases WHERE id=?',(self.case_id,))[0]['s3_prefix']
 def put(self,path,content):self.call('PUT',self.prefix+'/file',json={'path':path,'content':content})
 def remote(self,path):
  response=self.store.get_object(Bucket=self.bucket,Key=self.s3prefix.rstrip('/')+'/'+path)
  try:return response['Body'].read()
  finally:response['Body'].close()
 def live(self,path):
  response=self.client.get(self.prefix+'/raw',params={'path':path});response.raise_for_status();return response.content
 def settled(self,jid,expected='succeeded'):
  until=time.monotonic()+240
  while True:
   job=self.call('GET',f'/jobs/{jid}')
   if job['ended_at'] is not None and not job['completion_pending'] and job.get('attempt',{}).get('process_stop') is not None:break
   if time.monotonic()>until:raise TimeoutError('Actual FloTHERM job did not settle: '+str(jid))
   time.sleep(.1)
  write(self.root/f'job-{jid}.json',job);self.result['jobs'].append({'id':jid,'case_id':self.case_id,'status':job['status']});self.save()
  assert job['status']==expected,(jid,job['status'],job.get('log_backlog','')[-2000:])
  assert job['attempt']['process_stop']['verified'] and job['attempt']['process_stop']['scope_empty']
  for child in job['attempt']['child_processes'].get('children',{}).values():assert child['stop']['verified'] and child['stop']['scope_empty']
  return job
 def seed(self,source,install=False,value='1.25'):
  self.put(source+'/flothermHelper.C',SOURCE.replace('VALUE',value))
  self.put(source+'/Make/files','flothermHelper.C\n\nLIB = $('+('FOAM_USER_LIBBIN' if install else 'PWD')+')/libflothermHelper\n')
  self.put(source+'/Make/options','EXE_INC =\nLIB_LIBS =\n');self.put('unrelated.txt','original unrelated content\n')
 def build(self,source,destination,*,expected='succeeded',install=False):
  body={'source_dir':source,'destination_dir':destination};key='native-'+secrets.token_hex(8);headers={'Idempotency-Key':key}
  jid=self.call('POST',self.prefix+'/flotherm:build-library',json=body,headers=headers)['job_id'];job=self.settled(jid,expected)
  assert self.live('unrelated.txt')==b'original unrelated content\n'
  children=job['attempt']['child_processes']['children'];assert len(children)==1
  child=next(iter(children.values()));assert child['purpose']==('flotherm_library' if self.windows else 'flotherm_library_linux')
  publication=job['attempt']['outputs'].get('publication',{})
  if expected!='succeeded':
   assert publication.get('status')!='committed';return job,None
  assert publication['status']=='committed'
  base='.vibeflow/publications/'+publication['id']
  manifest=json.loads(self.remote(base+'/manifest.json'));write(self.root/f'publication-{jid}.json',manifest)
  native=manifest['descriptor']['receipt']['native']
  assert native['distro']==('vibeflow-linux' if self.windows else 'native-linux')
  if self.windows:assert native['uid']==65534
  assert native['libraries'] and set(native['steps'])=={'build','load'}
  assert all(s['returncode']==0 for s in native['steps'].values())
  assert bool(native['private_installations'])==install
  for path,record in native['libraries'].items():
   live=self.live(path);frozen=self.remote(base+'/files/'+path)
   assert live==frozen and live.startswith(b'\x7fELF') and sha(live)==record['sha256']
  assert b'FLOTHERM_LIBRARY_LOADED' in self.live(destination+'/flotherm-load.log')
  run=job['attempt']['workspace']['run_path']
  stage=json.loads(self.remote(run+'/.vibeflow/flotherm-library/stage.json'));assert stage['removed']
  if not self.windows:assert not Path(stage['path']).exists()
  else:
   import subprocess
   stopped=subprocess.run(['wsl.exe','-d','vibeflow-linux','-u','nobody','--exec','test','!','-e',stage['path']],capture_output=True,timeout=15)
   assert stopped.returncode==0
  assert self.call('POST',self.prefix+'/flotherm:build-library',json=body,headers=headers)['job_id']==jid
  return job,{'id':publication['id'],'base':base,'library':self.live(destination+'/libflothermHelper.so'),'manifest':manifest}
 def execute(self,publication,value):
  script='import ctypes,os;lib=ctypes.CDLL(os.path.abspath('+repr(LIB)+'));lib.vfFlothermPowerScale.restype=ctypes.c_double;value=lib.vfFlothermPowerScale();assert value=='+value+';print("ACTUAL_FLOTHERM_VALUE",value)'
  command='python3 -c '+shlex.quote(script)
  job=self.call('POST',self.prefix+'/jobs',json={'command':command,'runtime':'wsl' if self.windows else 'windows',
   'input_publications':[{'publication_id':publication['id'],'paths':[LIB],'require_current':True}]})
  done=self.settled(job['id']);assert 'ACTUAL_FLOTHERM_VALUE '+value in done['log_backlog']
 def run(self):
  try:
   with self.api:
    from app.services.process_identity_native import observe
    self.result['API_birth']=observe(self.api.proc.pid)
    with httpx.Client(base_url=self.api.base+'/api/v1',trust_env=False,timeout=120) as self.client:
     account={'email':'flotherm-'+secrets.token_hex(6)+'@example.com','password':secrets.token_hex(24)};write(self.root/'private/account.json',account)
     self.client.headers['Authorization']='Bearer '+self.call('POST','/auth/register',json={**account,'name':'Actual FloTHERM acceptance'})['token']
     self.workspace=self.call('GET','/workspaces')[0]['id'];self.case('Copy and rebuild FloTHERM helper')
     source='Ref/source %VALUE% & helper';self.seed(source)
     _,first=self.build(source,DEST);self.execute(first,'1.25')
     assert self.live(source+'/flothermHelper.C')==SOURCE.replace('VALUE','1.25').encode()
     if self.cancel_only:
      self.cancel_compiler(first);self.result['passed']=True;return
     self.put(DEST+'/flothermHelper.C',SOURCE.replace('VALUE','2.5'))
     _,second=self.build(DEST,DEST);self.execute(second,'2.5')
     assert first['library']!=second['library'] and self.remote(first['base']+'/files/'+LIB)==first['library']
     self.result['checks']+=['actual_copy_compile_load_function_call','source_edit_recompile_same_directory_and_execute_new_function','original_immutable_library_retained'];self.save()
     for mode,text,needle in [('compile_fail','invalid C++ source\n','error:'),('load_fail','extern "C" double missing_dependency();\nextern "C" double vfFlothermPowerScale() { return missing_dependency(); }\n','undefined symbol: missing_dependency')]:
      self.put(DEST+'/flothermHelper.C',text);job,_=self.build(DEST,DEST,expected='failed')
      assert self.live(LIB)==second['library'] and self.remote(first['base']+'/files/'+LIB)==first['library']
      assert needle in job['log_backlog'];self.result['checks'].append(mode+'_retains_previous_live_and_frozen_libraries');self.save()
     self.case('Private user library installation');self.seed('Ref/install',install=True)
     _,installed=self.build('Ref/install',DEST,install=True);self.execute(installed,'1.25')
     self.result['checks'].append('private_FOAM_USER_LIBBIN_install_publish_and_execute');self.result['passed']=True
  except BaseException as error:self.result['failure']={'type':type(error).__name__,'message':str(error)[:3000]};raise
  finally:
   self.result['API_exited']=self.api.proc is None or self.api.proc.poll() is not None
   if 'API_birth' in self.result:
    from app.services.process_identity_native import probe
    self.result['API_exit']=probe(self.result['API_birth'])
   self.save()

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('review',type=Path);p.add_argument('--windows',action='store_true');p.add_argument('--s3-endpoint',required=True);p.add_argument('--cancel-only',action='store_true');args=p.parse_args()
 review=read(args.review);assert review['classification']=='no_llm'
 for name,digest in review['sources'].items():
  assert name.startswith('platform/server/')
  source=Path(__file__).resolve().parents[2]/name if os.name=='nt' else Path(__file__).resolve().parent/name.removeprefix('platform/server/')
  assert sha(source.read_bytes())==digest,name
 assert args.windows==(os.name=='nt');assert not args.cancel_only or not args.windows;Run(args.windows,args.s3_endpoint,args.cancel_only).run()
