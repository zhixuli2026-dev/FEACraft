"""Cross-API cancellation at real arq delivery boundaries, with a private Redis."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from testsupport import SERVER
from job_delivery_redis_smoke import Broker
from job_delivery_recovery_smoke import new_api,payload
from job_owner_startup_smoke import shared
from workflow_migration_smoke import alembic

WORKER='''import os
os.environ.update(OVERRIDES)
import asyncio,json
from pathlib import Path
from app.services import job_execution,attempt_workspace,distributed_queue
from app.services.distributed_worker import create_worker
root=Path(__file__).parent
mode=MODE
async def hold():
 (root/'ready').touch()
 for _ in range(3000):
  if (root/'continue').exists():return
  await asyncio.sleep(.02)
 raise TimeoutError('Cancellation fixture barrier')
workspace=attempt_workspace.runtime_workspace
async def runtime_workspace(owner,*args,**kwargs):
 if mode=='claimed' and not kwargs.get('complete'):await hold()
 return await workspace(owner,*args,**kwargs)
attempt_workspace.runtime_workspace=runtime_workspace
phase=job_execution.phase
async def changing(owner,target,**kwargs):
 result=await phase(owner,target,**kwargs)
 if mode=='registered' and target=='running':await hold()
 return result
job_execution.phase=changing
finish=job_execution.finish
async def finishing(owner,*args,**kwargs):
 if mode=='finalizing':await hold()
 return await finish(owner,*args,**kwargs)
job_execution.finish=finishing
async def main():
 worker=create_worker(redis_settings=distributed_queue.redis_settings,
  queue_name=os.environ['DELIVERY_FIXTURE_QUEUE'],burst=True,handle_signals=False,poll_delay=.1)
 try:
  await worker.async_run()
  (root/'worker-finished.json').write_text(json.dumps({'complete':worker.jobs_complete,'failed':worker.jobs_failed}))
  assert worker.jobs_complete+worker.jobs_failed==1
 finally:await worker.close()
asyncio.run(main())
'''

DROP_WAKEUP='''
original_cancel=distributed_queue.cancel
async def cancelling(job_id):
 if (ROOT/'drop-wakeup').exists():
  (ROOT/'wakeup-dropped').touch()
  raise ConnectionError('Fixture lost cancellation broadcast')
 return await original_cancel(job_id)
distributed_queue.cancel=cancelling
'''


def wait_file(path,timeout=40):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if path.exists():return
        time.sleep(.05)
    raise TimeoutError('Cancellation fixture marker: '+str(path))


def verify(broker,url=None,runtime='windows'):
    queue='vf-isolated-cancel-'+uuid.uuid4().hex
    settings={'VIBEFLOW_REDIS_HOST':'127.0.0.1','VIBEFLOW_REDIS_PORT':str(broker.port),
        'VIBEFLOW_DISTRIBUTED_JOBS':'true','DELIVERY_FIXTURE_QUEUE':queue,
        'VIBEFLOW_JOB_HEARTBEAT_SECONDS':'2'}
    owner=new_api('cancel-redis-owner',url,settings,paused=True)
    checks=[];receipts=[]
    with owner:
        peer=new_api('cancel-redis-peer',url,{**shared(owner),**settings},paused=True)
        fixture=peer.root/'delivery_fixture.py'
        fixture.write_text(fixture.read_text(encoding='utf8')+DROP_WAKEUP,encoding='utf8')
        with peer:
            for mode in ('queued','claimed','registered','running','running_lost','finalizing','succeeded'):
                owner.create_case('Redis cancellation '+mode);peer.token=owner.token
                command,marker,finish=payload(owner.root,mode,runtime=runtime)
                if mode in {'finalizing','succeeded'}:finish.touch()
                row=owner.call('POST',owner.prefix+'/jobs',{'command':command,'runtime':runtime})
                job_id=row['id'];before=owner.call('GET',f'/jobs/{job_id}')
                assert before['status']=='queued' and not marker.exists()
                worker_root=owner.root/('worker-'+mode);worker_root.mkdir()
                script=worker_root/'worker.py'
                script.write_text(WORKER.replace('OVERRIDES',repr({**shared(owner),**settings})).replace('MODE',repr(mode)),encoding='utf8')
                dropped=peer.root/'drop-wakeup'
                if mode=='running_lost':dropped.touch()
                else:dropped.unlink(missing_ok=True)
                replies=[]
                if mode=='queued':
                    replies.append(peer.call('POST',f'/jobs/{job_id}:cancel'))
                    replies.append(peer.call('POST',f'/jobs/{job_id}:cancel'))
                env=dict(owner.env,PYTHONPATH=str(SERVER)+os.pathsep+owner.env.get('PYTHONPATH',''))
                with (worker_root/'worker.log').open('w',encoding='utf8') as log:
                    worker=subprocess.Popen([sys.executable,'-X','utf8','-B',str(script)],cwd=SERVER,env=env,
                        stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    try:
                        if mode in {'claimed','registered','finalizing'}:
                            wait_file(worker_root/'ready',timeout=40)
                            observed=owner.call('GET',f'/jobs/{job_id}')
                            assert observed['attempt']['phase']=={'claimed':'claimed','registered':'running','finalizing':'finalizing'}[mode],observed['attempt']['phase']
                            assert worker.poll() is None and not (worker_root/'worker-finished.json').exists()
                        elif mode in {'running','running_lost'}:wait_file(marker,timeout=40)
                        elif mode=='succeeded':owner.wait_job(job_id,timeout=60)
                        if mode!='queued':
                            replies.append(peer.call('POST',f'/jobs/{job_id}:cancel'))
                            replies.append(peer.call('POST',f'/jobs/{job_id}:cancel'))
                        (worker_root/'continue').touch()
                        code=worker.wait(timeout=60)
                        assert code==0,{'mode':mode,'exit_code':code,'log':str(worker_root/'worker.log')}
                    finally:
                        (worker_root/'continue').touch();finish.touch()
                        if worker.poll() is None:worker.terminate();worker.wait(timeout=15)
                final=owner.call('GET',f'/jobs/{job_id}')
                expected='succeeded' if mode=='succeeded' else 'cancelled'
                assert final['status']==expected and final['attempt']['phase']==expected,final
                assert not final['completion_pending'] and final['ended_at'] is not None
                assert final['attempt']['id']==before['attempt']['id']
                if mode=='succeeded':
                    assert all(not reply['ok'] for reply in replies)
                else:assert replies[0]['ok'] and not replies[1]['ok'],replies
                if mode in {'queued','claimed','registered'}:assert not marker.exists()
                else:assert marker.read_text().splitlines()==['started']
                if mode in {'queued','claimed'}:
                    assert final['attempt']['process_identity'] is None and final['attempt']['process_stop'] is None
                else:
                    assert final['attempt']['process_stop']['verified']
                    observed=owner.call('GET',f'/jobs/{job_id}/process')
                    assert observed['state']=='exited',observed
                if mode=='running_lost':assert (peer.root/'wakeup-dropped').exists()
                after=owner.call('GET',f'/fixture/rows/{owner.case_id}')
                assert len(after)==1 and after[0]['attempt_id']==before['attempt']['id']
                assert peer.call('GET',f'/jobs/{job_id}')['attempt']==final['attempt']
                check=mode+'_cross_API_cancel_keeps_original_attempt_and_verified_terminal'
                checks.append(check);receipts.append({'mode':mode,'final':final,'replies':replies,
                    'transport':json.loads((worker_root/'worker-finished.json').read_text()),'worker_exit':code})
                (owner.root/'cancel-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
                print('PASS '+check,flush=True)
    return {'passed':True,'count':len(checks),'checks':checks,'evidence':str(owner.root),
        'peer_evidence':str(peer.root),'runtime':runtime,'developer_redis_used':False,'developer_database_touched':False}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--runtime',choices=('windows','wsl'),default='windows')
    parser.add_argument('--redis-root',type=Path,default=SERVER.parents[1]/'tmp/delivery-redis-packages/root')
    args=parser.parse_args();report=Path(tempfile.mkdtemp(prefix='cancel-redis-evidence-',dir=SERVER.parents[1]/'tmp'))
    broker=Broker(args.redis_root,report)
    try:
        await broker.start()
        results={'sqlite':await asyncio.to_thread(verify,broker,runtime=args.runtime)}
        if args.postgres:
            import asyncpg
            from app.config import Settings
            url=make_url(Settings(_env_file=SERVER/'.env').database_url)
            assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
            name='tmp_vf_rediscancel_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_rediscancel_[0-9a-f]{32}',name)
            admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
            created=False
            try:
                await admin.execute('CREATE DATABASE "'+name+'"');created=True
                target=url.set(database=name).render_as_string(hide_password=False)
                await asyncio.to_thread(alembic,target,report,'upgrade','head')
                results['postgresql']=await asyncio.to_thread(verify,broker,target,args.runtime)
            finally:
                if created:
                    assert await admin.fetchval('SELECT current_database()')=='postgres'
                    await admin.execute('DROP DATABASE "'+name+'"')
                await admin.close()
            results['postgresql']['temporary_database_removed']=True
    finally:
        await broker.stop()
        (report/'broker-cleanup.json').write_text(json.dumps({'births':broker.births,'stops':broker.stops},indent=2),encoding='utf8')
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('REDIS_CANCEL_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items()}}))


if __name__=='__main__':asyncio.run(main())
