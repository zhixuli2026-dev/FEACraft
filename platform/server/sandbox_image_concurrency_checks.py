"""Two real API owners and repeated cancellation of registered image cleanup."""
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
from app.models import DurableEvent, NativeRequest
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native
from sandbox_retirement_checks import verify

FIXTURE = '''
import asyncio,json
from pathlib import Path
from fastapi import Depends,HTTPException
from app.main import app
from app.deps import get_current_user
from app.db import SessionLocal
from app.services import sandbox_image_cleanup as cleanup,native_request_records as records
from app.services.sandbox_retirement import authorized
tasks={}
_clean=cleanup.clean
async def clean(sid,*args):
 task=asyncio.current_task();tasks[sid]=task
 try:return await _clean(sid,*args)
 finally:
  if tasks.get(sid) is task:tasks.pop(sid)
cleanup.clean=clean
_release=records.release
async def release(*args,**kwargs):
 armed=Path('pause-image-release')
 if armed.exists():
  armed.unlink()
  Path('image-release-paused.json').write_text(json.dumps({'request_id':args[0].id,'process':args[3].record}))
  async with asyncio.timeout(30):
   while not Path('resume-image-release').exists():await asyncio.sleep(.02)
 return await _release(*args,**kwargs)
records.release=release
@app.post('/fixture/image-cancel/{sid}')
async def cancel(sid:str,user=Depends(get_current_user)):
 async with SessionLocal() as db:await authorized(db,sid,user.id)
 task=tasks.get(sid)
 if task is None:raise HTTPException(409,'No live image cleanup request')
 task.cancel()
 return {'cancel_requested':True,'still_running':not task.done()}
'''


async def after_retirement(api,sid,rid,proof,continuation=None):
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0,'checks':[],
        'database':'postgresql' if api.database_url else 'sqlite'}
    worker=None;log=(api.root/'second-api.log').open('w',encoding='utf8')
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    async def events(kind):
        async with engine.connect() as db:
            return list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type==kind)))
    async def request(identifier):
        async with engine.connect() as db:
            return dict((await db.execute(select(NativeRequest.__table__).where(NativeRequest.id==identifier))).mappings().one())
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind})
        print('IMAGE_CONCURRENCY_PASS '+name,flush=True)
    first=None
    try:
        selected=json.loads((await request(rid))['profile_json'])
        image=Path(selected['image_root'])/'volume.ext4';archive=Path(selected['root'])/'recovery.zip'
        original=archive.read_bytes()
        assert image.resolve().parent.parent==(api.root/'files/.terminal-export').resolve()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        args=list(api.proc.args);args[args.index('--port')+1]=str(port)
        worker=subprocess.Popen(args,cwd=api.root,env=api.env,stdout=log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        base='http://127.0.0.1:'+str(port);report['second_api']=base
        async with aiohttp.ClientSession(headers={'Authorization':'Bearer '+api.token},timeout=aiohttp.ClientTimeout(total=90)) as client:
            async with asyncio.timeout(30):
                while True:
                    assert worker.poll() is None
                    try:
                        async with client.get(base+'/api/health') as response:
                            if response.status==200:break
                    except aiohttp.ClientError:pass
                    await asyncio.sleep(.1)
            endpoint='/api/v1/terminal/sessions/'+sid+'/image-cleanup'
            async def submit(host):
                async with client.post(host+endpoint,json={'recovery_id':rid,'confirm':True}) as response:
                    return response.status,await response.text()
            (api.root/'pause-image-release').write_text('hold registered actual gate before final release')
            first=asyncio.create_task(submit(api.base))
            async with asyncio.timeout(25):
                while not (api.root/'image-release-paused.json').exists():
                    assert not first.done()
                    await asyncio.sleep(.02)
            paused=json.loads((api.root/'image-release-paused.json').read_text());process=paused['process']
            assert identity.probe(process['host'])['state']=='match'
            assert (await probe_native(process['native']['distribution'],process['native']['process']))['state']=='match'
            assert (await submit(base))[0]==409
            assert image.exists() and not await events('terminal.image_cleanup_completed')
            record('second_actual_api_refuses_while_registered_owner_and_gate_are_live')
            try:
                for _ in range(3):
                    async with client.post(api.base+'/fixture/image-cancel/'+sid) as response:
                        assert response.status==200
                        assert (await response.json())['still_running']
                assert not first.done()
                assert (await request(paused['request_id']))['phase']=='active'
                assert (await submit(base))[0]==409
                assert image.exists() and archive.read_bytes()==original
                record('three_cancellations_do_not_release_pending_native_scope_or_allow_peer_deletion')
            finally:(api.root/'resume-image-release').write_text('allow original executor to drain')
            reply=(await asyncio.gather(first,return_exceptions=True))[0]
            assert isinstance(reply,aiohttp.ClientError) or reply[0]==500
            report['cancelled_request_status']=type(reply).__name__ if isinstance(reply,BaseException) else reply[0]
            old=await request(paused['request_id']);children=json.loads(old['children_json'])['children']
            assert old['phase']=='cancelled' and len(children)==1
            child=next(iter(children.values()));assert child['stop']['verified'] is True
            assert identity.probe(process['host'])['state']=='exited'
            assert (await probe_native(process['native']['distribution'],process['native']['process']))['state']=='exited'
            assert image.exists(),'Cancelled gate must not release deletion payload'
            record('cancelled_request_records_actual_stop_and_original_image_survives')
            code,raw=await submit(base);assert code==200,(code,raw)
            done=json.loads(raw)
            assert done['image_absent'] and done['deleted_this_request'] is True and not image.exists()
            repeat=await asyncio.gather(submit(api.base),submit(base))
            assert all(status==200 and json.loads(body)==done for status,body in repeat)
            assert len(await events('terminal.image_cleanup_started'))==len(await events('terminal.image_cleanup_completed'))==1
            assert not await events('terminal.image_cleanup_abandoned'),'Verified normal cancellation does not invent abandonment'
            record('peer_retries_after_actual_drain_and_both_apis_return_one_completion')
            for host in (api.base,base):
                async with client.get(host+f'/fixture/{api.case_id}/validation') as response:assert response.status==200
                async with client.get(host+f'/fixture/{api.case_id}/admin') as response:
                    assert response.status==(409 if api.env.get('VIBEFLOW_STORAGE_BACKEND')=='s3' else 200)
            assert archive.read_bytes()==original
            async with client.get(api.base+f'/api/v1/terminal/sessions/{sid}/recoveries/{rid}/download') as response:
                assert response.status==200 and await response.read()==original
            record('retained_snapshot_download_and_validation_survive_two_api_cancel_retry')
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            record('real_model_reads_restoration_after_cancel_and_peer_cleanup','real_llm')
        report.update(passed=True,receipt=done)
        return report['llm_calls']
    finally:
        (api.root/'resume-image-release').write_text('ensure owned gate can drain')
        if first is not None and not first.done():await asyncio.gather(first,return_exceptions=True)
        if worker is not None:
            if worker.poll() is None:worker.terminate()
            await asyncio.to_thread(worker.wait,10)
            report['second_api_exited']=worker.returncode is not None
        log.close();await engine.dispose()
        trace=api.root/'upstream.jsonl'
        if trace.exists():
            report['llm_calls']=sum(row['kind']=='http' and row['path'].endswith('/chat/completions')
                for row in map(json.loads,trace.read_text().splitlines()))
        (api.root/'image-concurrency-checks.json').write_text(json.dumps(report,indent=2))


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    asyncio.run(verify(fixture_prefix=FIXTURE,after_retirement=after_retirement))
