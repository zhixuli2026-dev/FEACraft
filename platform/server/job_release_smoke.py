"""Two actual API instances serialize persisted cancellation with native release."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from professional_launch_smoke import settled
from app.services.owned_process import wsl_path
from workflow_migration_smoke import alembic

CONTROLLED='''import asyncio,json
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import SessionLocal
from app.models import JobAttempt
from app.services import job_execution,jobrunner
from app.services.owned_process import OwnedProcess
root=Path(__file__).parent
failure=ContextVar('fixture_release_commit_failure',default=False)
async def barrier(name):
 (root/(name+'.ready')).touch()
 for _ in range(1000):
  if (root/(name+'.continue')).exists():return
  await asyncio.sleep(.02)
 raise TimeoutError('Fixture '+name+' barrier')
original_phase=job_execution.phase
async def phase(owner,target,**kwargs):
 result=await original_phase(owner,target,**kwargs)
 if target=='running':
  (root/'registration.json').write_text(json.dumps({'job_id':owner.job_id,'identity':kwargs['identity']}))
  mode=(root/'mode').read_text()
  if mode=='cancel_before':await barrier('registered')
  if mode=='expired':
   async with SessionLocal() as db:
    row=await db.get(JobAttempt,owner.attempt_id)
    row.lease_expires_at=job_execution.now()-timedelta(seconds=1)
    await db.commit()
 return result
job_execution.phase=phase
original_release=OwnedProcess.release
async def release(self):
 record=json.loads((root/'registration.json').read_text()) if (root/'registration.json').exists() else {}
 primary=self.nonce==record.get('identity',{}).get('launch_nonce')
 if primary and (root/'mode').read_text()=='release_first':await barrier('release_locked')
 result=await original_release(self)
 if primary:(root/'release.sent').touch()
 return result
OwnedProcess.release=release
original_guard=job_execution.release_process
async def guard(owner,process):
 token=failure.set((root/'mode').read_text()=='commit_failure')
 try:return await original_guard(owner,process)
 finally:failure.reset(token)
job_execution.release_process=guard
original_commit=AsyncSession.commit
async def commit(self):
 if failure.get():
  (root/'commit.failed').touch()
  raise OSError('Injected release commit failure')
 return await original_commit(self)
AsyncSession.commit=commit
from app.main import app
'''

PEER='''import os
from pathlib import Path
os.environ.update(OVERRIDES)
from app.services import jobrunner,distributed_queue
root=Path(__file__).parent
original=jobrunner._persist_status
async def persist(*args,**kwargs):
 (root/'cancel.entered').touch()
 return await original(*args,**kwargs)
jobrunner._persist_status=persist
distributed_queue.distributed_enabled=lambda:True
async def unavailable(job_id):
 (root/'notification.failed').touch()
 raise OSError('Injected Redis notification outage')
distributed_queue.cancel=unavailable
from app.main import app
'''


def wait_file(path,seconds=20):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if path.exists():return
        time.sleep(.02)
    raise TimeoutError(str(path))


def verify(database_url=None):
    api=IsolatedServer('job-release',app='release_control:app',database_url=database_url)
    (api.root/'release_control.py').write_text(CONTROLLED,encoding='utf8')
    (api.root/'mode').write_text('normal')
    checks=[];receipts=[]
    sentinel=subprocess.Popen([sys.executable,'-I','-S','-u','-c','import sys;print("READY",flush=True);sys.stdin.buffer.read(1)'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    assert sentinel.stdout.readline().strip()==b'READY'
    try:
        with api:
            peer=IsolatedServer('job-release-peer',app='release_peer:app',database_url=database_url)
            overrides={key:api.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
            if database_url is None:overrides['VIBEFLOW_DATABASE_URL']=api.env['VIBEFLOW_DATABASE_URL']
            (peer.root/'release_peer.py').write_text(PEER.replace('OVERRIDES',repr(overrides)),encoding='utf8')
            # Both APIs are started before any live Job. Existing startup-wide
            # interruption is a separate N5 concern, not suppressed by this test.
            with peer:
                for runtime,mode in [(r,m) for r in ('windows','wsl') for m in ('normal','cancel_before','release_first')]+[
                        ('windows','expired'),('windows','commit_failure')]:
                    for name in ('registration.json','registered.ready','registered.continue','release_locked.ready',
                                 'release_locked.continue','release.sent','commit.failed'):
                        (api.root/name).unlink(missing_ok=True)
                    for name in ('cancel.entered','notification.failed'):(peer.root/name).unlink(missing_ok=True)
                    (api.root/'mode').write_text(mode)
                    api.create_case(runtime+' '+mode);peer.token=api.token
                    marker=api.root/(runtime+'-'+mode+'.started');finish=api.root/(runtime+'-'+mode+'.finish')
                    payload=api.root/(runtime+'-'+mode+'.py')
                    marker_path=str(marker) if runtime=='windows' else wsl_path(marker)
                    finish_path=str(finish) if runtime=='windows' else wsl_path(finish)
                    payload.write_text('from pathlib import Path\nimport time\nPath('+repr(marker_path)+').write_text("started")\n'
                        'end=time.monotonic()+30\nwhile not Path('+repr(finish_path)+').exists() and time.monotonic()<end:time.sleep(.02)\n',encoding='utf8')
                    command=('"'+sys.executable+'" "'+str(payload)+'"' if runtime=='windows' else 'python3 '+wsl_path(payload))
                    expected=409 if mode in {'cancel_before','expired','commit_failure'} else 200
                    cancel_reply=None
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        submitted=pool.submit(api.call,'POST',api.prefix+'/jobs',{'command':command,'runtime':runtime},expect=expected)
                        if mode=='cancel_before':
                            wait_file(api.root/'registered.ready')
                            launch=json.loads((api.root/'registration.json').read_text());job_id=launch['job_id']
                            assert not marker.exists()
                            cancel_reply=peer.call('POST',f'/jobs/{job_id}:cancel')
                            before=peer.call('GET',f'/jobs/{job_id}')
                            assert cancel_reply['ok'] and before['status']=='cancelled' and before['attempt']['phase']=='cancelling'
                            assert not marker.exists()
                            (api.root/'registered.continue').touch()
                        elif mode=='release_first':
                            wait_file(api.root/'release_locked.ready')
                            launch=json.loads((api.root/'registration.json').read_text());job_id=launch['job_id']
                            cancelling=pool.submit(peer.call,'POST',f'/jobs/{job_id}:cancel')
                            wait_file(peer.root/'cancel.entered')
                            time.sleep(.15)
                            assert not cancelling.done(),'Cancellation crossed the held release transaction'
                            assert not marker.exists()
                            (api.root/'release_locked.continue').touch()
                            cancel_reply=cancelling.result(timeout=20)
                            assert cancel_reply['ok']
                        response=submitted.result(timeout=30)
                    launch=json.loads((api.root/'registration.json').read_text());job_id=launch['job_id']
                    if expected==409:
                        if mode!='commit_failure':assert response['detail']['job_id']==job_id,response
                        assert not (api.root/'release.sent').exists() if mode!='commit_failure' else (api.root/'commit.failed').exists()
                    if mode=='normal':
                        wait_file(marker);finish.touch()
                    elif mode=='release_first':
                        wait_file(marker)
                    row=settled(api,job_id)
                    assert row['status']==('succeeded' if mode=='normal' else 'cancelled' if mode in {'cancel_before','release_first'} else 'failed'),row
                    stop=row['attempt']['process_stop'];record=row['attempt']['process_identity']
                    assert stop['verified'] and stop['launch_nonce']==record['launch_nonce'],row
                    if mode=='cancel_before':
                        assert not marker.exists() and stop['reason']=='cancelled'
                    if mode=='expired':assert not marker.exists() and stop['reason']=='ownership_lost'
                    if mode in {'cancel_before','release_first'}:
                        assert (peer.root/'notification.failed').exists()
                        assert not peer.call('POST',f'/jobs/{job_id}:cancel')['ok']
                    if runtime=='wsl':assert stop['native_state']=='exited'
                    observed=peer.call('GET',f'/jobs/{job_id}/process')
                    assert observed['state']=='exited',observed
                    persisted=peer.call('GET',f'/jobs/{job_id}')
                    assert persisted['attempt']['process_stop']==stop and persisted['status']==row['status']
                    assert sentinel.poll() is None
                    receipts.append({'runtime':runtime,'mode':mode,'job':row,'response':response,'cancel_reply':cancel_reply,
                        'observation':observed,'payload_started':marker.exists(),'external_sentinel_alive':True})
                    checks.append(runtime+'_'+mode+'_serializes_cancel_release_and_preserves_actual_cleanup')
                    (api.root/'release-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
                    print('PASS '+checks[-1],flush=True)
                token=peer.token;peer.create_case('Other release reader')
                peer.call('GET',f'/jobs/{receipts[0]["job"]["id"]}',expect=404);peer.token=token
    finally:
        sentinel.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),'peer_evidence':str(peer.root),
            'real_redis_used':False,'notification_failure_injected':True,'databases_isolated':True}
    (api.root/'release-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='job-release-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_release_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_release_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,
            password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('JOB_RELEASE_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
