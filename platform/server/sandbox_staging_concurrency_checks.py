"""Actual two API workers, repeated cancellation and filesystem drain; no LLM."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from app.models import DurableEvent
from sandbox_retirement_checks import verify

FIXTURE = '''
import asyncio,json,time
from pathlib import Path
from fastapi import Depends,HTTPException
from app.main import app
from app.deps import get_current_user
from app.db import SessionLocal
from app.services import sandbox_staging_cleanup as cleanup
from app.services.sandbox_retirement import authorized
tasks={}
_clean=cleanup.clean
async def clean(sid,*args):
 task=asyncio.current_task();tasks[sid]=task
 try:return await _clean(sid,*args)
 finally:
  if tasks.get(sid) is task:tasks.pop(sid)
cleanup.clean=clean
_remove=cleanup.remove_entry
def remove(*args):
 _remove(*args)
 armed=Path('pause-staging-cleanup')
 if args[2]['kind']=='file' and armed.exists():
  armed.unlink();Path('staging-cleanup-paused.json').write_text(json.dumps({'removed':args[1]}))
  deadline=time.monotonic()+20
  while not Path('release-staging-cleanup').exists():
   if time.monotonic()>deadline:raise TimeoutError('Test filesystem gate timed out')
   time.sleep(.02)
  Path('staging-cleanup-drained').write_text('actual I/O resumed')
cleanup.remove_entry=remove
@app.post('/fixture/staging-cancel/{sid}')
async def cancel(sid:str,user=Depends(get_current_user)):
 async with SessionLocal() as db:await authorized(db,sid,user.id)
 task=tasks.get(sid)
 if task is None:raise HTTPException(409,'No live cleanup request')
 task.cancel()
 return {'cancel_requested':True,'still_running':not task.done()}
'''


async def after_retirement(api,sid,rid,proof):
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0,'checks':[]}
    worker=None;log=(api.root/'second-api.log').open('w',encoding='utf8')
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    report['database']='postgresql' if api.database_url else 'sqlite'
    async def events(kind):
        async with engine.connect() as db:
            return list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type==kind)))
    def record(name):
        report['checks'].append({'name':name,'kind':'actual_infrastructure'})
        print('STAGING_CONCURRENCY_PASS '+name,flush=True)
    try:
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        args=list(api.proc.args);args[args.index('--port')+1]=str(port)
        worker=subprocess.Popen(args,cwd=api.root,env=api.env,stdout=log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        base='http://127.0.0.1:'+str(port)
        report['second_api']=base
        async with aiohttp.ClientSession(headers={'Authorization':'Bearer '+api.token},timeout=aiohttp.ClientTimeout(total=40)) as client:
            async with asyncio.timeout(30):
                while True:
                    assert worker.poll() is None
                    try:
                        async with client.get(base+'/api/health') as response:
                            if response.status==200:break
                    except aiohttp.ClientError:pass
                    await asyncio.sleep(.1)
            endpoint='/api/v1/terminal/sessions/'+sid+'/staging-cleanup'
            async def submit(host):
                async with client.post(host+endpoint,json={'recovery_id':rid,'confirm':True}) as response:
                    return response.status,await response.text()
            (api.root/'pause-staging-cleanup').write_text('pause after actual first unlink')
            first=asyncio.create_task(submit(api.base))
            async with asyncio.timeout(15):
                while not (api.root/'staging-cleanup-paused.json').exists():
                    assert not first.done()
                    await asyncio.sleep(.02)
            second=asyncio.create_task(submit(base))
            try:
                for _ in range(3):
                    async with client.post(api.base+'/fixture/staging-cancel/'+sid) as response:
                        assert response.status==200
                        assert (await response.json())['still_running']
                await asyncio.sleep(.2)
                assert not first.done() and not second.done()
                assert not (api.root/'staging-cleanup-drained').exists()
                assert not await events('terminal.staging_cleanup_completed')
                record('three_real_cancellations_keep_original_lock_until_actual_io_drains')
            finally:(api.root/'release-staging-cleanup').write_text('release actual I/O')
            replies=await asyncio.gather(first,second,return_exceptions=True)
            assert not isinstance(replies[1],BaseException),type(replies[1]).__name__
            assert replies[1][0]==200,replies[1][0]
            assert json.loads(replies[1][1])['status']=='completed'
            assert isinstance(replies[0],aiohttp.ClientError) or replies[0][0]==500
            assert (api.root/'staging-cleanup-drained').exists()
            report['cancelled_request_status']=type(replies[0]).__name__ if isinstance(replies[0],BaseException) else replies[0][0]
            record('second_actual_worker_reconciles_after_cancelled_writer_drains')
            repeat=await asyncio.gather(submit(api.base),submit(base))
            assert all(status==200 and json.loads(body)==json.loads(replies[1][1]) for status,body in repeat)
            assert len(await events('terminal.staging_cleanup_started'))==1
            assert len(await events('terminal.staging_cleanup_completed'))==1
            for host in (api.base,base):
                async with client.get(host+f'/fixture/{api.case_id}/admin') as response:assert response.status==200
            record('both_actual_workers_return_one_receipt_and_release_admin')
            async with client.get(api.base+f'/api/v1/terminal/sessions/{sid}/recoveries/{rid}/download') as response:
                assert response.status==200 and len(await response.read())>0
            record('retained_snapshot_download_survives_cancelled_cross_worker_cleanup')
        report['passed']=True
        return 0
    finally:
        (api.root/'release-staging-cleanup').write_text('ensure outstanding I/O drains')
        if worker is not None:
            if worker.poll() is None:worker.terminate()
            await asyncio.to_thread(worker.wait,10)
            report['second_api_exited']=worker.returncode is not None
        log.close()
        await engine.dispose()
        (api.root/'staging-concurrency-checks.json').write_text(json.dumps(report,indent=2))


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    from s3testsupport import ObjectStore
    with ObjectStore(executable=Path(__file__).resolve().parents[2]/'third_party/seaweed-4.52-runtime/weed') as store:
        asyncio.run(verify(store,fixture_prefix=FIXTURE,after_retirement=after_retirement))
