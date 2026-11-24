"""Actual isolated Redis and real arq worker lifecycle helpers; no inference."""
import asyncio,json,os,socket,subprocess,sys,time
from pathlib import Path

from app.services.owned_process import OwnedProcess,wsl_path
from app.services import process_identity_native as identity
from testsupport import SERVER


class Broker:
    def __init__(self,root):
        self.root=root;self.owned=None;self.drain=None;self.births=[];self.stops=[]
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]

    def settings(self):
        return {'VIBEFLOW_DISTRIBUTED_JOBS':'true','VIBEFLOW_REDIS_HOST':'127.0.0.1',
            'VIBEFLOW_REDIS_PORT':str(self.port),'VIBEFLOW_REDIS_PASSWORD':''}

    async def start(self):
        from redis.asyncio import Redis
        package=SERVER.parents[1]/'tmp/delivery-redis-packages/root'
        self.owned=OwnedProcess();base=wsl_path(package)
        version=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--exec','env',
            'LD_LIBRARY_PATH='+base+'/usr/lib/x86_64-linux-gnu',base+'/usr/bin/redis-server','--version'],
            capture_output=True,text=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
        assert version.returncode==0 and version.stdout.startswith('Redis server v='),'Actual WSL Redis runtime is unavailable'
        self.binary_version=version.stdout.strip()
        argv=['wsl.exe','-d','vibeflow-linux','--exec','env','LD_LIBRARY_PATH='+base+'/usr/lib/x86_64-linux-gnu',
            base+'/usr/bin/redis-server','--bind','127.0.0.1','--port',str(self.port),'--protected-mode','yes',
            '--save','','--appendonly','no','--daemonize','no','--maxmemory','64mb','--maxmemory-policy','noeviction',
            '--dir',wsl_path(self.root),'--pidfile',wsl_path(self.root/'redis.pid')]
        await self.owned.prepare(argv,cwd=self.root,env=dict(os.environ),runtime='wsl')
        await self.owned.verify_registration();await self.owned.release()
        async def drain():
            with (self.root/('redis-'+str(len(self.births))+'.log')).open('wb') as stream:
                while data:=await self.owned.proc.stdout.read(8192):stream.write(data)
        self.drain=asyncio.create_task(drain())
        client=Redis(host='127.0.0.1',port=self.port,socket_connect_timeout=1,socket_timeout=1)
        try:
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if self.owned.proc.returncode is not None:raise RuntimeError('Owned isolated Redis exited; inspect its log')
                try:
                    info=await client.info('server');pidfile=self.root/'redis.pid'
                    if not pidfile.exists():await asyncio.sleep(.1);continue
                    assert int(pidfile.read_text().strip())==info['process_id']
                    self.births.append({'run_id':info['run_id'],'process_id':info['process_id'],'version':info['redis_version'],'port':self.port})
                    return
                except (OSError,__import__('redis').exceptions.RedisError):await asyncio.sleep(.1)
            raise TimeoutError('Owned Redis did not expose its matching PID file')
        finally:await client.aclose()

    async def stop(self):
        if self.owned is None:return
        try:
            proof=await self.owned.stop(reason='cancelled',grace_ms=2000);self.stops.append(proof)
            (self.root/'redis-stops.json').write_text(json.dumps(self.stops,indent=2),encoding='utf8')
            assert proof['verified']
            if self.drain is not None:await self.drain
        finally:self.owned.close();self.owned=None

    async def restart(self):
        old=self.births[-1]['run_id'];await self.stop();await self.start()
        assert self.births[-1]['run_id']!=old


WORKER=r'''
import asyncio,json,os
from pathlib import Path
from app.services.distributed_worker import create_worker
from app.services.distributed_queue import on_startup
from app.services import process_identity_native as identity
async def ready(ctx):
 await on_startup(ctx)
 assert (await ctx['redis'].info('server'))['run_id']==EXPECTED_REDIS
 Path(READY).write_text(json.dumps({'identity':identity.observe(os.getpid()),'redis_run_id':EXPECTED_REDIS}),encoding='utf8')
async def main():
 worker=create_worker(max_jobs=1,poll_delay=.1,handle_signals=False,on_startup=ready)
 task=asyncio.create_task(worker.async_run())
 try:
  while not Path(STOP).exists() and not task.done():await asyncio.sleep(.1)
  if task.done():await task
 finally:
  await worker.close();await asyncio.gather(task,return_exceptions=True)
asyncio.run(main())
'''


class Workers:
    def __init__(self,api,broker,store):
        self.api=api;self.broker=broker;self.store=store;self.running=[];self.receipts=[];self.sequence=0

    def start(self):
        import ast
        for _ in range(2):
            index=self.sequence;self.sequence+=1
            root=self.api.root/('batch-worker-'+str(index));root.mkdir()
            script=root/'worker.py';stop=root/'stop';ready=root/'ready.json'
            settings={k:v for k,v in self.api.env.items() if k.startswith('VIBEFLOW_')}
            settings.update(self.broker.settings())
            if self.store:
                settings.update(self.store.settings());settings['VIBEFLOW_STORAGE_FS_ROOT']=str(root/'s3-cache')
            code='import os\nos.environ.update('+repr(settings)+')\n'
            code+='STOP='+repr(str(stop))+'\nREADY='+repr(str(ready))+'\nEXPECTED_REDIS='+repr(self.broker.births[-1]['run_id'])+'\n'+WORKER
            ast.parse(code);script.write_text(code,encoding='utf8')
            log=(root/'worker.log').open('w',encoding='utf8')
            env=dict(self.api.env,PYTHONPATH=str(SERVER)+os.pathsep+self.api.env.get('PYTHONPATH',''))
            process=subprocess.Popen([sys.executable,'-X','utf8','-B',str(script)],cwd=SERVER,env=env,
                stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            record={'process':process,'stop':stop,'ready':ready,'log':log,'root':root};self.running.append(record)
            deadline=time.monotonic()+30
            while not ready.exists():
                if process.poll() is not None:raise RuntimeError('Actual arq worker exited; inspect '+str(root/'worker.log'))
                if time.monotonic()>deadline:raise TimeoutError('Actual arq worker did not connect to owned Redis')
                time.sleep(.1)
            record['identity']=json.loads(ready.read_text())['identity'];assert identity.probe(record['identity'])['state']=='match'

    def identities(self):return [r['identity'] for r in self.running]

    def stop(self):
        errors=[]
        for record in self.running:record['stop'].touch()
        for record in self.running:
            process=record['process']
            try:
                process.wait(timeout=30)
                assert process.returncode==0,'Actual arq worker failed; inspect '+str(record['root']/'worker.log')
                expected=record.get('identity')
                state=identity.probe(expected) if expected else {'state':'unverified'}
                assert state['state']=='exited'
                self.receipts.append({'root':str(record['root']),'identity':expected,'exit_code':process.returncode,'actual_probe':state})
            except Exception as error:errors.append(type(error).__name__+': inspect '+str(record['root']/'worker.log'))
            finally:record['log'].close()
        self.running=[r for r in self.running if r['process'].poll() is None]
        if errors:raise RuntimeError('Actual worker shutdown not verified: '+'; '.join(errors))
