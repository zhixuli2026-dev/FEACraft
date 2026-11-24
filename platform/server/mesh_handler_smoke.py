"""Real registered geometry -> mesh -> C confirmation in disposable APIs."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from app.services.casedict import set_block
from cae_fixtures import cavity_2d
from geometry_publication_smoke import script, wait_file
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic

CONTROLLED='''import asyncio
from pathlib import Path
import re, shlex
from app.services.jobrunner import win_to_wsl_path
root=Path(__file__).parent
original=asyncio.create_subprocess_exec
async def spawn(original,*args,**kwargs):
    if args and args[0]=='wsl.exe' and 'mesh_worker.py' in str(args[-1]):
        match=re.search(r'/cases/(\\d+)/',args[-1])
        case_id=match[1]
        marker=root/('control-'+case_id)
        control=marker.read_text() if marker.exists() else ''
        command=args[-1]
        if control=='noop':
            command='true'
        elif control=='badlog':
            command=command.replace('&& exec python3','&& python3')+" && printf 'Mesh OK.\\n' > C-run/case-1/checkMesh.log"
        elif control=='badmesh':
            command=command.replace('&& exec python3','&& python3')+" && printf '\\n// altered mesh\\n' >> C-run/case-1/constant/polyMesh/points"
        elif control in {'hold','hold_failure'}:
            ready=shlex.quote(win_to_wsl_path(root/('hold-'+case_id+'.ready')))
            release=shlex.quote(win_to_wsl_path(root/('hold-'+case_id+'.release')))
            command=command.replace('&& exec python3','&& python3')+' && touch '+ready+' && while [ ! -f '+release+' ]; do sleep 0.05; done'
            if control=='hold_failure':
                command+='; exit 7'
        args=(*args[:-1],command)
    return await original(*args,**kwargs)
from testsupport import intercept_owned_spawn
asyncio.create_subprocess_exec=intercept_owned_spawn(original,spawn)
from app.services import job_write_checkpoint
original_drained=job_write_checkpoint.record_drained
async def drained(owner,*args,**kwargs):
    result=await original_drained(owner,*args,**kwargs)
    barrier=root/('finish-'+str(owner.job_id))
    if result and barrier.exists():
        barrier.with_suffix('.ready').touch()
        while not barrier.with_suffix('.release').exists():
            await asyncio.sleep(.05)
    return result
job_write_checkpoint.record_drained=drained
from app.main import app
'''


def verify(database_url=None):
    checks=[]
    def done(name):
        checks.append(name)
        print('PASS '+name,flush=True)
    server=IsolatedServer('mesh-handler',app='controlled_mesh:app',database_url=database_url)
    (server.root/'controlled_mesh.py').write_text(CONTROLLED,encoding='utf-8')
    with server as api:
        def new_case(name,targets=None):
            api.create_case(name)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            saved=api.call('GET',api.prefix+'/caseDict')
            text=set_block(saved['content'],'mesh',{'source':'blockMesh',
                'qualityTargets':targets or {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
            api.call('PUT',api.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
            for relative,content in cavity_2d().items():
                target=root/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_text(content,encoding='utf-8')
            (root/'A-geo/geometry.py').write_text(script(2),encoding='utf-8')
            (root/'C-run/case-1/0.orig').mkdir()
            (root/'C-run/case-1/0.orig/p').write_bytes((root/'C-run/case-1/0/p').read_bytes())
            return root
        root=new_case('registered mesh')
        def submit(key=None):
            revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
            return api.call('POST',api.prefix+'/workflow-runs',{'target':'mesh','expected_revision':revision,
                'budget':{'timeout_seconds':120,'max_log_bytes':1048576}},
                extra_headers={'Idempotency-Key':key} if key else None)
        def detail(run):
            return api.call('GET',f'/workflow-runs/{run["id"]}')
        def wait(run,confirmation=None,status=None):
            deadline=time.monotonic()+140
            while time.monotonic()<deadline:
                result=detail(run)
                steps={step['key']:step for step in result['steps']}
                if status and result['status']==status:
                    return result
                if confirmation and steps[confirmation]['status']=='awaiting_confirmation':
                    return result
                if result['status'] in {'failed','interrupted','cancelled'}:
                    raise AssertionError(result)
                time.sleep(.15)
            raise TimeoutError(result)
        def confirm(run,key,expect=200,**overrides):
            step=next(step for step in run['steps'] if step['key']==key)
            body={'state':step['confirmation_state'],'confirmed':True,
                'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],
                'expected_step_version':step['version']}
            body.update(overrides)
            return api.call('POST',api.prefix+'/caseDict:advance',body,expect=expect)
        def mesh_step(run):
            return next(step for step in run['steps'] if step['key']=='mesh')
        def cancel(run):
            actual=detail(run)
            return api.call('POST',f'/workflow-runs/{run["id"]}:cancel',{'expected_version':actual['version']})
        with ThreadPoolExecutor(max_workers=3) as pool:
            requests=list(pool.map(lambda _:submit('same-mesh'),range(3)))
        assert len({item['id'] for item in requests})==1
        run=wait(requests[0],'confirm_B')
        assert next(step for step in run['steps'] if step['key']=='mesh')['status']=='pending'
        done('mesh_never_starts_before_explicit_B_confirmation')
        response=confirm(run,'confirm_B')
        assert response['workflow_dispatch']['status']=='dispatched',response
        meshed=wait(run,'confirm_C')
        step=next(step for step in meshed['steps'] if step['key']=='mesh')
        attempt=step['attempts'][0]
        job=api.wait_job(attempt['job_id'])
        assert attempt['handler']['key']=='mesh.generate'
        assert step['publication']['kind']=='mesh' and step['publication']['status']=='committed'
        workspace=job['attempt']['workspace']
        assert workspace['publications'][0]['kind']=='geometry'
        assert not any('/polyMesh/' in path for path in workspace['files'])
        sealed=root/workspace['inputs_path']
        work=root/workspace['run_path']
        assert (sealed/'C-run/case-1/0.orig/p').read_bytes()==(work/'C-run/case-1/0.orig/p').read_bytes()
        quality=json.loads((work/'B-mesh/quality.json').read_text())
        assert quality['mesh_ok'] and quality['status']=='pass'
        boundaries=json.loads((work/'B-mesh/boundaries.json').read_text())
        assert {patch['name'] for patch in boundaries['patches']}=={'movingWall','fixedWalls','frontAndBack'}
        toolchain=json.loads((work/'B-mesh/toolchain.json').read_text())
        assert toolchain['openfoam']=='v2406' or toolchain['openfoam']=='2406'
        assert toolchain['executable_sha256']['blockMesh'] and toolchain['execution_id']==quality['execution_id']
        assert (work/'C-run/case-1/checkMesh.log').is_file()
        done('real_blockMesh_checkMesh_publish_quality_boundary_and_toolchain_with_frozen_geometry_lineage_and_initial_fields')
        published=api.call('GET',api.prefix+'/mesh/results')
        assert published['quality']==quality and published['boundaries']==boundaries
        assert published['publication']['id']==step['publication']['id'] and not published['publication']['stale']
        done('current_mesh_read_returns_verified_immutable_quality_boundaries_and_provenance')
        assert api.call('GET',f'/cases/{api.case_id}')['workflow_state']=='B'
        for relative in ('C-run/case-1/constant/polyMesh/points','C-run/case-1/checkMesh.log','B-mesh/quality.json'):
            path=root/relative
            original=path.read_bytes()
            try:
                path.write_bytes(original+b'\n// changed working copy\n')
                confirm(meshed,'confirm_C',expect=409)
            finally:
                path.write_bytes(original)
        done('raw_mesh_log_or_quality_alias_changes_cannot_pass_C_confirmation')
        immutable=root/'.vibeflow/publications'/step['publication']['id']/'files/C-run/case-1/constant/polyMesh/boundary'
        original=immutable.read_bytes()
        try:
            immutable.write_bytes(original+b'\n// damaged published boundary\n')
            api.call('GET',api.prefix+'/mesh/results',expect=409)
            confirm(meshed,'confirm_C',expect=409)
        finally:
            immutable.write_bytes(original)
        done('damaged_immutable_mesh_bundle_is_rejected_by_result_reader_and_C_gate')
        other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
            'password':'mesh-other-123','name':'Other'})['token']
        for path in ('/mesh/results','/mesh/results?publication_id='+step['publication']['id'],'/mesh/publications'):
            api.call('GET',api.prefix+path,expect=404,extra_headers={'Authorization':'Bearer '+other})
        geometry_id=workspace['publications'][0]['publication_id']
        api.call('GET',api.prefix+'/mesh/results?publication_id='+geometry_id,expect=404)
        done('mesh_result_history_and_publication_selection_enforce_case_ownership_and_artifact_kind')
        confirm(meshed,'confirm_C')
        assert detail(run)['status']=='succeeded'
        assert api.call('GET',f'/cases/{api.case_id}')['workflow_state']=='C'
        done('successful_mesh_requires_explicit_C_confirmation_and_completes_registered_workflow')

        for policy,reason,success in [('fail',None,False),('warn',None,True),
                ('allowWithReason',None,False),('allowWithReason','measured degradation accepted',True)]:
            targets={'maxAspectRatio':0.1,'policy':policy}
            if reason:
                targets['reason']=reason
            root=new_case('quality-'+policy,targets)
            flow=wait(submit(),'confirm_B')
            confirm(flow,'confirm_B')
            terminal=wait(flow,'confirm_C' if success else None,status=None if success else 'failed')
            if success:
                assert api.call('GET',api.prefix+'/mesh/results')['quality']['status']=='fail'
                confirm(terminal,'confirm_C')
            else:
                assert mesh_step(terminal)['status']=='failed'
                assert not any(row['current'] for row in api.call('GET',api.prefix+'/mesh/publications'))
            done('measured_quality_policy_'+policy+('_with_reason' if reason else '_without_reason'))

        root=new_case('missing Jacobian',{'minJacobian':0.001,'policy':'warn'})
        flow=wait(submit(),'confirm_B')
        confirm(flow,'confirm_B')
        failure=wait(flow,status='failed')
        assert mesh_step(failure)['status']=='failed'
        done('missing_configured_metric_fails_even_under_warn_policy')

        root=new_case('pinned confirmed geometry')
        flow=wait(submit(),'confirm_B')
        control=root/'C-run/case-1/system/controlDict'
        saved_control=control.read_bytes()
        control.unlink()
        response=confirm(flow,'confirm_B')
        assert response['workflow_dispatch']['status']=='pending'
        control.write_bytes(saved_control)
        preview=api.call('POST',api.prefix+'/geometry:preview')
        api.wait_job(preview['job_id'])
        refused=api.call('POST',f'/workflow-runs/{flow["id"]}:dispatch',
            {'expected_version':detail(flow)['version']},expect=409)
        assert refused['detail']['code']=='input_stale'
        assert not mesh_step(detail(flow))['attempts']
        replacement=wait(submit(),'confirm_C')
        job=api.wait_job(mesh_step(replacement)['attempts'][0]['job_id'])
        assert job['attempt']['workspace']['publications'][0]['publication_id']==preview['publication_id']
        done('post_confirmation_dispatch_cannot_switch_geometry_producer_but_explicit_new_workflow_can')

        root=new_case('dispatch admission failure')
        flow=wait(submit(),'confirm_B')
        control=root/'C-run/case-1/system/controlDict'
        saved_control=control.read_bytes()
        control.unlink()
        response=confirm(flow,'confirm_B')
        assert response['workflow_state']=='B' and response['workflow_dispatch']['status']=='pending'
        assert mesh_step(detail(flow))['status']=='ready'
        control.write_bytes(saved_control)
        api.call('POST',f'/workflow-runs/{flow["id"]}:dispatch',{'expected_version':detail(flow)['version']})
        waiting=wait(flow,'confirm_C')
        done('post_confirmation_admission_failure_keeps_B_commit_and_ready_step_for_explicit_dispatch')

        old_id=mesh_step(waiting)['publication']['id']
        for fault in ('noop','badlog','badmesh'):
            marker=api.root/f'control-{api.case_id}'
            marker.write_text(fault)
            try:
                failed=wait(submit(),status='failed')
            finally:
                marker.unlink()
            assert mesh_step(failed)['attempts'][0]['reason_code']=='publication_failed'
            assert api.call('GET',api.prefix+'/mesh/results')['publication']['id']==old_id
            done(fault+'_cannot_publish_or_replace_previous_valid_mesh')

        newer=wait(submit(),'confirm_C')
        rejected=confirm(waiting,'confirm_C',expect=409)
        assert rejected['detail']['code']=='input_stale'
        old=api.call('GET',api.prefix+'/mesh/results?publication_id='+old_id)
        assert not old['publication']['current'] and old['quality']['mesh_ok']
        done('C_confirmation_cannot_borrow_newer_workflow_mesh_and_old_publication_remains_readable')

        # A new geometry version invalidates mesh through its pinned upstream
        # dependency even when geometry source bytes have not changed.
        preview=api.call('POST',api.prefix+'/geometry:preview')
        api.wait_job(preview['job_id'])
        assert api.call('GET',api.prefix+'/mesh/results')['publication']['stale']
        assert confirm(newer,'confirm_C',expect=409)['detail']['code']=='input_stale'
        mesh_id=mesh_step(newer)['publication']['id']
        body={'command':'echo pinned-mesh','runtime':'windows','type':'shell',
            'input_publications':[{'publication_id':mesh_id,'paths':['C-run/case-1/constant/polyMesh/points']}]}
        api.call('POST',api.prefix+'/jobs',body,expect=409)
        body['input_publications'][0]['require_current']=False
        api.wait_job(api.call('POST',api.prefix+'/jobs',body)['id'])
        done('upstream_geometry_head_change_invalidates_current_mesh_dependencies_but_explicit_historical_consumption_works')

        for scenario in ('source_change','superseded','cancel','late_failure'):
            root=new_case('held mesh '+scenario)
            flow=wait(submit(),'confirm_B')
            marker=api.root/f'control-{api.case_id}'
            barrier=api.root/f'hold-{api.case_id}'
            marker.write_text('hold_failure' if scenario=='late_failure' else 'hold')
            finish_barrier=None
            try:
                confirm(flow,'confirm_B')
                wait_file(barrier.with_suffix('.ready'))
                marker.unlink()
                if scenario=='source_change':
                    with (root/'C-run/case-1/system/blockMeshDict').open('a') as handle:
                        handle.write('\n// newer input\n')
                elif scenario in {'superseded','late_failure'}:
                    winner=submit()
                    deadline=time.monotonic()+30
                    while mesh_step(detail(winner))['status']!='finalizing':
                        assert time.monotonic()<deadline,'Newer mesh did not reach finalizing'
                        time.sleep(.05)
                    # Source validation correctly waits for every native writer
                    # to drain; waiting for publication before release deadlocks.
                    if scenario=='late_failure':
                        old_job=mesh_step(detail(flow))['attempts'][0]['job_id']
                        finish_barrier=api.root/f'finish-{old_job}'
                        finish_barrier.touch()
                    barrier.with_suffix('.release').touch()
                    if finish_barrier is not None:wait_file(finish_barrier.with_suffix('.ready'))
                    winner=wait(winner,'confirm_C')
                    if scenario=='late_failure':
                        confirm(winner,'confirm_C')
                else:
                    cancel(flow)
            finally:
                marker.unlink(missing_ok=True)
                barrier.with_suffix('.release').touch()
                if finish_barrier is not None:finish_barrier.with_suffix('.release').touch()
            terminal=wait(flow,status='cancelled' if scenario=='cancel' else 'failed')
            if scenario=='source_change':
                assert mesh_step(terminal)['attempts'][0]['reason_code']=='input_stale'
            elif scenario=='superseded':
                assert mesh_step(terminal)['status']=='succeeded'
                assert mesh_step(terminal)['publication']['status']=='superseded'
                assert api.call('GET',api.prefix+'/mesh/results')['publication']['id']==mesh_step(winner)['publication']['id']
            elif scenario=='late_failure':
                api.wait_job(mesh_step(terminal)['attempts'][0]['job_id'],expected='failed')
                assert api.call('GET',f'/cases/{api.case_id}')['workflow_state']=='C'
                fallback=next(event for event in api.call('GET',api.prefix+'/events')['events'] if event['type']=='workflow.fallback')
                assert fallback['regressed'] is False
                assert fallback['superseded_by_publication_id']==mesh_step(winner)['publication']['id']
            else:
                assert not any(row['current'] for row in api.call('GET',api.prefix+'/mesh/publications'))
            done('held_real_mesh_'+scenario+'_preserves_publication_and_confirmation_fences')
        result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
        (api.root/'mesh-handler-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        return result


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='mesh-handler-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_mesh_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_mesh_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,
            password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('MESH_HANDLER_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':
    asyncio.run(main())
