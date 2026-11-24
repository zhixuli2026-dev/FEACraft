"""Actual validated report copy interrupted before original drain metadata."""
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
from publication_finalizing_smoke import INSPECT,peer,wait_result
from workflow_migration_smoke import alembic

CONTROLLED='''import asyncio,json,time
from pathlib import Path
from app.services import artifact_publication as pub,attempt_workspace as workspace
root=Path(__file__).parent
def mode():return (root/'mode').read_text()
def hold():
 (root/'ready').touch()
 deadline=time.monotonic()+120
 while not (root/'release').exists():
  if time.monotonic()>deadline:raise TimeoutError('Copy preparation fixture')
  time.sleep(.02)
copy=pub.prepare_bundle
def copying(*args):
 if mode() not in {'partial_copy','after_copy'}:hold()
 result=copy(*args)
 if mode()=='after_copy':hold()
 return result
pub.prepare_bundle=copying
files=workspace.copy_records
def copy_files(source,target,records,directories,**kwargs):
 if mode()=='partial_copy' and target.parent.name.startswith('.preparing-') and target.parent.parent.name=='publications':
  first=next(iter(records))
  files(source,target,{first:records[first]},directories,**kwargs)
  hold()
  raise AssertionError('Original partial copier must be terminated')
 return files(source,target,records,directories,**kwargs)
workspace.copy_records=copy_files
from app.main import app
'''+INSPECT+'''
@app.post('/api/v1/fixture/preparation/{job_id}')
async def mutate(job_id:int,body:dict,user:User=Depends(get_current_user)):
 from app.services.durable_events import lock_case_execution,json_text
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  await lock_case_execution(db,job.case_id)
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  value=json.loads(attempt.output_manifest_json)
  if body['mode']=='missing_proof':value.pop('bundle_preparation')
  else:value['bundle_preparation']['stop']['verified']=False
  attempt.output_manifest_json=json_text(value);await db.commit()
 return {'ok':True}
'''

REHOLD='''
from app.services import publication_preparation_recovery as recovery
import asyncio,time
from pathlib import Path
recovery_root=Path(__file__).parent
def wait_recovery():
 (recovery_root/'recovery.ready').touch()
 deadline=time.monotonic()+120
 while time.monotonic()<deadline:time.sleep(.02)
 raise TimeoutError('Recovery death fixture')
original=recovery.resume_copy
def copying(records):
 result=original(records)
 wait_recovery()
 return result
recovery.resume_copy=copying
'''
AFTER_DRAIN='''
from app.services import publication_preparation_recovery as recovery
import asyncio
from pathlib import Path
original=recovery.checkpoint
async def drained(*args):
 result=await original(*args)
 (Path(__file__).parent/'recovery.ready').touch()
 await asyncio.Event().wait()
 return result
recovery.checkpoint=drained
@app.post('/api/v1/fixture/change-drain/{job_id}')
async def change_drain(job_id:int,user:User=Depends(get_current_user)):
 from app.services.durable_events import lock_case_execution
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  await lock_case_execution(db,job.case_id)
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  event=await db.scalar(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type=='attempt.bundle_copy_drained'))
  event.payload_json='{}';await db.commit()
 return {'ok':True}
'''

ALIAS_HOLD='''
from app.services import artifact_publication
from pathlib import Path
import time
def hold_aliases(*args):
 (Path(__file__).parent/'recovery.ready').touch()
 deadline=time.monotonic()+120
 while time.monotonic()<deadline:time.sleep(.02)
 raise TimeoutError('Committed alias recovery fixture')
artifact_publication._write_aliases=hold_aliases
'''

MODES=('before_copy','partial_copy','after_copy','second_crash','after_drain','after_terminal_commit','changed_drain_history','edited_output','edited_source','missing_proof','corrupt_proof','two_peers','owner_live')


def verify(database_url=None,modes=MODES,handler='report'):
    checks=[];runs=[]
    for mode in modes:
        api=IsolatedServer('prebundle-'+handler+'-'+mode,app='prebundle_fixture:app',database_url=database_url)
        (api.root/'prebundle_fixture.py').write_text(CONTROLLED,encoding='utf8')
        (api.root/'mode').write_text(mode)
        with api:
            api.create_case('Prebundle recovery '+mode)
            workflow_id=None
            source='A-geo/geometry.py' if handler=='geometry' else 'E-report/latex/main.tex' if handler=='latex' else 'E-report/report.md'
            if handler=='geometry':
                from geometry_publication_smoke import script
                # The public geometry file-save route also queues an automatic
                # preview. Seed this disposable fixture before the one workflow
                # submission so its barrier belongs to the workflow attempt.
                fixture_root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                (fixture_root/source).write_text(script(2),encoding='utf8')
                revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
                workflow=api.call('POST',api.prefix+'/workflow-runs',{'target':'geometry','expected_revision':revision,
                    'parameters':{'geometry_mode':'preview'},'budget':{'timeout_seconds':120,'max_log_bytes':1024*1024}})
                workflow_id=workflow['id']
            else:
                api.put(source,r'\documentclass{article}\begin{document}Retained original TeX result.\end{document}' if handler=='latex'
                    else '# Original prepared result\n\nDo not recompute this report.\n')
                submission=api.call('POST',api.prefix+('/latex:build' if handler=='latex' else '/report:build'),
                    {'main':source,'recipe':'pdflatex'} if handler=='latex' else {})
            wait_file(api.root/'ready',seconds=220 if handler=='latex' else 90)
            if handler=='geometry':
                jobs=api.call('GET',api.prefix+'/jobs');assert len(jobs)==1,jobs
                job_id=jobs[0]['id']
            else:job_id=submission['job_id']
            before=api.call('GET',f'/fixture/finalizing/{job_id}')
            publication=before['publication_id']
            assert before['checkpoint']=={} and before['phase']=='finalizing',before
            assert 'bundle_preparation' in before['output'] and 'finalization_intent' not in before['output']
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            journal=root/'.vibeflow/publications/.journal'/publication
            manifest=json.loads((journal/'intent.json').read_text(encoding='utf8'))['manifest']
            run=root/'.vibeflow/attempts'/before['attempt_id']/'run'
            paths=['E-report/build/report.html','E-report/build/report.docx']
            if handler=='latex':
                from app.services.latex_artifacts import paths as latex_paths
                paths=[latex_paths(source)[key] for key in ('pdf','synctex')]
            elif handler=='geometry':paths=list(manifest['files'])
            expected={p:(run/p).read_bytes() for p in paths}
            target=root/'.vibeflow/publications'/publication
            timestamps={p:(target/'files'/p).stat().st_mtime_ns for p in paths} if mode=='after_copy' else None
            if mode in {'missing_proof','corrupt_proof'}:
                api.call('POST',f'/fixture/preparation/{job_id}',{'mode':mode})
            if mode!='owner_live':api.proc.terminate();api.proc.wait(timeout=10)
            if mode=='edited_output':(run/paths[0]).write_text('CHANGED VALIDATED OUTPUT',encoding='utf8')
            if mode=='edited_source':(root/source).write_text('A later source',encoding='utf8')
            if mode in {'second_crash','after_drain','changed_drain_history','after_terminal_commit'}:
                first=peer(api)
                with (first.root/'finalizing_peer.py').open('a',encoding='utf8') as stream:
                    stream.write(REHOLD if mode=='second_crash' else ALIAS_HOLD if mode=='after_terminal_commit' else AFTER_DRAIN)
                with first:
                    wait_file(first.root/'recovery.ready',seconds=90)
                    timestamps={p:(target/'files'/p).stat().st_mtime_ns for p in paths}
                    if mode=='changed_drain_history':first.call('POST',f'/fixture/change-drain/{job_id}',{})
                    first.proc.terminate();first.proc.wait(timeout=10)
            readers=[peer(api) for _ in range(2 if mode=='two_peers' else 1)]
            try:
                start=time.monotonic()
                with ThreadPoolExecutor(max_workers=len(readers)) as pool:
                    list(pool.map(lambda reader:reader.__enter__(),readers))
                assert time.monotonic()-start<12,'Recovery blocked API readiness'
                reader=readers[0]
                if mode=='owner_live':
                    time.sleep(.2)
                    held=reader.call('GET',f'/fixture/finalizing/{job_id}')
                    assert held['checkpoint']=={} and held['phase']=='finalizing'
                    (api.root/'release').touch();wait_result(api,job_id,'succeeded')
                elif mode in {'edited_output','missing_proof','corrupt_proof','changed_drain_history'}:
                    wait_result(reader,job_id,'running')
                    assert not (root/paths[0]).exists()
                    if mode=='changed_drain_history':
                        assert all((target/'files'/p).read_bytes()==data for p,data in expected.items())
                    else:assert not target.exists()
                else:
                    expected_status='failed' if mode=='edited_source' else 'succeeded'
                    wait_result(reader,job_id,expected_status)
                    after=reader.call('GET',f'/fixture/finalizing/{job_id}')
                    assert after['attempt_count']==1 and after['attempt_id']==before['attempt_id']
                    assert after['publication_id']==publication
                    assert after['checkpoint']['schema']=='vibeflow.job-write-checkpoint.v3'
                    assert after['checkpoint']['stop']==before['output']['bundle_preparation']['stop']
                    assert sum(e['type']=='attempt.bundle_copy_drained' for e in after['events'])==1
                    assert sum(e['type']=='attempt.finalization_recovered' for e in after['events'])==1
                    assert sum(e['type']=='jobs.updated' and e['payload']['status']==expected_status for e in after['events'])==1
                    assert all((target/'files'/p).read_bytes()==data for p,data in expected.items())
                    if expected_status=='succeeded':assert all((root/p).read_bytes()==data for p,data in expected.items())
                    else:assert not (root/paths[0]).exists()
                    if timestamps:assert all((target/'files'/p).stat().st_mtime_ns==stamp for p,stamp in timestamps.items())
                    if mode=='partial_copy':assert list(journal.glob('interrupted-*')),'Partial copy evidence was discarded'
                    if workflow_id and expected_status=='succeeded':
                        workflow=reader.call('GET',f'/workflow-runs/{workflow_id}')
                        step=next(s for s in workflow['steps'] if s['key']=='geometry')
                        assert workflow['status']=='awaiting_confirmation' and step['status']=='succeeded',workflow
                        assert step['publication']['id']==publication and len(step['attempts'])==1
                        assert reader.call('GET',api.prefix)['workflow_state']=='A'
                assert all(not (r.root/'finalizing_peer.replayed').exists() for r in readers)
                runs.append({'mode':mode,'api':str(api.root),'peers':[str(r.root) for r in readers],'job_id':job_id})
            finally:
                for reader in readers:
                    if reader.proc is not None:reader.__exit__(None,None,None)
            if mode not in {'owner_live','edited_output','missing_proof','corrupt_proof','changed_drain_history'}:
                with peer(api) as repeated:
                    assert repeated.call('GET',f'/fixture/finalizing/{job_id}')==after
            checks.append(mode+'_preserves_validated_outputs_and_original_lineage')
            print('PASS '+checks[-1],flush=True)
    return {'passed':True,'handler':handler,'count':len(checks),'checks':checks,'runs':runs}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--handler',choices=('report','latex','geometry'),default='report')
    parser.add_argument('--modes',nargs='+',choices=MODES,default=list(MODES));args=parser.parse_args()
    evidence=Path(tempfile.mkdtemp(prefix='prebundle-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify,modes=args.modes,handler=args.handler)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_prebundle_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,evidence,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary,args.modes,args.handler)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (evidence/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('PREBUNDLE_RECOVERY_OK '+json.dumps({'databases':list(results),'evidence':str(evidence)}),flush=True)


if __name__=='__main__':asyncio.run(main())
