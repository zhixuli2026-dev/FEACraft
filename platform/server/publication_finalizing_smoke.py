"""Real Pandoc finalization resumes without replay after API death."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import uuid

from testsupport import IsolatedServer,SERVER
from job_release_smoke import wait_file
from job_launch_lease_smoke import PEER
from workflow_migration_smoke import alembic

INSPECT='''
from fastapi import Depends
from sqlalchemy import select
from app.db import SessionLocal
from app.models import ArtifactPublication,Job,JobAttempt,DurableEvent,User
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
@app.get('/api/v1/fixture/finalizing/{job_id}')
async def state(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db,write=False)
  attempts=list(await db.scalars(select(JobAttempt).where(JobAttempt.job_id==job_id)))
  a=attempts[-1]
  publication=await db.scalar(select(ArtifactPublication).where(ArtifactPublication.attempt_id==a.id))
  events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==a.id).order_by(DurableEvent.seq)))
  return {'status':job.status,'phase':a.phase,'attempt_count':len(attempts),'attempt_id':a.id,
   'publication_status':publication.status,'publication_id':publication.id,'output':json.loads(a.output_manifest_json),'checkpoint':json.loads(a.write_checkpoint_json),
   'events':[{'type':e.type,'payload':json.loads(e.payload_json)} for e in events]}
'''

CONTROLLED='''import os
os.environ['VIBEFLOW_JOB_LEASE_SECONDS']='10'
os.environ['VIBEFLOW_JOB_HEARTBEAT_SECONDS']='2'
import asyncio,json,time
from pathlib import Path
from app.services import publication_finalizing as recovery,publication_source_validation as source
root=Path(__file__).parent
def mode():return (root/'mode').read_text()
async def hold():
 (root/'ready').touch()
 for _ in range(6000):
  if (root/'release').exists():return
  await asyncio.sleep(.02)
 raise TimeoutError('Finalizing fixture release')
record=recovery.record
async def recording(*args,**kwargs):
 await record(*args,**kwargs)
 if mode()!='after_validation':await hold()
recovery.record=recording
inspect=source.SourceValidation.inspect
async def inspecting(self,*args,**kwargs):
 await inspect(self,*args,**kwargs)
 if mode()=='after_validation':await hold()
source.SourceValidation.inspect=inspecting
from app.main import app
'''+INSPECT


def peer(api):
    reader=IsolatedServer('finalizing-peer',app='finalizing_peer:app',database_url=api.database_url)
    overrides={k:api.env[k] for k in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
    script=PEER.replace('OVERRIDES',repr(overrides))
    # Any attempt to use the original builder is an explicit test failure.
    script+='''
import json
from pathlib import Path
from app.services import artifact_publication,publication_finalizing
async def forbidden(*args,**kwargs):
 Path(__file__).with_suffix('.replayed').touch()
 raise AssertionError('Native preparation replayed during finalizing recovery')
artifact_publication.prepare_for_finish=forbidden
'''+INSPECT
    (reader.root/'finalizing_peer.py').write_text(script,encoding='utf8');reader.token=api.token
    return reader


def wait_result(api,job_id,expected):
    deadline=time.monotonic()+70
    while time.monotonic()<deadline:
        row=api.call('GET',f'/jobs/{job_id}')
        if row['status']==expected and (expected!='running' or row.get('recovery')):
            return row
        time.sleep(.1)
    raise TimeoutError(str(row))


MODES=('prepared','after_validation','two_peers','edited_source','corrupt_bundle','live_owner','compose')


def verify(database_url=None,modes=MODES):
    checks=[];runs=[]
    for mode in modes:
        api=IsolatedServer('finalizing-'+mode,app='finalizing_fixture:app',database_url=database_url)
        (api.root/'finalizing_fixture.py').write_text(CONTROLLED,encoding='utf8')
        (api.root/'mode').write_text(mode)
        with api:
            api.create_case('finalizing '+mode)
            api.put('E-report/report.md','# Recover the prepared report\n\nActual generated content.\n')
            created=api.call('POST',api.prefix+('/report:compose?wait=false' if mode=='compose' else '/report:build'),
                {'overwrite':True} if mode=='compose' else {})
            job_id=created['job_id'];pub_id=created['publication_id']
            wait_file(api.root/'ready',seconds=90)
            before=api.call('GET',f'/fixture/finalizing/{job_id}')
            assert before['phase']=='finalizing' and before['publication_status']=='prepared',before
            assert before['output']['finalization_intent']['status']=='succeeded',before
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            bundle=root/'.vibeflow/publications'/pub_id
            artifacts=['E-report/build/report.html','E-report/build/report.docx']
            if mode=='compose':
                manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf8'))
                artifacts=list(manifest['files'])
            expected={p:(bundle/'files'/p).read_bytes() for p in artifacts}
            mtimes={p:(bundle/'files'/p).stat().st_mtime_ns for p in artifacts}
            if mode=='compose':
                from app.services.compose_artifacts import directory
                expected['E-report/report.md']=(bundle/'files'/directory(manifest['descriptor']['execution_id'])/'report.md').read_bytes()
            if mode!='live_owner':
                api.proc.terminate();api.proc.wait(timeout=10)
            if mode=='edited_source':(root/'E-report/report.md').write_text('# Changed while offline\n',encoding='utf8')
            if mode=='corrupt_bundle':(bundle/'files'/artifacts[0]).write_text('changed bytes',encoding='utf8')
            readers=[peer(api) for _ in range(2 if mode=='two_peers' else 1)]
            try:
                started=time.monotonic()
                if len(readers)==2:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        list(pool.map(lambda reader:reader.__enter__(),readers))
                else:readers[0].__enter__()
                assert time.monotonic()-started<12,'Recovery delayed API readiness'
                reader=readers[0]
                if mode=='live_owner':
                    time.sleep(.3)
                    after=reader.call('GET',f'/fixture/finalizing/{job_id}')
                    assert after['phase']=='finalizing' and after['publication_status']=='prepared'
                    assert not any(e['type']=='attempt.finalization_recovered' for e in after['events'])
                    (api.root/'release').touch();wait_result(api,job_id,'succeeded')
                elif mode=='corrupt_bundle':
                    wait_result(reader,job_id,'running')
                    assert (bundle/'files'/artifacts[0]).read_text()=='changed bytes'
                    assert not (root/artifacts[0]).exists()
                else:
                    expected_status='failed' if mode=='edited_source' else 'succeeded'
                    wait_result(reader,job_id,expected_status)
                    after=reader.call('GET',f'/fixture/finalizing/{job_id}')
                    assert after['attempt_id']==before['attempt_id'] and after['attempt_count']==1
                    assert after['publication_id']==pub_id
                    assert sum(e['type']=='attempt.finalization_recovered' for e in after['events'])==1,after
                    assert sum(e['type']=='jobs.updated' and e['payload']['status']==expected_status for e in after['events'])==1
                    if mode=='edited_source':
                        assert not (root/artifacts[0]).exists()
                    else:
                        assert after['publication_status']=='committed'
                        assert all((root/p).read_bytes()==value for p,value in expected.items())
                        assert all((bundle/'files'/p).stat().st_mtime_ns==stamp for p,stamp in mtimes.items())
                assert all(not (r.root/'finalizing_peer.replayed').exists() for r in readers)
                runs.append({'mode':mode,'api':str(api.root),'peers':[str(r.root) for r in readers],'job_id':job_id})
            finally:
                for reader in readers:
                    if reader.proc is not None:reader.__exit__(None,None,None)
            if mode not in {'live_owner','corrupt_bundle'}:
                with peer(api) as again:
                    repeated=again.call('GET',f'/fixture/finalizing/{job_id}')
                    assert repeated==after,'Repeated recovery changed terminal events or lineage'
            checks.append(mode+'_preserves_original_publication_and_no_native_replay')
            print('PASS '+checks[-1],flush=True)
    return {'passed':True,'count':len(checks),'checks':checks,'runs':runs}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--modes',nargs='+',choices=MODES,default=list(MODES));args=parser.parse_args()
    evidence=Path(tempfile.mkdtemp(prefix='finalizing-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify,modes=args.modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_finalizing_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,evidence,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary,args.modes)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (evidence/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('FINALIZING_RECOVERY_OK '+json.dumps({'databases':list(results),'evidence':str(evidence)}),flush=True)


if __name__=='__main__':asyncio.run(main())
