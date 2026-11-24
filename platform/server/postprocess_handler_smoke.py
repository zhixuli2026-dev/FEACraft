"""Registered real extraction, immutable metrics/plots and E confirmation."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from app.services.casedict import set_block
from cae_benchmarks import thermal_files
from geometry_publication_smoke import script, wait_file
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic

CONTROLLED='''import asyncio,json,sys
from pathlib import Path
root=Path(__file__).parent
original=asyncio.create_subprocess_exec
async def spawn(original,*args,**kwargs):
    if len(args)>1 and Path(str(args[1])).name in {'postprocess_worker.py','report_worker.py','registered_report_worker.py'}:
        kind='post' if Path(args[1]).name=='postprocess_worker.py' else 'report'
        parts=Path(kwargs['cwd']).parts
        case_id=parts[parts.index('cases')+1]
        marker=root/(kind+'-control-'+case_id)
        control=marker.read_text() if marker.exists() else ''
        if control=='noop':
            args=(sys.executable,'-c','pass')
        elif control:
            args=(sys.executable,str(root/'controlled_worker.py'),control,kind,case_id,*args[1:])
    return await original(*args,**kwargs)
from testsupport import intercept_owned_spawn
asyncio.create_subprocess_exec=intercept_owned_spawn(original,spawn)
from app.services.postprocess_handler import PostprocessHandler
prepare=PostprocessHandler.prepare
async def controlled_prepare(self,db,case,user,**kwargs):
    marker=root/('post-control-'+str(case.id))
    if marker.exists() and marker.read_text()=='prepare_failure':
        raise ValueError('Controlled extraction preparation failure')
    return await prepare(self,db,case,user,**kwargs)
PostprocessHandler.prepare=controlled_prepare
from contextlib import asynccontextmanager
from app.services import publication_source_validation
from app.db import SessionLocal
from app.models import Job
original_finish=publication_source_validation.for_finish
@asynccontextmanager
async def held_finish(owner,status,prepared):
    async with SessionLocal() as db:
        job=await db.get(Job,owner.job_id)
        marker=root/('final-post-'+str(job.case_id)) if job and job.type=='postprocess' else None
    if marker is not None and marker.exists():
        marker.with_suffix('.ready').touch()
        while not marker.with_suffix('.release').exists():await asyncio.sleep(.03)
    async with original_finish(owner,status,prepared) as value:
        yield value
publication_source_validation.for_finish=held_finish
from app.routers import postprocess
coordinate=postprocess.coordinate
@asynccontextmanager
async def admission_barrier(db,case,**kwargs):
    marker=root/('admission-'+str(case.id))
    if marker.exists():
        marker.with_suffix('.ready').touch()
        while not marker.with_suffix('.release').exists(): await asyncio.sleep(.03)
    async with coordinate(db,case,**kwargs) as value:
        yield value
postprocess.coordinate=admission_barrier
from app.main import app
'''

WORKER='''import hashlib,json,runpy,sys,time
from pathlib import Path
root=Path(__file__).parent
control,kind,case_id=sys.argv[1:4]
sys.argv=sys.argv[4:]
runpy.run_path(sys.argv[0],run_name='__main__')
if control=='final_failure':raise SystemExit(7)
if control in {'hold','hold_failure'}:
    marker=root/('hold-'+kind+'-'+case_id)
    marker.with_suffix('.ready').touch()
    while not marker.with_suffix('.release').exists(): time.sleep(.03)
    if control=='hold_failure': raise SystemExit(7)
elif control in {'badmetric','badreadiness','badplot'}:
    pointer=json.loads(Path('D-post/data/latest-postprocess.json').read_text(encoding='utf-8'))
    metrics_path=Path(pointer['metrics_path'])
    manifest_path=Path(pointer['manifest_path'])
    metrics=json.loads(metrics_path.read_text(encoding='utf-8'))
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if control=='badmetric': metrics['metrics'][0]['value']+=100
    if control=='badreadiness':
        metrics['readiness']['report_ready']=True
        metrics['readiness']['blockers']=[]
        manifest['readiness']=metrics['readiness']
    if control=='badplot':
        plot=metrics_path.parent/'temperature.png'
        plot.write_bytes(plot.read_bytes()+b'forged plot trailer')
    else: metrics_path.write_text(json.dumps(metrics),encoding='utf-8')
    for record in manifest['artifacts']:
        data=Path(record['path']).read_bytes()
        record.update(size=len(data),sha256=hashlib.sha256(data).hexdigest())
    manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
'''


def verify(database_url=None):
    checks=[]
    def done(name):
        checks.append(name)
        print('PASS '+name,flush=True)
    server=IsolatedServer('postprocess-handler',app='controlled_postprocess:app',database_url=database_url)
    (server.root/'controlled_postprocess.py').write_text(CONTROLLED,encoding='utf-8')
    (server.root/'controlled_worker.py').write_text(WORKER,encoding='utf-8')
    with server as api:
        run_dir='C-run/conduction'
        def new_case(name):
            api.create_case(name)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            saved=api.call('GET',api.prefix+'/caseDict')
            text=set_block(saved['content'],'mesh',{'source':'blockMesh',
                'qualityTargets':{'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
            text=set_block(text,'numericalMethod',{'solver':'laplacianFoam'})
            post={'runDir':run_dir,'fields':{'T':{'unit':'K'}},'dataExtraction':{
                'volumeAverages':{'mean':{'field':'T','time':1}},
                'pointProbes':{'center':{'fields':['T'],'points':[.5,.5,.5],'times':[1]}},
                'lineSamples':{'profile':{'fields':['T'],'start':[.05,.5,.5],'end':[.95,.5,.5],'nPoints':19,'times':[1]}}},
                'derivedQuantities':{'referenceValues':{'analytic_mean':{'metric':'"mean.T@1"','value':350,
                    'unit':'K','kind':'verification','source':'"analytic T(x)=300+100*x"','l2_max':1e-6}},
                    'referenceFields':{'analytic_field':{'field':'T','path':'references/T','time':1,
                        'kind':'verification','source':'"analytic cell-center temperature"','l2_max':1e-6}}},
                'artifacts':{'plots':{'temperature':{'source':'profile','field':'T','time':1,
                    'formats':['pdf','svg','png'],'title':'"Temperature profile"'}}}}
            text=set_block(text,'postProcessing',post)
            api.call('PUT',api.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
            for relative,content in thermal_files().items():
                target=root/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_text(content.replace('(20 2 2)','(20 4 4)'),encoding='utf-8')
            (root/'A-geo/geometry.py').write_text(script(2),encoding='utf-8')
            (root/'references').mkdir()
            values=[300+100*((i%20)+.5)/20 for i in range(320)]
            (root/'references/T').write_text('internalField nonuniform List<scalar>\n320\n(\n'+
                '\n'.join(map(str,values))+'\n);\n',encoding='utf-8')
            return root
        def submit(target='postprocess'):
            revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
            return api.call('POST',api.prefix+'/workflow-runs',{'target':target,'run_dir':run_dir,
                'expected_revision':revision,'budget':{'timeout_seconds':120,'max_log_bytes':1048576}})
        def detail(run):
            return api.call('GET',f'/workflow-runs/{run["id"]}')
        def wait(run,confirmation=None,status=None):
            deadline=time.monotonic()+160
            while time.monotonic()<deadline:
                result=detail(run)
                steps={step['key']:step for step in result['steps']}
                if status and result['status']==status:
                    return result
                if confirmation and steps[confirmation]['status']=='awaiting_confirmation':
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
        def through_solver(flow):
            for key in ('confirm_B','confirm_C'):
                flow=wait(flow,key)
                confirm(flow,key)
            return wait(flow,'confirm_D')
        def post_step(flow):
            return next(row for row in flow['steps'] if row['key']=='postprocess')
        def cancel(flow):
            return api.call('POST',f'/workflow-runs/{flow["id"]}:cancel',{'expected_version':detail(flow)['version']})
        def direct(key=None):
            return api.call('POST',api.prefix+'/postprocess:run',{'run_dir':run_dir},
                extra_headers={'Idempotency-Key':key} if key else None)
        root=new_case('registered postprocess')
        flow=submit()
        solved=through_solver(flow)
        assert next(row for row in solved['steps'] if row['key']=='postprocess')['status']=='pending'
        done('postprocessing_waits_for_explicit_D_confirmation')
        confirm(solved,'confirm_D')
        extracted=wait(flow,'confirm_E')
        step=next(row for row in extracted['steps'] if row['key']=='postprocess')
        job=api.wait_job(step['attempts'][0]['job_id'])
        workspace=job['attempt']['workspace']
        assert workspace['source']['job_id']==job['id']
        assert {row['kind'] for row in workspace['publications']}=={'solve','mesh'}
        assert workspace['files']['references/T']
        result=api.call('GET',api.prefix+'/postprocess/results')
        assert result['publication']['id']==step['publication']['id'] and not result['publication']['stale']
        assert result['metrics']['readiness']['report_ready']
        assert result['metrics']['readiness']['can_claim_verification']
        assert not result['metrics']['readiness']['can_claim_validation']
        assert all(row['status']=='pass' for row in result['metrics']['comparisons'])
        mean=next(row for row in result['metrics']['metrics'] if row['name']=='mean.T@1')
        assert abs(mean['value']-350)<1e-6
        for suffix in ('pdf','svg','png'):
            assert (root/result['result_dir']/('temperature.'+suffix)).stat().st_size>100
        done('real_pinned_heat_extraction_reference_L2_and_deterministic_PDF_SVG_PNG_publish_verified_readiness')
        current_id=result['publication']['id']
        current_metrics=result['metrics']
        for relative in (result['result_dir']+'/metrics.json',result['result_dir']+'/temperature.png',
                'C-run/conduction/1/T','C-run/conduction/constant/polyMesh/points','references/T'):
            path=root/relative
            original=path.read_bytes()
            try:
                path.write_bytes(original+b'\n// changed evidence\n')
                confirm(extracted,'confirm_E',expect=409)
                api.call('POST',api.prefix+'/report:compose',{'overwrite':True},expect=409)
            finally:
                path.write_bytes(original)
        extra=root/'C-run/conduction/2/T'
        extra.parent.mkdir()
        extra.write_bytes((root/'C-run/conduction/1/T').read_bytes())
        confirm(extracted,'confirm_E',expect=409)
        extra.unlink()
        done('E_confirmation_and_report_composition_reject_altered_metrics_plots_mesh_fields_references_and_extra_times')
        immutable=root/'.vibeflow/publications'/current_id/'files'/result['result_dir']/'metrics.json'
        original=immutable.read_bytes()
        try:
            immutable.write_bytes(original+b' ')
            api.call('GET',api.prefix+'/postprocess/results',expect=409)
            confirm(extracted,'confirm_E',expect=409)
        finally:
            immutable.write_bytes(original)
        done('immutable_postprocess_damage_blocks_reading_and_confirmation')
        other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
            'password':'post-other-123','name':'Other'})['token']
        for path in ('/postprocess/results','/postprocess/results?publication_id='+current_id,'/postprocess/publications'):
            api.call('GET',api.prefix+path,expect=404,extra_headers={'Authorization':'Bearer '+other})
        solve_id=next(row for row in solved['steps'] if row['key']=='solve')['publication']['id']
        api.call('GET',api.prefix+'/postprocess/results?publication_id='+solve_id,expect=404)
        done('postprocess_current_history_and_publication_selection_enforce_case_ownership_and_kind')
        confirm(extracted,'confirm_E')
        assert detail(flow)['status']=='succeeded'
        assert api.call('GET',f'/cases/{api.case_id}')['workflow_state']=='E'
        done('registered_postprocessing_finishes_only_after_explicit_E_confirmation')
        with ThreadPoolExecutor(max_workers=3) as pool:
            submitted=list(pool.map(lambda _:direct('same-extraction'),range(3)))
        assert len({item['job_id'] for item in submitted})==1
        api.wait_job(submitted[0]['job_id'])
        current=api.call('GET',api.prefix+'/postprocess/results')
        assert current['job_id']==submitted[0]['job_id']
        historical=api.call('GET',api.prefix+'/postprocess/results?publication_id='+current_id)
        assert historical['metrics']==current_metrics and not historical['publication']['current']
        assert historical['publication']['stale']
        assert api.call('GET',api.prefix+'/postprocess/results?job_id='+str(result['job_id']))['publication']['id']==current_id
        assert direct('same-extraction')['job_id']==submitted[0]['job_id']
        done('existing_postprocess_API_uses_registered_handler_idempotency_and_explicit_immutable_history')

        for mode in ('noop','badmetric','badplot'):
            marker=api.root/f'post-control-{api.case_id}'
            marker.write_text(mode)
            try:
                failed=api.wait_job(direct()['job_id'],expected='failed')
            finally:
                marker.unlink(missing_ok=True)
            assert failed['attempt']['reason_code']=='publication_failed'
            assert api.call('GET',api.prefix+'/postprocess/results')['publication']['id']==current['publication']['id']
            done(mode+'_cannot_publish_even_with_recomputed_output_hashes_or_replace_last_good_postprocess')

        reference=root/'references/T'
        original_reference=reference.read_bytes()
        reference.write_text('internalField uniform 500;\n',encoding='utf-8')
        failed_comparison=direct()
        api.wait_job(failed_comparison['job_id'])
        diagnostics=api.call('GET',api.prefix+'/postprocess/results')
        assert diagnostics['metrics']['readiness']['report_ready'] is False
        assert any(row['status']=='fail' for row in diagnostics['metrics']['comparisons'])
        api.advance('E',expect=409)
        api.call('POST',api.prefix+'/report:compose',{'overwrite':True},expect=409)
        done('valid_extraction_with_failed_reference_is_published_as_diagnostics_but_cannot_advance_or_report')
        marker=api.root/f'post-control-{api.case_id}'
        marker.write_text('badreadiness')
        try:
            failed=api.wait_job(direct()['job_id'],expected='failed')
        finally:
            marker.unlink(missing_ok=True)
        assert failed['attempt']['reason_code']=='publication_failed'
        assert api.call('GET',api.prefix+'/postprocess/results')['publication']['id']==diagnostics['publication']['id']
        done('forged_ready_flag_and_matching_manifest_cannot_override_failed_numeric_reference')
        reference.write_bytes(original_reference)
        api.wait_job(direct()['job_id'])
        api.advance('E')

        api.call('POST',api.prefix+'/report:compose',{'overwrite':True})
        api.wait_job(api.call('POST',api.prefix+'/report:build',{})['job_id'])
        report=root/'E-report/build/report-manifest.json'
        previous_report=report.read_bytes()
        marker=api.root/f'report-control-{api.case_id}'
        barrier=api.root/f'hold-report-{api.case_id}'
        marker.write_text('hold')
        try:
            held_report=api.call('POST',api.prefix+'/report:build',{})
            wait_file(barrier.with_suffix('.ready'))
            current=api.call('GET',api.prefix+'/postprocess/results')
            path=current['result_dir']+'/metrics.json'
            api.put(path,(root/path).read_text(encoding='utf-8'))
            api.call('POST',api.prefix+'/report:compose',{'overwrite':True},expect=409)
        finally:
            marker.unlink(missing_ok=True)
            barrier.with_suffix('.release').touch()
        api.wait_job(held_report['job_id'],expected='failed')
        assert report.read_bytes()==previous_report
        api.wait_job(direct()['job_id'])
        done('canonical_head_invalidation_without_byte_change_blocks_inflight_report_publication_and_allows_explicit_repair')

        root=new_case('pinned solve producer')
        flow=through_solver(submit())
        marker=api.root/f'post-control-{api.case_id}'
        marker.write_text('prepare_failure')
        try:
            response=confirm(flow,'confirm_D')
            assert response['workflow_dispatch']['status']=='pending'
            assert api.call('GET',f'/cases/{api.case_id}')['workflow_state']=='D'
        finally:
            marker.unlink(missing_ok=True)
        api.advance('A')
        replacement=through_solver(submit('solve'))
        confirm(replacement,'confirm_D')
        api.call('POST',f'/workflow-runs/{flow["id"]}:dispatch',
            {'expected_version':detail(flow)['version']},expect=409)
        assert not post_step(detail(flow))['attempts']
        cancel(flow)
        fresh=wait(submit(),'confirm_E')
        confirm(fresh,'confirm_E')
        done('D_confirmation_survives_prepare_failure_retry_cannot_switch_solver_and_new_run_can_select_current_version')

        root=new_case('pinned postprocess confirmation')
        flow=through_solver(submit())
        confirm(flow,'confirm_D')
        old=wait(flow,'confirm_E')
        old_publication=post_step(old)['publication']['id']
        api.wait_job(direct()['job_id'])
        confirm(old,'confirm_E',expect=409)
        old_step=next(row for row in old['steps'] if row['key']=='confirm_E')
        api.call('POST',f'/workflow-runs/{old["id"]}/steps/{old_step["id"]}:refresh-confirmation',
            {'expected_version':old_step['version']},expect=409)
        assert api.call('GET',api.prefix+'/postprocess/results?publication_id='+old_publication)['publication']['stale']
        cancel(old)
        api.advance('E')
        done('E_confirmation_and_refresh_cannot_borrow_another_postprocessing_producer')

        root=new_case('solve published during postprocess admission')
        flow=submit('solve')
        confirm(wait(flow,'confirm_B'),'confirm_B')
        meshed=wait(flow,'confirm_C')
        barrier=api.root/f'admission-{api.case_id}'
        barrier.touch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(direct,'admission-race')
            try:
                wait_file(barrier.with_suffix('.ready'))
                confirm(meshed,'confirm_C')
                solved=wait(flow,'confirm_D')
            finally:
                barrier.unlink(missing_ok=True)
                barrier.with_suffix('.release').touch()
            submitted=pending.result(timeout=30)
        completed=api.wait_job(submitted['job_id'])
        assert completed['attempt']['workspace']['publications']
        current=api.call('GET',api.prefix+'/postprocess/results')
        assert current['publication']['id']==submitted['publication_id']
        assert direct('admission-race')['job_id']==submitted['job_id']
        confirm(solved,'confirm_D')
        done('solve_published_while_admission_waits_cannot_downgrade_postprocessing_to_legacy_execution')

        for scenario in ('reference_change','configuration_change','superseded','cancel','late_failure'):
            root=new_case('held extraction '+scenario)
            flow=through_solver(submit())
            marker=api.root/f'post-control-{api.case_id}'
            barrier=api.root/f'hold-post-{api.case_id}'
            final_marker=None
            if scenario in {'superseded','late_failure'}:
                # Both native writers must drain before either source fence can
                # validate. Hold only the older terminal commit, after its real
                # stop/checkpoint, so the newer producer can finish first.
                final_marker=api.root/f'final-post-{api.case_id}'
                final_marker.touch();barrier=final_marker
                if scenario=='late_failure':marker.write_text('final_failure')
            else:marker.write_text('hold')
            try:
                confirm(flow,'confirm_D')
                wait_file(barrier.with_suffix('.ready'))
                marker.unlink(missing_ok=True)
                if final_marker is not None:final_marker.unlink()
                if scenario=='reference_change':
                    with (root/'references/T').open('a') as handle:
                        handle.write('\n// changed independent reference\n')
                elif scenario=='configuration_change':
                    saved=api.call('GET',api.prefix+'/caseDict')
                    from app.services.casedict import parse_casedict
                    post=parse_casedict(saved['content'])['postProcessing']
                    post['derivedQuantities']['referenceValues']['analytic_mean']['value']=351
                    api.call('PUT',api.prefix+'/caseDict',{'content':set_block(saved['content'],'postProcessing',post),
                        'expected_revision':saved['dict_revision']})
                elif scenario in {'superseded','late_failure'}:
                    winner=wait(submit(),'confirm_E')
                    if scenario=='late_failure':
                        confirm(winner,'confirm_E')
                else:
                    cancel(flow)
            finally:
                marker.unlink(missing_ok=True)
                if final_marker is not None:final_marker.unlink(missing_ok=True)
                barrier.with_suffix('.release').touch()
            terminal=wait(flow,status='cancelled' if scenario=='cancel' else 'failed')
            if scenario in {'reference_change','configuration_change'}:
                assert post_step(terminal)['attempts'][0]['reason_code']=='input_stale'
            elif scenario=='superseded':
                assert post_step(terminal)['status']=='succeeded'
                assert post_step(terminal)['publication']['status']=='superseded'
                assert api.call('GET',api.prefix+'/postprocess/results')['publication']['id']==post_step(winner)['publication']['id']
            elif scenario=='late_failure':
                api.wait_job(post_step(terminal)['attempts'][0]['job_id'],expected='failed')
                assert api.call('GET',f'/cases/{api.case_id}')['workflow_state']=='E'
                fallback=next(event for event in api.call('GET',api.prefix+'/events')['events'] if event['type']=='workflow.fallback')
                assert fallback['regressed'] is False
                assert fallback['superseded_by_publication_id']==post_step(winner)['publication']['id']
            else:
                assert not any(row['current'] for row in api.call('GET',api.prefix+'/postprocess/publications'))
            done('held_real_postprocessing_'+scenario+'_preserves_version_and_confirmation_fences')
        result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
        (api.root/'postprocess-handler-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        return result


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='postprocess-handler-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_postprocess_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_postprocess_[0-9a-f]{32}',name)
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
    print('POSTPROCESS_HANDLER_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':
    asyncio.run(main())
