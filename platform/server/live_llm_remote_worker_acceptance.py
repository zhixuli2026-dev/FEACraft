"""Windows admission -> actual Linux Redis worker -> shared PG/S3 -> real DeepSeek."""
import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

import asyncpg
from sqlalchemy.engine import make_url

from app.config import Settings
from app.services.owned_process import probe_native, wsl_path
from live_llm_acceptance import FIXTURE, source
from live_llm_sandbox_recovery_acceptance import model_read
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER
from workflow_batch_redis_support import Broker
from workflow_migration_smoke import alembic

ROOT = SERVER.parents[1]
PYTHON = '/opt/vibeflow-worker-venv/bin/python'
WORKER = '''
import asyncio,json,os,sys
from pathlib import Path
settings=json.loads(Path(sys.argv[1]).read_text())
os.environ.update(settings)
sys.path.insert(0,SERVER)
from app.services.distributed_worker import create_worker
from app.services.distributed_queue import on_startup
from app.services import process_identity_native as identity
async def ready(ctx):
 await on_startup(ctx)
 assert os.getuid()==65534
 assert (await ctx['redis'].info('server'))['run_id']==REDIS
 ready_file=Path(READY)
 staged=ready_file.with_suffix('.staged')
 staged.write_text(json.dumps({'identity':identity.observe(os.getpid()),'uid':os.getuid()}))
 staged.replace(ready_file)
async def main():
 worker=create_worker(max_jobs=1,poll_delay=.1,handle_signals=False,on_startup=ready)
 task=asyncio.create_task(worker.async_run())
 try:
  while not Path(STOP).exists() and not task.done():await asyncio.sleep(.1)
  if task.done():await task
 finally:
  await worker.close(grace_seconds=0)
  await asyncio.gather(task,return_exceptions=True)
asyncio.run(main())
'''


def linux_observation():
    code = "import sys,json,socket,struct;from pathlib import Path;sys.path.insert(0,"+repr(wsl_path(SERVER))+ ");from app.services import process_identity_native as p;g=[socket.inet_ntoa(struct.pack('<I',int(x.split()[2],16))) for x in Path('/proc/net/route').read_text().splitlines()[1:] if x.split()[1]=='00000000'][0];s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect((g,5432));print(json.dumps({'host_id':p.host_id(),'gateway':g,'source_ip':s.getsockname()[0]}));s.close()"
    process = subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-c',code],
        capture_output=True,text=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    assert process.returncode==0, 'Linux host observation failed'
    return json.loads(process.stdout)


class HbaAccess:
    """Temporary SCRAM rule for one disposable DB, one role and one actual WSL address."""
    def __init__(self, selected, observed, evidence):
        import ipaddress
        assert re.fullmatch('tmp_vf_remoteworker_[0-9a-f]{32}',selected.database)
        assert re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',selected.username)
        address=str(ipaddress.IPv4Address(observed['source_ip']))
        assert ipaddress.ip_address(address).is_private
        self.root=Path('C:/Program Files/PostgreSQL/16/data')
        self.path=self.root/'pg_hba.conf'
        self.block=('\n# isolated remote worker '+selected.database+'\nhost '+selected.database+' '+selected.username+' '+address+'/32 scram-sha-256\n').encode()
        self.evidence=evidence
        self.original=self.path.read_bytes()

    def reload(self):
        result=subprocess.run(['C:/Program Files/PostgreSQL/16/bin/pg_ctl.exe','reload','-D',str(self.root)],
            capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode==0,'PostgreSQL configuration reload failed'

    def __enter__(self):
        assert self.path.read_bytes()==self.original and self.block not in self.original
        self.path.write_bytes(self.original+self.block)
        try:self.reload()
        except BaseException:
            assert self.path.read_bytes()==self.original+self.block
            self.path.write_bytes(self.original)
            self.reload()
            raise
        return self

    def __exit__(self,*args):
        current=self.path.read_bytes()
        assert current.count(self.block)==1,'Temporary PostgreSQL access rule changed externally'
        self.path.write_bytes(current.replace(self.block,b'',1))
        self.reload()
        (self.evidence/'postgres-access.json').write_text(json.dumps({'temporary_rule_removed':True,
            'original_config_restored':self.path.read_bytes()==self.original,'authentication':'scram-sha-256',
            'scope':'one disposable database, one role, one actual WSL address'}),'utf8')


class LinuxWorker:
    def __init__(self, api, broker, store, selected, observed):
        self.api, self.broker, self.store = api, broker, store
        self.selected, self.observed = selected, observed
        self.process = None
        self.receipts = []
        self.index = 0

    def fixture_code(self, root):
        return ''

    def start(self):
        root = self.api.root/('linux-worker-'+str(self.index))
        root.mkdir();self.index+=1
        self.root = root
        self.stopfile = root/'stop'
        ready = root/'ready.json'
        cache = '/tmp/vibeflow-remote-worker-'+uuid.uuid4().hex
        settings = {k:v for k,v in self.api.env.items() if k.startswith('VIBEFLOW_')}
        settings.update(self.broker.settings())
        settings.update(self.store.settings())
        settings.update(VIBEFLOW_DATABASE_URL=self.selected.set(host=self.observed['gateway']).render_as_string(hide_password=False),
            VIBEFLOW_STORAGE_FS_ROOT=cache+'/files', VIBEFLOW_INDEX_ROOT=cache+'/index',
            VIBEFLOW_AUDIT_DIR=cache+'/audit', VIBEFLOW_RUNTIME_CONFIG=cache+'/runtime.json',
            VIBEFLOW_CODEX_STATE_ROOT=cache+'/cli-state', VIBEFLOW_SKILLS_STORAGE_ROOT=cache+'/skills',
            VIBEFLOW_WORKER_HOST_QUEUE='true', VIBEFLOW_JOB_EXECUTION_HOSTS='{}')
        (root/'settings.json').write_text(json.dumps(settings),'utf8')
        script=WORKER.replace('\nasync def main():','\n'+self.fixture_code(root)+'\nasync def main():')
        code = 'SERVER='+repr(wsl_path(SERVER))+'\nREADY='+repr(wsl_path(ready))+'\nSTOP='+repr(wsl_path(self.stopfile))+'\nREDIS='+repr(self.broker.births[-1]['run_id'])+'\n'+script
        (root/'worker.py').write_text(code,'utf8')
        self.log = (root/'worker.log').open('w',encoding='utf8')
        self.process = subprocess.Popen(['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',PYTHON,
            '-B',wsl_path(root/'worker.py'),wsl_path(root/'settings.json')], stdout=self.log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic()+45
        while not ready.exists():
            if self.process.poll() is not None:
                raise RuntimeError('Linux worker failed; inspect '+str(root/'worker.log'))
            if time.monotonic()>=deadline:
                raise TimeoutError('Linux worker startup did not finish')
            time.sleep(.1)
        self.birth = json.loads(ready.read_text())['identity']
        assert self.birth['host_id']==self.observed['host_id'] and self.birth['uid']==65534

    async def stop(self):
        if self.process is None:
            return
        self.stopfile.touch()
        await asyncio.to_thread(self.process.wait, 45)
        self.log.close()
        if self.process.returncode!=0:
            raise RuntimeError('Linux worker did not close cleanly; inspect '+str(self.root/'worker.log'))
        observation = await probe_native('vibeflow-linux',self.birth)
        assert observation['state']=='exited'
        self.receipts.append({'birth':self.birth,'observation':observation,'exit_code':0,'root':str(self.root)})
        self.process = None


async def verify(configuration, selected, admin, observed, *, worker_factory=LinuxWorker, after_publish=None, environment_overrides=None, store_volume_max=3):
    api = IsolatedServer('remote-worker',app='remote_fixture:app',database_url=selected.render_as_string(hide_password=False))
    broker = Broker(api.root)
    store = ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed',volume_max=store_volume_max)
    worker = None
    result = {'passed':False,'mock_llm_calls':0,'checks':[],'jobs':[],'linux_host_id':observed['host_id']}
    def save():(api.root/'remote-result.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
    async def call(method,path,**kwargs):
        deadline=time.monotonic()+45
        while True:
            try:return await asyncio.to_thread(api.call,method,path,**kwargs)
            except AssertionError as error:
                if not any(s in str(error) for s in ('storage_quota_busy','validation_busy')) or time.monotonic()>=deadline:raise
                await asyncio.sleep(.2)
    async def wait(identifier, predicate):
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            job=await call('GET','/jobs/'+str(identifier))
            if predicate(job):return job
            if job['status']=='failed':raise RuntimeError('Remote job failed; inspect API and worker logs')
            await asyncio.sleep(.3)
        raise TimeoutError('Remote job did not reach required state')
    async def submit(command,kind=None):
        body={'command':'python3 -c '+shlex.quote(command),'runtime':'windows','timeout_seconds':120}
        if kind:body['type']=kind
        return await call('POST',api.prefix+'/jobs',body=body)
    async def terminal(identifier,status,label):
        job=await wait(identifier,lambda j:j['status']==status and bool(j['attempt'].get('process_stop')))
        assert job['attempt']['process_stop']['verified']
        process=job['attempt']['process_identity']
        assert process['host']['platform']=='linux' and process['host']['host_id']==observed['host_id']
        assert process['host']['uid']==65534
        assert (await probe_native('vibeflow-linux',process['host']))['state']=='exited'
        result['jobs'].append({'id':identifier,'status':status,'attempt':job['attempt']})
        result['checks'].append(label);save();print('REMOTE_WORKER_PASS '+label,flush=True)
        return job
    try:
        await broker.start()
        with store:
            store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
            env={**broker.settings(),**store.settings(), 'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({'windows':{'host_id':observed['host_id'],'platform':'linux'}})}
            if environment_overrides:env.update(environment_overrides)
            (api.root/'remote_fixture.py').write_text('import os\nos.environ.update('+repr(env)+')\n'+FIXTURE,'utf8')
            with api:
                await asyncio.to_thread(api.create_case,'Actual remote Linux worker')
                await call('PUT',api.prefix+'/file',body={'path':'Ref/input.txt','content':'REMOTE_SEALED_INPUT'})
                first=await submit("from pathlib import Path\nimport secrets,os\nassert os.getuid()==65534\nassert Path('Ref/input.txt').read_text()=='REMOTE_SEALED_INPUT'\nPath('Ref/proof.txt').write_text('RETIREMENT_'+secrets.token_hex(16)+'\\n')\n",'convert')
                await asyncio.sleep(2)
                assert (await call('GET','/jobs/'+str(first['id'])))['status']=='queued'
                result['checks'].append('remote target remains queued until its actual host worker starts')
                worker=worker_factory(api,broker,store,selected,observed)
                await asyncio.to_thread(worker.start)
                await terminal(first['id'],'succeeded','Linux host hydrates sealed S3 inputs and publishes real outputs')
                if after_publish is not None:
                    await after_publish(api,worker,selected)
                proof=(await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)).decode().strip()
                result['real_model_calls']=await asyncio.to_thread(model_read,api,'','',proof,configuration,already_restored=True,path='Ref/proof.txt')
                job=await submit("import time\nprint('REMOTE_RUNNING',flush=True)\ntime.sleep(90)\n")
                await wait(job['id'],lambda j:j['status']=='running' and bool(j['attempt'].get('process_identity')))
                await call('POST','/jobs/'+str(job['id'])+':cancel')
                await terminal(job['id'],'cancelled','Windows API cancels the actual Linux worker command')
                job=await submit("import time\nprint('REMOTE_RUNNING',flush=True)\ntime.sleep(90)\n")
                await wait(job['id'],lambda j:j['status']=='running' and bool(j['attempt'].get('process_identity')))
                await worker.stop()
                await terminal(job['id'],'interrupted','remote worker shutdown persists interruption before process exit')
                await asyncio.to_thread(worker.start)
                job=await submit("print('REMOTE_RESTARTED',flush=True)\n")
                await terminal(job['id'],'succeeded','restarted Linux worker accepts a fresh durable request')
                await worker.stop()
                result['workers']=worker.receipts
                result['passed']=True
    except BaseException as error:
        result['failure_type']=type(error).__name__
        raise
    finally:
        try:
            if worker is not None and worker.process is not None:await worker.stop()
        finally:
            await broker.stop()
            result['redis_stops']=broker.stops
            result['store_stopped']=store.proc is None or store.proc.poll() is not None
            save()
    return api.root


async def main(*, entry='platform/server/live_llm_remote_worker_acceptance.py', verifier=verify):
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args()
    review=json.loads(args.review.read_text('utf8'))
    assert review['entry']==entry
    for name,digest in review['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    configuration=await source(args.source_profile_id)
    observed=await asyncio.to_thread(linux_observation)
    selected=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert selected.drivername=='postgresql+asyncpg' and selected.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_remoteworker_'+uuid.uuid4().hex
    admin=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,password=selected.password,database='postgres',timeout=10)
    evidence=ROOT/'tmp'/('remote-worker-postgres-'+uuid.uuid4().hex);evidence.mkdir()
    report={'passed':False,'database':name,'developer_database_modified':False}
    created=False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        selected=selected.set(database=name)
        await asyncio.to_thread(alembic,selected.render_as_string(hide_password=False),evidence,'upgrade','head')
        with HbaAccess(selected,observed,evidence):
            report['evidence']=str(await verifier(configuration,selected,admin,observed));report['passed']=True
    finally:
        try:
            if created:
                await admin.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:
            await admin.close();(evidence/'result.json').write_text(json.dumps(report,indent=2),'utf8')


if __name__=='__main__':asyncio.run(main())
