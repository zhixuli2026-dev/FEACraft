"""Real sealed serial solves, immutable fields and explicit D confirmation."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from app.services.casedict import set_block
from cae_fixtures import cavity_2d, cavity_3d
from cae_benchmarks import thermal_files
from geometry_publication_smoke import script, wait_file
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic

CONTROLLED = '''import asyncio
from pathlib import Path
import re, shlex
from app.services.jobrunner import win_to_wsl_path
root=Path(__file__).parent
original=asyncio.create_subprocess_exec
async def spawn(original,*args,**kwargs):
    if args and args[0]=='wsl.exe' and 'solve_worker.py' in str(args[-1]):
        case_id=re.search(r'/cases/(\\d+)/',args[-1])[1]
        marker=root/('control-'+case_id)
        control=marker.read_text() if marker.exists() else ''
        command=args[-1]
        if control=='noop':
            command='true'
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
from app.services.solve_handler import SolveHandler
prepare=SolveHandler.prepare
async def controlled_prepare(self,db,case,user,**kwargs):
    marker=root/('control-'+str(case.id))
    if marker.exists() and marker.read_text()=='prepare_failure':
        raise ValueError('Controlled transient solver preparation failure')
    return await prepare(self,db,case,user,**kwargs)
SolveHandler.prepare=controlled_prepare
from app.main import app
'''


def verify(database_url=None):
    checks=[]
    def done(name):
        checks.append(name)
        print('PASS '+name,flush=True)
    server=IsolatedServer('solve-handler',app='controlled_solve:app',database_url=database_url)
    (server.root/'controlled_solve.py').write_text(CONTROLLED,encoding='utf-8')
    with server as api:
        def new_case(name,files=None,run_dir='C-run/case-1',binary=False):
            api.create_case(name)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            saved=api.call('GET',api.prefix+'/caseDict')
            text=set_block(saved['content'],'mesh',{'source':'blockMesh',
                'qualityTargets':{'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
            solver='laplacianFoam' if 'conduction' in run_dir else 'icoFoam'
            text=set_block(text,'numericalMethod',{'solver':solver})
            text=set_block(text,'postProcessing',{'runDir':run_dir})
            api.call('PUT',api.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
            for relative,content in (files or cavity_2d()).items():
                target=root/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                if solver=='laplacianFoam' and relative.endswith('/blockMeshDict'):
                    content=content.replace('(20 2 2)','(20 4 4)')
                if binary and relative.endswith('/controlDict'):
                    if binary!='gzip':
                        content=re.sub(r'writeFormat\s+ascii;', 'writeFormat binary;',content)
                    content=re.sub(r'writeCompression\s+off;', 'writeCompression on;',content)
                target.write_text(content,encoding='utf-8')
            (root/'A-geo/geometry.py').write_text(script(2),encoding='utf-8')
            (root/run_dir/'0.orig').mkdir()
            initial=next((root/run_dir/'0').iterdir())
            (root/run_dir/'0.orig'/initial.name).write_bytes(initial.read_bytes())
            return root
        def submit(run_dir='C-run/case-1',target='solve'):
            revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
            return api.call('POST',api.prefix+'/workflow-runs',{'target':target,'run_dir':run_dir,
                'expected_revision':revision,'budget':{'timeout_seconds':120,'max_log_bytes':1048576}})
        def detail(run):
            return api.call('GET',f'/workflow-runs/{run["id"]}')
        def wait(run,confirmation=None,status=None):
            deadline=time.monotonic()+150
            while time.monotonic()<deadline:
                result=detail(run)
                steps={step['key']:step for step in result['steps']}
                if status and result['status']==status:
                    return result
                if confirmation and steps[confirmation]['status']=='awaiting_confirmation':
                    # Workflow state commits before best-effort compatibility
                    # mirroring. Job API waits for local finalization hooks.
                    for producer in result['steps']:
                        if producer['status']=='succeeded' and producer['attempts']:
                            api.wait_job(producer['attempts'][-1]['job_id'])
                    return detail(run)
                if result['status'] in {'failed','interrupted','cancelled'}:
                    raise AssertionError(result)
                time.sleep(.15)
            raise TimeoutError(result)
        def confirm(run,key,expect=200):
            step=next(step for step in run['steps'] if step['key']==key)
            return api.call('POST',api.prefix+'/caseDict:advance',{'state':step['confirmation_state'],
                'confirmed':True,'expected_revision':step['confirmation_revision'],
                'workflow_step_id':step['id'],'expected_step_version':step['version']},expect=expect)
        def solve_step(run):
            return next(step for step in run['steps'] if step['key']=='solve')
        def through_mesh(run):
            flow=wait(run,'confirm_B')
            confirm(flow,'confirm_B')
            return wait(flow,'confirm_C')
        def cancel(run):
            current=detail(run)
            return api.call('POST',f'/workflow-runs/{run["id"]}:cancel',{'expected_version':current['version']})
        root=new_case('registered solve')
        meshed=through_mesh(submit())
        assert solve_step(meshed)['status']=='pending'
        done('solve_waits_for_explicit_C_confirmation')
        confirm(meshed,'confirm_C')
        solved=wait(meshed,'confirm_D')
        step=solve_step(solved)
        job=api.wait_job(step['attempts'][0]['job_id'])
        workspace=job['attempt']['workspace']
        assert workspace['publications'][0]['kind']=='mesh'
        mesh=next(row for row in meshed['steps'] if row['key']=='mesh')['publication']
        assert workspace['publications'][0]['publication_id']==mesh['id']
        sealed=root/workspace['inputs_path']
        work=root/workspace['run_path']
        for initial in ('0','0.orig'):
            for path in (sealed/'C-run/case-1'/initial).iterdir():
                assert path.read_bytes()==(work/path.relative_to(sealed)).read_bytes()
        assert not (sealed/'C-run/case-1/0.5').exists()
        published=api.call('GET',api.prefix+'/solve/results')
        evidence=published['evidence']
        assert evidence['summary']['last_time']==0.5
        assert set(evidence['summary']['fields'])=={'Ux','Uy','p'}
        assert evidence['completion_reason']=='end_time_reached'
        assert published['toolchain']['solver']=='icoFoam'
        assert published['toolchain']['execution_id']==evidence['execution_id']
        assert published['publication']['id']==step['publication']['id'] and not published['publication']['stale']
        done('real_icoFoam_fields_residuals_and_toolchain_publish_with_exact_mesh_lineage_and_preserved_initial_fields')
        assert api.call('GET',f'/cases/{api.case_id}')['workflow_state']=='C'
        for relative in ('C-run/case-1/0.5/U','C-run/case-1/solve.log','C-run/case-1/solve-evidence.json',
                'C-run/case-1/constant/polyMesh/points'):
            path=root/relative
            original=path.read_bytes()
            try:
                path.write_bytes(original+b'\n// changed working copy\n')
                confirm(solved,'confirm_D',expect=409)
            finally:
                path.write_bytes(original)
        extra=root/'C-run/case-1/0.25/U'
        extra.parent.mkdir()
        extra.write_bytes((root/'C-run/case-1/0.5/U').read_bytes())
        confirm(solved,'confirm_D',expect=409)
        extra.unlink()
        done('D_gate_rejects_modified_fields_mesh_logs_proof_and_unpublished_output_times')
        immutable=root/'.vibeflow/publications'/step['publication']['id']/'files/C-run/case-1/0.5/U'
        original=immutable.read_bytes()
        try:
            immutable.write_bytes(original+b'\n// damaged immutable field\n')
            api.call('GET',api.prefix+'/solve/results',expect=409)
            confirm(solved,'confirm_D',expect=409)
        finally:
            immutable.write_bytes(original)
        done('immutable_field_damage_blocks_result_read_and_D_confirmation')
        other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
            'password':'solve-other-123','name':'Other'})['token']
        for path in ('/solve/results','/solve/results?publication_id='+step['publication']['id'],'/solve/publications'):
            api.call('GET',api.prefix+path,expect=404,extra_headers={'Authorization':'Bearer '+other})
        api.call('GET',api.prefix+'/solve/results?publication_id='+mesh['id'],expect=404)
        done('solver_publication_read_enforces_ownership_and_kind')
        confirm(solved,'confirm_D')
        assert detail(solved)['status']=='succeeded'
        assert api.call('GET',f'/cases/{api.case_id}')['workflow_state']=='D'
        done('registered_solve_completes_only_after_explicit_D_confirmation')

        # Recompute inventories after deliberate corruptions so hashes alone
        # cannot satisfy the professional field validator.
        from app.services.solve_artifacts import initial_snapshot, result as solve_result
        from app.services.solve_publication import validate_fields
        field=work/'C-run/case-1/0.5/U'
        original=field.read_bytes()
        for name,content in [('missing',None),('truncated',original[:180]),
                ('wrong_object',re.sub(rb'object\s+U;',b'object wrong;',original,count=1)),
                ('nonfinite',re.sub(rb'(internalField\s+nonuniform\s+List<vector>\s+\d+\s*\(\s*\()[-+\d.eE]+',
                    rb'\g<1>nan',original,count=1)),
                ('nonfinite_boundary',re.sub(rb'(value\s+uniform\s+\()[-+\d.eE]+',rb'\g<1>nan',original,count=1))]:
            assert content!=original,name
            try:
                field.unlink() if content is None else field.write_bytes(content)
                proof=solve_result(work,initial_snapshot(work,'C-run/case-1',evidence['execution_id']))
                try:
                    validate_fields(work,'C-run/case-1',proof)
                except (ValueError,OSError):
                    pass
                else:
                    raise AssertionError('Damaged field accepted: '+name)
            finally:
                field.write_bytes(original)
            done('actual_field_validation_rejects_'+name+'_despite_recomputed_inventory')
        log=work/'C-run/case-1/solve.log'
        original_log=log.read_bytes()
        try:
            log.write_bytes(re.sub(rb'Initial residual = [-+\d.eE]+',b'Initial residual = nan',original_log,count=1))
            try:
                solve_result(work,initial_snapshot(work,'C-run/case-1',evidence['execution_id']))
            except ValueError:
                pass
            else:
                raise AssertionError('Non-finite residual accepted')
        finally:
            log.write_bytes(original_log)
        done('nonfinite_residual_cannot_disappear_from_solver_evidence')

        from app.services.solve_artifacts import configuration
        control=work/'C-run/case-1/system/controlDict'
        original_control=control.read_bytes()
        for pattern,replacement in [(rb'startFrom\s+startTime;',b'startFrom latestTime;'),
                (rb'startTime\s+0;',b'startTime 0.2;'),(rb'stopAt\s+endTime;',b'stopAt writeNow;'),
                (rb'deltaT\s+0.005;',b'deltaT nan;'),(rb'endTime\s+0.5;',b''),
                (rb'application\s+icoFoam;',b'application simpleFoam;')]:
            try:
                edited_control=re.sub(pattern,replacement,original_control)
                assert edited_control!=original_control
                control.write_bytes(edited_control)
                try:
                    configuration(work,'C-run/case-1')
                except ValueError:
                    pass
                else:
                    raise AssertionError('Unsupported fresh solve configuration accepted')
            finally:
                control.write_bytes(original_control)
        done('fresh_solve_rejects_resume_modes_missing_nonfinite_controls_and_solver_mismatch')

        terminal={path:path.read_bytes() for path in (work/'C-run/case-1/0.5').rglob('*') if path.is_file()}
        try:
            for path in terminal:
                path.unlink()
            try:
                solve_result(work,initial_snapshot(work,'C-run/case-1',evidence['execution_id']))
            except ValueError:
                pass
            else:
                raise AssertionError('Missing terminal output accepted')
        finally:
            for path,content in terminal.items():
                path.write_bytes(content)
        done('solver_end_marker_without_last_time_fields_cannot_prove_completion')

        api.advance('A')
        control=root/'C-run/case-1/system/controlDict'
        control.write_text(re.sub(r'writeInterval\s+20;', 'writeInterval 100;',control.read_text()),encoding='utf-8')
        edited=root/'C-run/case-1/0.1/U'
        edited.write_bytes(edited.read_bytes()+b'\n// user-owned edit\n')
        preserved=edited.read_bytes()
        flow=through_mesh(submit())
        confirm(flow,'confirm_C')
        replaced=wait(flow,'confirm_D')
        assert not (root/'C-run/case-1/0.2/U').exists()
        assert edited.read_bytes()==preserved
        confirm(replaced,'confirm_D',expect=409)
        edited.unlink()  # Explicit fixture cleanup of its own test edit.
        confirm(replaced,'confirm_D')
        historical=api.call('GET',api.prefix+'/solve/results?publication_id='+step['publication']['id'])
        assert not historical['publication']['current'] and historical['publication']['stale']
        assert historical['evidence']==evidence
        done('repeat_solve_retires_only_unchanged_old_times_preserves_edits_and_keeps_immutable_history')

        for name,files,run_dir,binary in [('cavity3d',cavity_3d(),'C-run/case-3d',False),
                ('conduction',thermal_files(),'C-run/conduction',False),
                ('binary',cavity_2d(),'C-run/case-1',True),
                ('ascii_gzip',cavity_2d(),'C-run/case-1','gzip')]:
            root=new_case(name,files,run_dir,binary)
            flow=through_mesh(submit(run_dir))
            confirm(flow,'confirm_C')
            solved=wait(flow,'confirm_D')
            published=api.call('GET',api.prefix+'/solve/results')
            assert published['evidence']['outputs'] and not published['publication']['stale']
            if binary=='gzip':
                assert any(row['path'].endswith('.gz') for row in published['evidence']['outputs'])
                mesh_result=api.call('GET',api.prefix+'/mesh/results')
                assert set(mesh_result['toolchain']['normalized_gzip_mesh_files'])=={'points','faces','owner','neighbour'}
                assert (root/run_dir/'constant/polyMesh/boundary').is_file()
            elif binary:
                from app.services.solve_artifacts import field_header
                assert field_header(root/run_dir/'0.5/U')['format']=='binary'
            if name=='conduction':
                import numpy as np
                import vtk
                from vtk.util.numpy_support import vtk_to_numpy
                reader=vtk.vtkOpenFOAMReader()
                reader.SetFileName(str(root/run_dir/'conduction.foam'))
                reader.CreateCellToPointOff()
                reader.UpdateInformation()
                reader.EnableAllCellArrays()
                reader.UpdateTimeStep(1)
                grid=reader.GetOutput().GetBlock(0)
                centers=vtk.vtkCellCenters()
                centers.SetInputData(grid)
                centers.Update()
                x=vtk_to_numpy(centers.GetOutput().GetPoints().GetData())[:,0]
                temperature=vtk_to_numpy(grid.GetCellData().GetArray('T'))
                assert np.sqrt(np.mean((temperature-(300+100*x))**2))<1e-4
            confirm(solved,'confirm_D')
            done('real_'+name+'_registered_solve_and_field_read_validation')

        previous=published['publication']['id']
        api.advance('A')
        flow=through_mesh(submit())
        marker=api.root/f'control-{api.case_id}'
        marker.write_text('noop')
        try:
            confirm(flow,'confirm_C')
            failed=wait(flow,status='failed')
        finally:
            marker.unlink(missing_ok=True)
        assert solve_step(failed)['attempts'][0]['reason_code']=='publication_failed'
        assert api.call('GET',api.prefix+'/solve/results')['publication']['id']==previous
        assert api.call('GET',api.prefix+'/solve/results')['publication']['stale']
        done('noop_cannot_publish_or_replace_last_solve_and_new_mesh_invalidates_old_solver_lineage')

        root=new_case('pinned mesh producer')
        flow=through_mesh(submit())
        marker=api.root/f'control-{api.case_id}'
        marker.write_text('prepare_failure')
        try:
            response=confirm(flow,'confirm_C')
            assert response['workflow_dispatch']['status']=='pending'
            assert api.call('GET',f'/cases/{api.case_id}')['workflow_state']=='C'
        finally:
            marker.unlink(missing_ok=True)
        api.advance('A')
        replacement=through_mesh(submit(target='mesh'))
        confirm(replacement,'confirm_C')
        api.call('POST',f'/workflow-runs/{flow["id"]}:dispatch',
            {'expected_version':detail(flow)['version']},expect=409)
        assert not solve_step(detail(flow))['attempts']
        cancel(flow)
        done('confirmed_C_survives_prepare_failure_but_retry_cannot_borrow_another_mesh_producer')

        for scenario in ('source_change','superseded','cancel','late_failure'):
            root=new_case('held solve '+scenario)
            flow=through_mesh(submit())
            marker=api.root/f'control-{api.case_id}'
            barrier=api.root/f'hold-{api.case_id}'
            marker.write_text('hold_failure' if scenario=='late_failure' else 'hold')
            finish_barrier=None
            try:
                confirm(flow,'confirm_C')
                wait_file(barrier.with_suffix('.ready'))
                marker.unlink()
                if scenario=='source_change':
                    with (root/'C-run/case-1/0/p').open('a') as handle:
                        handle.write('\n// newer initial field\n')
                elif scenario in {'superseded','late_failure'}:
                    winner=submit()
                    deadline=time.monotonic()+30
                    while solve_step(detail(winner))['status']!='finalizing':
                        assert time.monotonic()<deadline,'Newer solve did not reach finalizing'
                        time.sleep(.05)
                    if scenario=='late_failure':
                        old_job=solve_step(detail(flow))['attempts'][0]['job_id']
                        finish_barrier=api.root/f'finish-{old_job}'
                        finish_barrier.touch()
                    barrier.with_suffix('.release').touch()
                    if finish_barrier is not None:wait_file(finish_barrier.with_suffix('.ready'))
                    winner=wait(winner,'confirm_D')
                    if scenario=='late_failure':
                        confirm(winner,'confirm_D')
                else:
                    cancel(flow)
            finally:
                marker.unlink(missing_ok=True)
                barrier.with_suffix('.release').touch()
                if finish_barrier is not None:finish_barrier.with_suffix('.release').touch()
            terminal=wait(flow,status='cancelled' if scenario=='cancel' else 'failed')
            if scenario=='source_change':
                assert solve_step(terminal)['attempts'][0]['reason_code']=='input_stale'
            elif scenario=='superseded':
                assert solve_step(terminal)['status']=='succeeded'
                assert solve_step(terminal)['publication']['status']=='superseded'
                assert api.call('GET',api.prefix+'/solve/results')['publication']['id']==solve_step(winner)['publication']['id']
            elif scenario=='late_failure':
                api.wait_job(solve_step(terminal)['attempts'][0]['job_id'],expected='failed')
                assert api.call('GET',f'/cases/{api.case_id}')['workflow_state']=='D'
                fallback=next(event for event in api.call('GET',api.prefix+'/events')['events'] if event['type']=='workflow.fallback')
                assert fallback['regressed'] is False
                assert fallback['superseded_by_publication_id']==solve_step(winner)['publication']['id']
            else:
                assert not any(row['current'] for row in api.call('GET',api.prefix+'/solve/publications'))
            done('held_real_solve_'+scenario+'_preserves_publication_and_confirmation_fences')
        result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
        (api.root/'solve-handler-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        return result


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='solve-handler-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_solve_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_solve_[0-9a-f]{32}',name)
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
    print('SOLVE_HANDLER_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':
    asyncio.run(main())
