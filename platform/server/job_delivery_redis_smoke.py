"""Real arq and a disposable WSL Redis, including complete broker queue loss.

Requires extracted Ubuntu redis-server/tools and their shared libraries at
--redis-root. No installed Redis service or user's queue/database is used.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from testsupport import SERVER
from job_delivery_recovery_smoke import new_api,payload,kill_owner
from job_owner_startup_smoke import shared
from app.services.owned_process import OwnedProcess,wsl_path
from workflow_migration_smoke import alembic

WORKER = '''import os
os.environ.update(OVERRIDES)
import asyncio
import json
from pathlib import Path
from app.services.distributed_worker import create_worker
from app.services import distributed_queue
async def main():
 worker=create_worker(redis_settings=distributed_queue.redis_settings,
  queue_name=os.environ['DELIVERY_FIXTURE_QUEUE'],burst=True,handle_signals=False,poll_delay=.1)
 try:
  await worker.async_run()
  Path(__file__).with_name('worker-finished.json').write_text(json.dumps({'complete':worker.jobs_complete,'failed':worker.jobs_failed}))
  assert worker.jobs_complete==1 and worker.jobs_failed==0,(worker.jobs_complete,worker.jobs_failed)
 finally:
  await worker.close()
asyncio.run(main())
'''


class Broker:
    def __init__(self,root,report):
        self.root=root.resolve();self.report=report;self.process=None;self.drain=None;self.stops=[];self.births=[]
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]

    async def start(self):
        base=wsl_path(self.root)
        self.process=OwnedProcess()
        argv=['wsl.exe','-d','vibeflow-linux','--exec','env','LD_LIBRARY_PATH='+base+'/usr/lib/x86_64-linux-gnu',
            base+'/usr/bin/redis-server','--bind','127.0.0.1','--port',str(self.port),'--protected-mode','yes',
            '--save','','--appendonly','no','--daemonize','no','--dir',wsl_path(self.report)]
        await self.process.prepare(argv,cwd=self.report,env=dict(os.environ),runtime='wsl')
        await self.process.release()
        async def drain():
            with (self.report/('redis-'+str(len(self.births))+'.log')).open('wb') as stream:
                while data:=await self.process.proc.stdout.read(8192):stream.write(data)
        self.drain=asyncio.create_task(drain())
        client=Redis(host='127.0.0.1',port=self.port,socket_connect_timeout=1,socket_timeout=1)
        try:
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                try:
                    info=await client.info('server')
                    self.births.append(info['run_id']);return
                except (OSError,ConnectionError,__import__('redis').exceptions.RedisError):await asyncio.sleep(.1)
            raise AssertionError('Disposable Redis did not listen')
        finally:await client.aclose()

    async def stop(self):
        if self.process is not None:
            proof=await self.process.stop(reason='cancelled',grace_ms=2000)
            self.stops.append(proof);assert proof['verified'],proof
            if self.drain is not None:await self.drain
            self.process=None

    async def restart(self):
        previous=self.births[-1]
        await self.stop();await self.start()
        assert self.births[-1]!=previous


def verify(broker,loop,url=None):
    queue='vf-isolated-delivery-'+uuid.uuid4().hex
    settings={'VIBEFLOW_REDIS_HOST':'127.0.0.1','VIBEFLOW_REDIS_PORT':str(broker.port),
        'VIBEFLOW_DISTRIBUTED_JOBS':'true','DELIVERY_FIXTURE_QUEUE':queue}
    owner=new_api('delivery-redis-owner',url,settings,paused=True)
    checks=[]
    with owner:
        owner.create_case('Redis delivery survives broker loss')
        command,marker,finish=payload(owner.root,'redis');finish.touch()
        submitted=owner.call('POST',owner.prefix+'/jobs',{'command':command})
        identifier=submitted['id'];before=owner.call('GET',f'/fixture/rows/{owner.case_id}')
        assert before[0]['phase']=='queued' and not before[0]['claim'] and not marker.exists()
        async def queued():
            client=Redis(host='127.0.0.1',port=broker.port)
            try:return await client.zcard(queue)
            finally:await client.aclose()
        assert asyncio.run_coroutine_threadsafe(queued(),loop).result(timeout=10)==1
        checks.append('committed_redis_intent_and_real_arq_message_without_local_launch')
        kill_owner(owner,owner.call('GET','/fixture/birth'))
        asyncio.run_coroutine_threadsafe(broker.restart(),loop).result(timeout=40)
        assert asyncio.run_coroutine_threadsafe(queued(),loop).result(timeout=10)==0
        checks.append('real_API_death_and_broker_restart_erase_transport_only')
        peer=new_api('delivery-redis-peer',url,{**shared(owner),**settings})
        with peer:
            peer.token=owner.token
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if asyncio.run_coroutine_threadsafe(queued(),loop).result(timeout=10)==1:break
                time.sleep(.1)
            else:raise AssertionError('SQL delivery intent did not restore Redis queue')
            for _ in range(3):peer.call('POST','/fixture/recover')
            assert asyncio.run_coroutine_threadsafe(queued(),loop).result(timeout=10)==1
            checks.append('new_API_rebuilds_one_deterministic_transport_message_from_SQL')
            script=peer.root/'delivery_worker.py'
            values={**shared(owner),**settings}
            script.write_text(WORKER.replace('OVERRIDES',repr(values)),encoding='utf8')
            env=dict(peer.env,PYTHONPATH=str(SERVER)+os.pathsep+peer.env.get('PYTHONPATH',''))
            with (peer.root/'worker.log').open('w',encoding='utf8') as log:
                worker=subprocess.Popen([sys.executable,'-X','utf8','-B',str(script)],cwd=SERVER,env=env,
                    stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                try:
                    final=peer.wait_job(identifier,timeout=60)
                    code=worker.wait(timeout=20)
                    (peer.root/'worker-exit.json').write_text(json.dumps({'exit_code':code}),encoding='utf8')
                    assert code==0,{'exit_code':code,'log':(peer.root/'worker.log').read_text(encoding='utf8')}
                finally:
                    if worker.poll() is None:worker.terminate();worker.wait(timeout=15)
            assert final['status']=='succeeded' and final['attempt']['process_stop']['verified']
            assert marker.read_text().splitlines()==['started']
            after=peer.call('GET',f'/fixture/rows/{owner.case_id}')
            assert len(after)==1 and after[0]['attempt_id']==before[0]['attempt_id']
            checks.append('real_arq_worker_executes_original_attempt_once_and_preserves_cleanup')
            peer.call('POST','/fixture/recover')
            assert asyncio.run_coroutine_threadsafe(queued(),loop).result(timeout=10)==0
            checks.append('terminal_attempt_is_not_reenqueued_after_transport_result_expires')
            return {'passed':True,'count':len(checks),'checks':checks,'before':before,'after':after,
                'final':final,'worker_exit_code':code,'evidence':str(peer.root),'queue':queue,
                'developer_redis_used':False,'developer_database_touched':False}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--redis-root',type=Path,default=SERVER.parents[1]/'tmp/delivery-redis-packages/root');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='delivery-redis-evidence-',dir=SERVER.parents[1]/'tmp'))
    hashes={p.relative_to(SERVER).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (SERVER/'app').rglob('*.py')}
    (report/'source.json').write_text(json.dumps(hashes,indent=2),encoding='utf8')
    broker=Broker(args.redis_root,report)
    try:
        await broker.start()
        loop=asyncio.get_running_loop()
        results={'sqlite':await asyncio.to_thread(verify,broker,loop)}
        if args.postgres:
            from app.config import Settings
            import asyncpg
            url=make_url(Settings(_env_file=SERVER/'.env').database_url)
            assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
            name='tmp_vf_redisdelivery_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_redisdelivery_[0-9a-f]{32}',name)
            admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
            created=False
            try:
                await admin.execute('CREATE DATABASE "'+name+'"');created=True
                target=url.set(database=name).render_as_string(hide_password=False)
                await asyncio.to_thread(alembic,target,report,'upgrade','head')
                results['postgresql']=await asyncio.to_thread(verify,broker,loop,target)
            finally:
                if created:
                    assert await admin.fetchval('SELECT current_database()')=='postgres'
                    await admin.execute('DROP DATABASE "'+name+'"')
                await admin.close()
            results['postgresql']['temporary_database_removed']=True
    finally:
        await broker.stop()
        (report/'broker-cleanup.json').write_text(json.dumps({'births':broker.births,'stops':broker.stops},indent=2),encoding='utf8')
    assert hashes=={p.relative_to(SERVER).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (SERVER/'app').rglob('*.py')}
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('REDIS_DELIVERY_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items()}}))


if __name__=='__main__':asyncio.run(main())
