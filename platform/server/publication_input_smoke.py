"""Real publication consumers, input races and retry lineage on disposable DBs."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid
from unittest.mock import patch

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from workflow_migration_smoke import SERVER, alembic, engine_for, legacy


async def verify(url, root):
    root.mkdir(parents=True)
    await asyncio.to_thread(alembic,url,root,'upgrade','head')
    await legacy(url,seed=True)
    from app import db as app_db, storage
    from app.main import app
    from app.models import Case, Job, JobAttempt, User, WorkflowStep
    from app.routers.geometry import _launch
    from app.security import create_token
    from app.services import job_execution as execution, jobrunner, attempt_workspace as workspace
    from app.services import quota, workflow_scheduler as scheduler
    from app.services.casedict import SCAFFOLD_FILES
    from app.services.publication_inputs import PublicationInputStale
    engine=engine_for(url)
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    prefix=f'{root.name}/workspaces/101/cases/101'
    original=storage._fs_path(prefix,'.')
    checks=[]
    def done(name):
        checks.append(name)
        print('PASS '+root.name+' '+name,flush=True)
    async def get_db():
        async with sessions() as db:
            yield db
    async def count():
        async with sessions() as db:
            return await db.scalar(select(func.count()).select_from(Job))
    async def publish(width=2):
        storage._fs_put(prefix,'A-geo/geometry.py',
            (f"import cadquery as cq\ndef build_geometry(ctx):\n    return {{'box':cq.Workplane('XY').box({width},3,4)}}\n").encode())
        async with sessions() as db:
            result=await _launch(101,'export',await db.get(User,101),db)
        proc=jobrunner.get_proc(result['job_id'])
        assert await proc.wait()=='succeeded',proc.backlog()
        return result
    def reference(publication, current=True, paths=None):
        return {'publication_id':publication['publication_id'], 'require_current':current,
            'paths':paths or ['A-geo/exports/box.step']}
    def spec_for(code):
        return {'command':'publication input consumer','runtime':'windows','case_prefix':prefix,
            'argv':[sys.executable,'-c',code],'timeout_seconds':45,'max_log_bytes':1024*1024,'evidence_run_dir':None}
    async def submit(refs,code="print('consumer')",paths=None,key=None):
        spec=spec_for(code)
        async with sessions() as db:
            job,created=await execution.submit(db,case_id=101,user_id=101,key=key,
                digest=execution.request_hash({'spec':spec,'paths':paths,'publications':refs}),
                job_type='shell',command=spec['command'],spec=spec,input_paths=paths,input_publications=refs)
            await db.commit()
            attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
            return job,attempt,spec,created
    def manifest(submitted):
        return json.loads(submitted[1].input_manifest_json)['workspace']
    async def launch(submitted,wait=True):
        job,_,spec,_=submitted
        proc=await jobrunner.start_job(job.id,spec['command'],prefix,argv=spec['argv'],
            timeout_seconds=spec['timeout_seconds'],max_log_bytes=spec['max_log_bytes'])
        if wait:
            await proc.wait()
        return proc
    async def state(submitted):
        async with sessions() as db:
            return await db.get(JobAttempt,submitted[1].id)
    async def wait_file(path):
        for _ in range(1500):
            if path.exists():
                return
            await asyncio.sleep(.02)
        raise TimeoutError(str(path))
    app.dependency_overrides[app_db.get_db]=get_db
    try:
        with patch.object(app_db,'SessionLocal',sessions):
            async with sessions() as db:
                case=await db.get(Case,101)
                case.s3_prefix,case.workflow_state=prefix,'A'
                db.add(User(id=202,email='publication-other@example.com',name='Other',password_hash='not-for-login'))
                db.add(Case(id=202,workspace_id=101,name='Other case',s3_prefix=prefix+'-other'))
                await db.commit()
            for path,content in SCAFFOLD_FILES.items():
                storage.put_text(prefix,path,content)
            first=await publish()
            artifact=original/'.vibeflow/publications'/first['publication_id']/'files/A-geo/exports/box.step'
            expected=artifact.read_bytes()
            expected_hash=hashlib.sha256(expected).hexdigest()
            # A native writer damaged only the compatibility alias. Consumers
            # must still read the immutable publication, never this live path.
            storage._fs_put(prefix,'A-geo/exports/box.step',b'wrong live alias')
            code="import cadquery as cq; from pathlib import Path; shape=cq.importers.importStep('A-geo/exports/box.step'); Path('volume.txt').write_text(str(shape.val().Volume()))"
            consumed=await submit([reference(first)],code,['0-caseDict/caseDict'])
            frozen=manifest(consumed)
            assert set(frozen['files'])=={'0-caseDict/caseDict','A-geo/exports/box.step'}
            assert frozen['publications'][0]['publication_id']==first['publication_id']
            assert frozen['publications'][0]['manifest_sha256'] and frozen['publications'][0]['require_current']
            consumed_proc=await launch(consumed)
            assert consumed_proc.status=='succeeded'
            run=original/frozen['run_path']
            assert abs(float((run/'volume.txt').read_text())-24)<1e-6
            assert storage.get_bytes(prefix,'A-geo/exports/box.step')==b'wrong live alias'
            assert not (original/'volume.txt').exists()
            done('real_CadQuery_consumer_reads_selected_immutable_STEP_with_live_inputs_and_exact_lineage')

            entire=await submit([{'publication_id':first['publication_id']}])
            assert {'A-geo/scene.json','A-geo/validation.json','A-geo/toolchain.json',
                'A-geo/geometry_manifest.json','A-geo/exports/box.step','A-geo/exports/box.stl'}==set(manifest(entire)['files'])
            assert (await launch(entire)).status=='succeeded'
            done('omitted_file_selection_seals_all_owned_manifest_artifacts')

            transport=httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,base_url='http://publication-input-test',
                    headers={'Authorization':'Bearer '+create_token(101)}) as client:
                body={'command':'type A-geo\\exports\\box.step > copied.step','input_publications':[reference(first)]}
                response=await client.post('/api/v1/cases/101/jobs',json=body,headers={'Idempotency-Key':'publication-api'})
                assert response.status_code==200,response.text
                api_job=response.json()['id']
                proc=jobrunner.get_proc(api_job)
                assert await proc.wait()=='succeeded',proc.backlog()
                assert hashlib.sha256((proc.cwd/'copied.step').read_bytes()).hexdigest()==expected_hash
                detail=(await client.get(f'/api/v1/jobs/{api_job}')).json()
                assert detail['attempt']['workspace']['selection']==[]
                assert detail['attempt']['outputs']['publications']==detail['attempt']['workspace']['publications']
                foreign=await client.get(f'/api/v1/jobs/{api_job}',headers={'Authorization':'Bearer '+create_token(202)})
                assert foreign.status_code==404
                done('HTTP_publication_only_submission_runs_in_own_workspace_and_details_enforce_ownership')
                for refs,status in [
                    ([{'publication_id':'not-uuid'}],400),
                    ([reference(first,paths=['../escape'])],400),
                    ([reference(first,paths=['A-geo/not-in-manifest.step'])],400),
                    ([reference(first),reference(first)],400),
                    ([dict(reference(first),require_current='false')],422),
                    ([dict(reference(first),unknown='field')],422),
                ]:
                    invalid=await client.post('/api/v1/cases/101/jobs',json={'command':'echo invalid','input_publications':refs})
                    assert invalid.status_code==status,invalid.text
                unknown=await client.post('/api/v1/cases/202/jobs',json={'command':'echo no','input_publications':[reference(first)]})
                missing=await client.post('/api/v1/cases/202/jobs',json={'command':'echo no','input_publications':[{'publication_id':str(uuid.uuid4())}]})
                assert unknown.status_code==missing.status_code==400 and unknown.json()==missing.json()
                denied=await client.post('/api/v1/cases/101/jobs',json=body,headers={'Authorization':'Bearer '+create_token(202)})
                assert denied.status_code==404
                done('reference_schema_path_membership_and_case_user_ownership_reject_invalid_inputs')

                before=await count()
                collision=await client.post('/api/v1/cases/101/jobs',json={**body,'input_paths':['A-geo/exports']})
                assert collision.status_code==400 and await count()==before,collision.text
                done('overlap_with_live_input_rejected_without_Job_or_silent_overwrite')

                pending=await submit([reference(first)],"from pathlib import Path; Path('must-not-run.txt').touch()")
                newer=await publish(5)
                replay=await client.post('/api/v1/cases/101/jobs',json=body,headers={'Idempotency-Key':'publication-api'})
                assert replay.status_code==200 and replay.json()['id']==api_job
                changed=await client.post('/api/v1/cases/101/jobs',json={**body,'input_publications':[reference(first,False)]},headers={'Idempotency-Key':'publication-api'})
                assert changed.status_code==409
                stale=await client.post('/api/v1/cases/101/jobs',json=body)
                assert stale.status_code==409 and stale.json()['detail']['code']=='input_stale',stale.text
                done('idempotent_replay_survives_head_change_but_new_current_requests_and_changed_payloads_are_rejected')
                try:
                    await launch(pending)
                    raise AssertionError('Stale queued dependency launched')
                except PublicationInputStale:
                    pass
                assert (await state(pending)).reason_code=='input_stale'
                assert not (original/manifest(pending)['run_path']/'must-not-run.txt').exists()
                done('dependency_changed_after_seal_is_rejected_before_native_launch')

                assert await execution.finish(consumed_proc.owner,'succeeded',0)=='succeeded'
                assert (await state(consumed)).phase=='succeeded'
                done('late_duplicate_completion_does_not_revoke_terminal_success_after_dependency_change')

                historical=await submit([reference(first,False)],code)
                assert (await launch(historical)).status=='succeeded'
                assert abs(float((original/manifest(historical)['run_path']/'volume.txt').read_text())-24)<1e-6
                assert (original/'A-geo/exports/box.step').read_bytes()!=expected
                done('explicit_historical_consumption_keeps_old_exact_geometry_after_new_head')

                wsl=await client.post('/api/v1/cases/101/jobs',json={'command':'sha256sum A-geo/exports/box.step > input.sha256',
                    'runtime':'wsl','input_publications':[reference(first,False)]})
                assert wsl.status_code==200,wsl.text
                wsl_proc=jobrunner.get_proc(wsl.json()['id'])
                assert await wsl_proc.wait()=='succeeded',wsl_proc.backlog()
                assert (wsl_proc.cwd/'input.sha256').read_text().startswith(expected_hash)
                done('real_WSL_consumer_uses_the_same_pinned_input_version')

            barrier=root/'running'
            code_wait=(f"from pathlib import Path; import time; p=Path({str(barrier)!r}); p.with_suffix('.ready').touch(); "
                "deadline=time.monotonic()+35\nwhile not p.with_suffix('.release').exists():\n assert time.monotonic()<deadline\n time.sleep(.02)\nPath('computed.txt').write_text('old pinned computation')")
            running=await submit([reference(newer)],code_wait)
            proc=await launch(running,wait=False)
            try:
                await wait_file(barrier.with_suffix('.ready'))
                newest=await publish(6)
            finally:
                barrier.with_suffix('.release').touch()
            assert await proc.wait()=='failed'
            failed=await state(running)
            assert failed.reason_code=='input_stale'
            output=json.loads(failed.output_manifest_json)
            assert output['dependency_error']=='input_stale' and 'computed.txt' in output['files']
            done('head_change_during_native_execution_keeps_output_inventory_but_refuses_success')

            finalizing=await submit([reference(newest)])
            original_finish=execution.finish
            ready,release=asyncio.Event(),asyncio.Event()
            async def delayed_finish(owner,*args,**kwargs):
                if owner.job_id==finalizing[0].id:
                    ready.set()
                    await asyncio.wait_for(release.wait(),40)
                return await original_finish(owner,*args,**kwargs)
            with patch.object(execution,'finish',delayed_finish):
                proc=await launch(finalizing,wait=False)
                try:
                    await asyncio.wait_for(ready.wait(),35)
                    current=await publish(7)
                finally:
                    release.set()
                assert await proc.wait()=='failed'
            assert (await state(finalizing)).reason_code=='input_stale'
            assert json.loads((await state(finalizing)).output_manifest_json)['dependency_error']=='input_stale'
            done('last_transaction_rechecks_dependencies_after_output_capture_before_Job_success')

            failure=await submit([reference(first,False)],"raise SystemExit(7)")
            assert (await launch(failure)).status=='failed'
            async with sessions() as db:
                retry,attempt=await execution.allocate_retry(db,failure[1].id)
                await db.commit()
            retried=(retry,attempt,failure[2],True)
            assert manifest(retried)['publications']==manifest(failure)['publications']
            assert manifest(retried)['files']==manifest(failure)['files']
            assert manifest(retried)['run_path']!=manifest(failure)['run_path']
            assert (await launch(retried)).status=='failed'
            done('restricted_retry_preserves_historical_publication_lineage_in_a_new_attempt')

            many_paths=[f'input-{index}.txt' for index in range(256)]
            for path in many_paths:
                storage.put_text(prefix,path,'small live input')
            many=await submit([reference(first,False)],"raise SystemExit(7)",many_paths)
            assert (await launch(many)).status=='failed'
            async with sessions() as db:
                retry,attempt=await execution.allocate_retry(db,many[1].id)
                await db.commit()
            retried_many=(retry,attempt,many[2],True)
            assert manifest(retried_many)['selection']==manifest(many)['selection']
            assert len(manifest(retried_many)['selection'])==256
            assert manifest(retried_many)['files']==manifest(many)['files']
            assert (await launch(retried_many)).status=='failed'
            done('retry_preserves_all_256_live_selections_plus_additional_publication_roots')

            retry_current=await submit([reference(current)],"raise SystemExit(8)")
            assert (await launch(retry_current)).status=='failed'
            current=await publish(8)
            before=await count()
            try:
                async with sessions() as db:
                    await execution.allocate_retry(db,retry_current[1].id)
                raise AssertionError('Retry silently relaxed current-version policy')
            except PublicationInputStale:
                pass
            assert await count()==before
            done('retry_of_current_only_input_cannot_silently_switch_to_historical_policy')

            original_script=storage.get_bytes(prefix,'A-geo/geometry.py')
            storage._fs_put(prefix,'A-geo/geometry.py',original_script+b'\nchanged_parameter=1\n')
            try:
                try:
                    await submit([reference(current)])
                    raise AssertionError('Stale source accepted as current')
                except PublicationInputStale:
                    pass
                assert (await launch(await submit([reference(current,False)]))).status=='succeeded'
            finally:
                storage._fs_put(prefix,'A-geo/geometry.py',original_script)
            done('changed_geometry_source_invalidates_current_dependency_without_replacing_head')

            before=await count()
            incoming=[]
            async def insufficient(db,user,size):
                incoming.append(size)
                raise ValueError('injected combined quota rejection')
            with patch.object(quota,'check_storage_quota',insufficient):
                try:
                    await submit([reference(current)],paths=['0-caseDict/caseDict'])
                    raise AssertionError('Quota rejection ignored')
                except ValueError:
                    pass
            assert await count()==before
            current_step=original/'.vibeflow/publications'/current['publication_id']/'files/A-geo/exports/box.step'
            assert incoming==[2*(current_step.stat().st_size+len(storage.get_bytes(prefix,'0-caseDict/caseDict')))]
            with patch.object(workspace,'MAX_BYTES',1):
                try:
                    await submit([reference(current)])
                    raise AssertionError('Publication bypassed input limits')
                except ValueError:
                    pass
            assert await count()==before
            done('combined_live_and_published_inputs_share_two_copy_quota_and_size_limits')

            saved=current_step.read_bytes()
            original_prepare=workspace.Snapshot.prepare
            def mutate_during_copy(snapshot):
                current_step.write_bytes(b'mutated after selection')
                return original_prepare(snapshot)
            try:
                with patch.object(workspace.Snapshot,'prepare',mutate_during_copy):
                    try:
                        await submit([reference(current)])
                        raise AssertionError('Changed publication was copied')
                    except ValueError:
                        pass
            finally:
                current_step.write_bytes(saved)
            assert await count()==before
            assert not list((original/'.vibeflow/attempts').glob('.preparing-*'))
            done('publication_change_during_copy_rejects_and_cleans_owned_stage_without_Job')

            async with sessions() as db:
                case=await db.get(Case,101)
                workflow,_=await scheduler.create_run(db,case_id=101,user=await db.get(User,101),
                    target='geometry',run_dir='C-run/one',expected_revision=case.dict_revision)
                await scheduler.reconcile(db,workflow.id)
                steps={s.step_key:s for s in await db.scalars(select(WorkflowStep).where(WorkflowStep.run_id==workflow.id))}
                await scheduler.complete_preflight(db,workflow.id,expected_version=steps['preflight'].version)
                await db.commit()
            async with sessions() as db:
                step=await db.get(WorkflowStep,steps['geometry'].id)
                spec=spec_for("from pathlib import Path; assert Path('A-geo/exports/box.step').exists()")
                job,_=await scheduler.queue_step(db,workflow.id,step.id,expected_version=step.version,
                    spec=spec,input_publications=[reference(current)])
                await db.commit()
                attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
            assert (await launch((job,attempt,spec,True))).status=='succeeded'
            async with sessions() as db:
                case=await db.get(Case,101)
                assert case.workflow_state=='A'
            done('workflow_queue_uses_same_publication_input_contract_without_automatic_engineering_advance')
    finally:
        app.dependency_overrides.pop(app_db.get_db,None)
        await engine.dispose()
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(root)}
    (root/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='publication-input-',dir=SERVER.parents[1]/'tmp'))
    pg_url=None
    if args.postgres:
        from app.config import Settings
        pg_url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert pg_url.drivername=='postgresql+asyncpg' and pg_url.host in {'127.0.0.1','localhost','::1'}
    url='sqlite+aiosqlite:///'+(report/'test.db').as_posix()
    os.environ.update(VIBEFLOW_DATABASE_URL=url,VIBEFLOW_STORAGE_BACKEND='fs',
        VIBEFLOW_STORAGE_FS_ROOT=str(report/'files'),VIBEFLOW_INDEX_ROOT=str(report/'index'),
        VIBEFLOW_AUDIT_DIR=str(report/'audit'),VIBEFLOW_JWT_SECRET=uuid.uuid4().hex,
        VIBEFLOW_RUNTIME_CONFIG=str(report/'runtime.json'),VIBEFLOW_DISTRIBUTED_JOBS='false')
    results={'sqlite':await verify(url,report/'sqlite')}
    if pg_url:
        import asyncpg
        name='tmp_vf_pubinput_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_pubinput_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=pg_url.host,port=pg_url.port or 5432,user=pg_url.username,
            password=pg_url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created=True
            results['postgresql']=await verify(pg_url.set(database=name).render_as_string(hide_password=False),report/'postgresql')
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('PUBLICATION_INPUT_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':
    asyncio.run(main())
