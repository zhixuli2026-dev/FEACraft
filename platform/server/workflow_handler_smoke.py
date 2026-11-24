"""Registered geometry workflows through two real APIs and actual CadQuery."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
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
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic
from geometry_publication_smoke import script, wait_file

CONTROLLED = '''from pathlib import Path
from dataclasses import replace
import asyncio
import sys
import json
from app.services.geometry_handler import GeometryHandler
from app.services import step_handlers
from app.services.geo_script import __file__ as worker
root=Path(__file__).parent
original_prepare=GeometryHandler.prepare
async def prepare(self,db,case,user,**kwargs):
    result=await original_prepare(self,db,case,user,**kwargs)
    if (root/f'tamper-{case.id}').exists():
        result=replace(result,spec={**result.spec,'argv':[sys.executable,'-c','pass']})
    if (root/f'badworker-{case.id}').exists():
        result=replace(result,descriptor={**result.descriptor,'worker_sha256':'0'*64})
    return result
GeometryHandler.prepare=prepare
original_spawn=asyncio.create_subprocess_exec
async def spawn(original_spawn,*args,**kwargs):
    if len(args)>1 and str(args[1])==worker and kwargs.get('cwd'):
        cwd=Path(kwargs['cwd'])
        parts=cwd.parts
        case_id=parts[parts.index('cases')+1]
        if (root/f'noop-{case_id}').exists():
            return await original_spawn(sys.executable,'-c',"print('injected zero-exit without worker outputs')",**kwargs)
    return await original_spawn(*args,**kwargs)
from testsupport import intercept_owned_spawn
asyncio.create_subprocess_exec=intercept_owned_spawn(original_spawn,spawn)
original_launch=step_handlers.launch
async def launch(job_id):
    from app.db import SessionLocal
    from app.models import Job, JobAttempt
    from sqlalchemy import select
    async with SessionLocal() as db:
        job=await db.get(Job,job_id)
        if (root/f'defer-{job.case_id}').exists():
            return None
        if (root/f'badtoolchain-{job.case_id}').exists() or (root/f'oldtoolchain-{job.case_id}').exists():
            from app.services import job_execution
            attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
            document=json.loads(attempt.input_manifest_json)
            if (root/f'badtoolchain-{job.case_id}').exists():
                document['handler']['toolchain']['components']['worker_sha256']='0'*64
            else:
                document['handler'].pop('toolchain')
            attempt.input_manifest_json=json.dumps(document)
            await db.commit()
            if (root/f'badtoolchain-{job.case_id}').exists():
                await job_execution.claim(db,job_id,document['execution_spec'])
                raise AssertionError('Damaged toolchain was claimed')
        if (root/f'unbind-{job.case_id}').exists():
            from app.services import job_execution
            attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
            document=json.loads(attempt.input_manifest_json)
            document.pop('handler')
            attempt.input_manifest_json=json.dumps(document)
            await db.commit()
            await job_execution.claim(db,job_id,document['execution_spec'])
            raise AssertionError('Unbound registered attempt was claimed')
    return await original_launch(job_id)
step_handlers.launch=launch
from app.main import app
'''


def verify(database_url=None):
    api=IsolatedServer('workflow-handler',app='controlled_workflow_handler:app',database_url=database_url)
    (api.root/'controlled_workflow_handler.py').write_text(CONTROLLED,encoding='utf-8')
    peer=IsolatedServer('workflow-handler-peer')
    checks=[]
    def done(name):
        checks.append(name)
        print('PASS '+name,flush=True)
    def start_peer():
        peer.log=(peer.root/'api.log').open('a',encoding='utf-8')
        peer.proc=subprocess.Popen([sys.executable,'-m','uvicorn','controlled_workflow_handler:app',
            '--app-dir',str(SERVER),'--host','127.0.0.1','--port',str(peer.port),'--no-access-log'],
            cwd=peer.root,env=api.env,stdout=peer.log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        for _ in range(200):
            if peer.proc.poll() is not None:
                raise AssertionError((peer.root/'api.log').read_text(encoding='utf-8'))
            try:
                if peer.call('GET','/api/health',api_prefix=False).get('ok'):
                    return
            except OSError:
                pass
            time.sleep(.1)
        raise TimeoutError('Peer did not start')
    def stop_peer():
        if peer.proc and peer.proc.poll() is None:
            peer.proc.terminate()
            try:
                peer.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                peer.proc.kill()
                peer.proc.wait(timeout=5)
        peer.log.close()
    with api:
        api.create_case('registered workflow')
        peer.token=api.token
        workspace_id=api.call('GET','/workspaces')[0]['id']
        def root_for(identifier):
            return next((api.root/'files/workspaces').glob(f'*/cases/{identifier}'))
        def new_case(name,source=None):
            result=api.call('POST',f'/workspaces/{workspace_id}/cases',{'name':name})
            (root_for(result['id'])/'A-geo/geometry.py').write_text(source or script(2),encoding='utf-8')
            return result['id']
        def request(case_id,mode='preview'):
            revision=api.call('GET',f'/cases/{case_id}/caseDict')['dict_revision']
            return {'target':'geometry','expected_revision':revision,'parameters':{'geometry_mode':mode},
                'budget':{'timeout_seconds':60,'max_log_bytes':1024*1024}}
        def submit(case_id,mode='preview',key=None,client=api,body=None,expect=200):
            return client.call('POST',f'/cases/{case_id}/workflow-runs',body or request(case_id,mode),expect=expect,
                extra_headers={'Idempotency-Key':key} if key else None)
        def detail(run,client=api):
            identifier=run['id'] if isinstance(run,dict) else run
            return client.call('GET',f'/workflow-runs/{identifier}')
        def wait_run(run,status='awaiting_confirmation'):
            deadline=time.monotonic()+65
            while time.monotonic()<deadline:
                result=detail(run,peer)
                if result['status']==status:
                    return result
                if result['status'] in {'failed','cancelled','succeeded','interrupted'}:
                    raise AssertionError(result)
                time.sleep(.1)
            raise TimeoutError(result)
        def geometry(run):
            return next(step for step in run['steps'] if step['key']=='geometry')
        def confirm(run,client=api,expect=200,**changes):
            step=next(step for step in run['steps'] if step['key']=='confirm_B')
            body={'state':'B','confirmed':True,'expected_revision':step['confirmation_revision'],
                'workflow_step_id':step['id'],'expected_step_version':step['version']}
            body.update(changes)
            return client.call('POST',f'/cases/{run["case_id"]}/caseDict:advance',body,expect=expect)
        def scene(case_id):
            return peer.call('GET',f'/cases/{case_id}/geometry/scene')
        start_peer()
        try:
            case_id=new_case('concurrent registered preview')
            body=request(case_id)
            with ThreadPoolExecutor(max_workers=5) as pool:
                replies=list(pool.map(lambda i:submit(case_id,key='same-request',client=api if i%2 else peer,body=body),range(5)))
            assert len({row['id'] for row in replies})==1
            run=wait_run(replies[0])
            step=geometry(run)
            assert len(step['attempts'])==1 and step['status']=='succeeded'
            attempt=step['attempts'][0]
            assert attempt['handler']['key']=='geometry.preview' and attempt['handler']['schema']=='vibeflow.step-handler.v1'
            assert attempt['handler']['recovery']['automatic_replay'] is False
            assert attempt['handler']['cancellation']['complete_process_tree'] is False
            declared=attempt['handler']['toolchain']
            assert declared['schema']=='vibeflow.handler-toolchain.v1' and declared['scope']=='prepared_handler'
            assert declared['components']['worker_sha256']==hashlib.sha256((SERVER/'app/services/geo_script.py').read_bytes()).hexdigest()
            assert declared['external_dependency_closure']=='not_claimed'
            assert declared['execution_sha256']==attempt['handler']['execution_sha256']
            assert step['publication']['id']==scene(case_id)['publication']['id']
            assert api.call('GET',f'/cases/{case_id}')['workflow_state']=='A'
            assert len(api.call('GET',f'/cases/{case_id}/geometry/publications'))==1
            done('five_cross_API_requests_create_one_registered_attempt_and_published_result_waiting_for_confirmation')

            confirm(run,expect=409,confirmed=False)
            confirm(run,expect=409,expected_revision=run['expected_revision']+99)
            confirm(run,peer)
            finished=wait_run(run,'succeeded')
            assert all(step['status']=='succeeded' for step in finished['steps'])
            assert api.call('GET',f'/cases/{case_id}')['workflow_state']=='B'
            assert submit(case_id,key='same-request',body=body)['id']==run['id']
            assert len(api.call('GET',f'/cases/{case_id}/jobs'))==1
            changed=submit(case_id,key='same-request',body={**body,'parameters':{'geometry_mode':'export'}},expect=409)
            assert changed['detail']['code']=='idempotency_conflict'
            done('explicit_owned_revision_confirmation_completes_workflow_and_idempotent_replay_preserves_history')

            export_id=new_case('registered export')
            exported=wait_run(submit(export_id,'export'))
            job_id=geometry(exported)['attempts'][0]['job_id']
            job=api.wait_job(job_id)
            frozen=job['attempt']['workspace']
            toolchain=json.loads((root_for(export_id)/frozen['run_path']/'A-geo/toolchain.json').read_text())
            assert str(uuid.UUID(toolchain['execution_id']))==toolchain['execution_id']
            assert toolchain['worker_sha256']==hashlib.sha256((SERVER/'app/services/geo_script.py').read_bytes()).hexdigest()
            assert (root_for(export_id)/'A-geo/exports/box.stl').stat().st_size>100
            assert geometry(exported)['handler']=='geometry.export'
            done('export_handler_publishes_real_STEP_STL_with_worker_hash_and_unique_invocation_identity')

            invalid_id=new_case('invalid requests')
            for patch_body,status in [({'command':'echo forged'},422),({'steps':[]},422),
                    ({'parameters':{'geometry_mode':'preview','argv':['cmd']}},422),
                    ({'expected_revision':True},422),({'budget':{'timeout_seconds':90000}},422),
                    ({'target':'not-registered'},422),({'run_dir':'../outside'},400)]:
                submit(invalid_id,body={**request(invalid_id),**patch_body},expect=status)
            assert not api.call('GET',f'/cases/{invalid_id}/workflow-runs')
            assert not api.call('GET',f'/cases/{invalid_id}/jobs')
            done('invalid_schema_unregistered_targets_and_arbitrary_commands_create_no_workflow_or_Job')

            tamper_id=new_case('tampered prepared command')
            (api.root/f'tamper-{tamper_id}').touch()
            rejected=submit(tamper_id,expect=409)
            assert rejected['detail']['code']=='evidence_invalid'
            assert not api.call('GET',f'/cases/{tamper_id}/workflow-runs')
            assert not (root_for(tamper_id)/'.vibeflow/attempts').exists()
            done('registered_queue_rejects_substituted_noop_command_before_sealing_or_Job_creation')

            unbound_id=new_case('lost durable handler binding')
            (api.root/f'unbind-{unbound_id}').touch()
            rejected=submit(unbound_id,expect=409)
            assert rejected['detail']['code']=='evidence_invalid'
            unbound=detail(api.call('GET',f'/cases/{unbound_id}/workflow-runs')[0])
            assert geometry(unbound)['attempts'][0]['phase']=='queued'
            assert not (root_for(unbound_id)/'A-geo/scene.json').exists()
            peer.call('POST',f'/workflow-runs/{unbound["id"]}:cancel',{'expected_version':unbound['version']})
            assert detail(unbound)['status']=='cancelled'
            done('atomic_claim_rejects_missing_registered_binding_without_native_execution_and_allows_cancellation')

            badtools_id=new_case('damaged toolchain provenance')
            (api.root/f'badtoolchain-{badtools_id}').touch()
            rejected=submit(badtools_id,expect=409)
            assert rejected['detail']['code']=='evidence_invalid'
            badtools=detail(api.call('GET',f'/cases/{badtools_id}/workflow-runs')[0])
            assert geometry(badtools)['attempts'][0]['phase']=='queued'
            assert not (root_for(badtools_id)/'A-geo/scene.json').exists()
            peer.call('POST',f'/workflow-runs/{badtools["id"]}:cancel',{'expected_version':badtools['version']})
            done('claim_rejects_changed_declared_toolchain_without_starting_native_worker')

            oldtools_id=new_case('old handler record without toolchain index')
            (api.root/f'oldtoolchain-{oldtools_id}').touch()
            oldtools=wait_run(submit(oldtools_id))
            assert geometry(oldtools)['status']=='succeeded'
            assert 'toolchain' not in geometry(oldtools)['attempts'][0]['handler']
            assert scene(oldtools_id)['publication']['id']==geometry(oldtools)['publication']['id']
            done('legacy_handler_record_remains_readable_and_executable_without_invented_toolchain_index')

            noop_id=new_case('zero exit without worker')
            baseline=wait_run(submit(noop_id))
            old_publication=geometry(baseline)['publication']['id']
            api.call('POST',f'/workflow-runs/{baseline["id"]}:cancel',{'expected_version':baseline['version']})
            (api.root/f'noop-{noop_id}').touch()
            bad=wait_run(submit(noop_id),'failed')
            assert geometry(bad)['attempts'][0]['reason_code']=='publication_failed'
            assert scene(noop_id)['publication']['id']==old_publication
            assert next(step for step in bad['steps'] if step['key']=='confirm_B')['status']=='blocked'
            done('zero_process_exit_with_copied_old_outputs_cannot_forge_this_invocation_or_advance_workflow')

            badworker_id=new_case('worker provenance mismatch')
            (api.root/f'badworker-{badworker_id}').touch()
            badworker=wait_run(submit(badworker_id),'failed')
            assert geometry(badworker)['attempts'][0]['reason_code']=='publication_failed'
            done('worker_fingerprint_mismatch_rejects_professional_publication')

            old=wait_run(submit(export_id))
            newer=wait_run(submit(export_id,'export',client=peer))
            rejected=confirm(old,expect=409)
            assert rejected['detail']['code']=='input_stale'
            stale_step=next(step for step in old['steps'] if step['key']=='confirm_B')
            refresh=peer.call('POST',f'/workflow-runs/{old["id"]}/steps/{stale_step["id"]}:refresh-confirmation',
                {'expected_version':stale_step['version']},expect=409)
            assert refresh['detail']['code']=='input_stale'
            confirm(newer,peer)
            assert detail(newer)['status']=='succeeded' and detail(old)['status']=='awaiting_confirmation'
            done('confirmation_is_bound_to_this_workflow_publication_and_cannot_borrow_a_newer_workflow_result')

            revision_id=new_case('refresh explicit confirmation')
            revision_run=wait_run(submit(revision_id))
            saved=api.call('GET',f'/cases/{revision_id}/caseDict')
            api.call('PUT',f'/cases/{revision_id}/caseDict',{'content':saved['content']+'\n// new bookkeeping revision\n'})
            confirm(revision_run,expect=409)
            confirm_step=next(step for step in revision_run['steps'] if step['key']=='confirm_B')
            refreshed=peer.call('POST',f'/workflow-runs/{revision_run["id"]}/steps/{confirm_step["id"]}:refresh-confirmation',
                {'expected_version':confirm_step['version']})
            assert geometry(refreshed)['publication']['id']==geometry(revision_run)['publication']['id']
            confirm(refreshed,peer)
            done('explicit_confirmation_refresh_accepts_bookkeeping_revision_without_swapping_producer_version')

            deferred_id=new_case('dispatch after committed queue')
            marker=api.root/f'defer-{deferred_id}'
            marker.touch()
            deferred=submit(deferred_id)
            queued=geometry(deferred)['attempts'][0]
            assert queued['phase']=='queued'
            marker.unlink()
            peer.call('POST',f'/workflow-runs/{deferred["id"]}:dispatch',{'expected_version':deferred['version']})
            resumed=wait_run(deferred)
            assert geometry(resumed)['attempts'][0]['id']==queued['id']
            peer.call('POST',f'/workflow-runs/{resumed["id"]}:dispatch',{'expected_version':resumed['version']})
            assert len(api.call('GET',f'/cases/{deferred_id}/jobs'))==1
            done('explicit_dispatch_uses_already_committed_contract_and_claim_prevents_duplicate_execution')

            barrier=api.root/'older-workflow'
            race_id=new_case('late workflow generation',script(1,barrier))
            slow=submit(race_id)
            try:
                wait_file(barrier.with_suffix('.ready'))
                (root_for(race_id)/'A-geo/geometry.py').write_text(script(4),encoding='utf-8')
                newer=submit(race_id,client=peer)
                # Publication validates source files only after all native
                # writers have drained. Holding the older writer until the
                # newer publication commits would deadlock this fixture.
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    current=detail(newer)
                    if geometry(current)['attempts'][0]['phase']=='finalizing':break
                    time.sleep(.05)
                else:raise AssertionError('Newer generation did not reach finalizing')
            finally:
                barrier.with_suffix('.release').touch()
            winner=wait_run(newer)
            obsolete=wait_run(slow,'failed')
            assert geometry(obsolete)['status']=='succeeded'
            assert geometry(obsolete)['publication']['status']=='superseded'
            assert next(step for step in obsolete['steps'] if step['key']=='confirm_B')['error_code']=='input_stale'
            assert scene(race_id)['publication']['id']==geometry(winner)['publication']['id']
            done('late_old_workflow_keeps_valid_historical_compute_but_blocks_confirmation_of_superseded_result')

            barrier=api.root/'cancel-workflow'
            cancel_id=new_case('running cancellation',script(3,barrier))
            cancellable=submit(cancel_id)
            wait_file(barrier.with_suffix('.ready'))
            active=detail(cancellable)
            api.call('POST',f'/workflow-runs/{active["id"]}:cancel',{'expected_version':active['version']+99},expect=409)
            api.call('POST',f'/workflow-runs/{active["id"]}:cancel',{'expected_version':active['version']})
            cancelled=wait_run(active,'cancelled')
            assert geometry(cancelled)['attempts'][0]['phase']=='cancelled'
            assert not any(row['current'] for row in api.call('GET',f'/cases/{cancel_id}/geometry/publications'))
            repeated=peer.call('POST',f'/workflow-runs/{active["id"]}:cancel',{'expected_version':active['version']})
            assert repeated['status']=='cancelled'
            done('durable_workflow_cancellation_stops_local_owned_worker_and_never_publishes_cancelled_output')

            other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
                'password':'workflow-other-123','name':'Other'})['token']
            for method,path,body in [
                ('GET',f'/cases/{case_id}/workflow-runs',None),
                ('GET',f'/workflow-runs/{run["id"]}',None),
                ('POST',f'/workflow-runs/{run["id"]}:dispatch',{'expected_version':finished['version']}),
                ('POST',f'/workflow-runs/{run["id"]}:cancel',{'expected_version':finished['version']}),
                ('POST',f'/cases/{case_id}/workflow-runs',request(case_id)),
            ]:
                api.call(method,path,body,expect=404,extra_headers={'Authorization':'Bearer '+other})
            assert all('claim_token' not in json.dumps(row) for row in detail(run)['steps'])
            done('workflow_list_detail_submission_dispatch_and_cancellation_enforce_case_ownership_without_claim_leakage')

            stop_peer()
            start_peer()
            persisted=detail(run,peer)
            assert persisted['status']=='succeeded'
            assert geometry(persisted)['publication']==geometry(finished)['publication']
            assert geometry(persisted)['attempts'][0]['handler']==attempt['handler']
            done('reader_API_restart_retains_completed_steps_attempts_handler_contract_and_publication_identity')
            result={'passed':True,'count':len(checks),'checks':checks,'api_processes':2,'evidence':str(api.root)}
            (api.root/'handler-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        finally:
            for marker in api.root.glob('*.ready'):
                marker.with_suffix('.release').touch()
            stop_peer()
    return result


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='workflow-handler-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        pg_url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert pg_url.drivername=='postgresql+asyncpg' and pg_url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_handler_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_handler_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=pg_url.host,port=pg_url.port or 5432,user=pg_url.username,
            password=pg_url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created=True
            url=pg_url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,url,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,url)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('WORKFLOW_HANDLER_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':
    asyncio.run(main())
