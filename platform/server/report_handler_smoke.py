"""Real registered report composition, native output proof and durable publication."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

from sqlalchemy.engine import make_url
from app.services.casedict import set_block
from cae_benchmarks import thermal_files
from geometry_publication_smoke import script, wait_file
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic
from postprocess_handler_smoke import CONTROLLED, WORKER

CONTROLLED+='''
from app.services.report_handler import ReportHandler
report_prepare=ReportHandler.prepare
async def controlled_report_prepare(self,db,case,user,**kwargs):
    marker=root/('report-control-'+str(case.id))
    if marker.exists() and marker.read_text()=='prepare_failure':
        raise ValueError('Controlled report preparation failure')
    return await report_prepare(self,db,case,user,**kwargs)
ReportHandler.prepare=controlled_report_prepare
from app.routers import reports
report_coordinate=reports.coordinate
@asynccontextmanager
async def report_admission_barrier(db,case,**kwargs):
    marker=root/('report-admission-'+str(case.id))
    if marker.exists():
        marker.with_suffix('.ready').touch()
        while not marker.with_suffix('.release').exists(): await asyncio.sleep(.03)
    async with report_coordinate(db,case,**kwargs) as value: yield value
reports.coordinate=report_admission_barrier
from app.services import native_request_execution as request_execution,native_request_records as request_records
request_command=request_execution.RequestExecutor._command
def held_request(self,purpose,parameters):
    argv,timeout,limit=request_command(self,purpose,parameters)
    case_id=int(self.cwd.name)
    if purpose=='pandoc_parse' and (root/('hold-preparation-'+str(case_id))).exists():
        from app.services import pandoc_execution
        (root/('preparing-'+str(case_id)+'.json')).write_text(json.dumps({'id':self.owner.id}))
        argv=[argv[0],'-I','-S','-u',str(root/'held_request_pandoc.py'),str(root),str(case_id),self.owner.id,
              str(pandoc_execution.NATIVE),parameters['config'],parameters['sha256']]
    return argv,timeout,limit
request_execution.RequestExecutor._command=held_request
request_complete=ReportHandler.complete_preparation
async def preparation_completed(self,prepared,case_id,user_id):
    try:return await request_complete(self,prepared,case_id,user_id)
    finally:
        marker=root/('preparing-'+str(case_id)+'.json')
        if marker.exists():
            from app.db import SessionLocal
            from app.models import NativeRequest
            async with SessionLocal() as db:
                row=await db.get(NativeRequest,json.loads(marker.read_text())['id'])
                marker.with_suffix('.result').write_text(json.dumps(request_records.public(row)))
ReportHandler.complete_preparation=preparation_completed
'''
WORKER+='''
elif control in {'badhtml','baddocx','badsource','badtoolchain'}:
    import zipfile
    manifest_path=Path('E-report/build/report-manifest.json')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if control=='badhtml':
        path=Path('E-report/build/report.html')
        path.write_bytes(path.read_bytes().replace(b'</body>',b'<p>forged report</p></body>'))
    elif control=='baddocx':
        path=Path('E-report/build/report.docx')
        with zipfile.ZipFile(path) as archive: records={name:archive.read(name) for name in archive.namelist()}
        records['word/document.xml']=records['word/document.xml'].replace(b'</w:body>',b'<w:p><w:r><w:t>forged report</w:t></w:r></w:p></w:body>')
        with zipfile.ZipFile(path,'w') as archive:
            for name,data in records.items(): archive.writestr(name,data)
    elif control=='badsource':
        path=Path(manifest['source_path'])
        path.write_bytes(path.read_bytes()+b'\\nForged generated source')
    else:
        path=Path('E-report/build/report-toolchain.json')
        value=json.loads(path.read_text(encoding='utf-8'))
        value['execution_id']='00000000-0000-0000-0000-000000000000'
        path.write_text(json.dumps(value),encoding='utf-8')
    for name,record in manifest['artifacts'].items():
        data=Path(name).read_bytes()
        record.update(size=len(data),sha256=hashlib.sha256(data).hexdigest())
    manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
'''


def verify(database_url=None):
    checks=[]
    def done(name):
        checks.append(name)
        print('PASS '+name,flush=True)
    server=IsolatedServer('report-handler',app='controlled_postprocess:app',database_url=database_url)
    (server.root/'controlled_postprocess.py').write_text(CONTROLLED,encoding='utf-8')
    (server.root/'controlled_worker.py').write_text(WORKER,encoding='utf-8')
    from document_admission_smoke import PANDOC
    (server.root/'held_request_pandoc.py').write_text(PANDOC,encoding='utf-8')
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
        def submit(target='report',*,report_mode=None):
            revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
            return api.call('POST',api.prefix+'/workflow-runs',{'target':target,'run_dir':run_dir,
                'expected_revision':revision,'budget':{'timeout_seconds':120,'max_log_bytes':1048576},
                'parameters':{'report_mode':report_mode} if report_mode else {}})
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
            return api.call('POST',api.prefix+'/report:build',{},
                extra_headers={'Idempotency-Key':key} if key else None)
        root=new_case('registered report')
        user_draft=(root/'E-report/report.md').read_bytes()
        flow=through_solver(submit())
        confirm(flow,'confirm_D')
        waiting=wait(flow,'confirm_E')
        assert next(s for s in waiting['steps'] if s['key']=='report')['status']=='pending'
        done('report_waits_for_explicit_E_confirmation')
        confirm(waiting,'confirm_E')
        finished=wait(flow,status='succeeded')
        report_step=next(s for s in finished['steps'] if s['key']=='report')
        job=api.wait_job(report_step['attempts'][0]['job_id'])
        assert job['attempt']['workspace']['publications']
        result=api.call('GET',api.prefix+'/report/results')
        assert result['publication']['id']==report_step['publication']['id']
        assert result['manifest']['mode']=='compose' and not result['publication']['stale']
        assert (root/'E-report/report.md').read_bytes()==user_draft
        assert 'verification' in (root/result['manifest']['source_path']).read_text(encoding='utf-8')
        assert b'<html' in (root/'E-report/build/report.html').read_bytes()[:4096].lower()
        with zipfile.ZipFile(root/'E-report/build/report.docx') as archive:
            import xml.etree.ElementTree as ET
            xml=ET.fromstring(archive.read('word/document.xml'))
            text=' '.join(item.text or '' for item in xml.iter() if item.tag.endswith('}t'))
            assert 'mean.T@1' in text and any(abs(float(value)-350)<1e-6 for value in re.findall(r'\d+(?:\.\d+)?',text))
        done('real_Pandoc_HTML_DOCX_and_generated_Markdown_publish_from_confirmed_postprocess_without_overwriting_user_draft')
        original_id=result['publication']['id']
        for path in result['manifest']['artifacts']:
            assert api.call('GET',api.prefix+'/report/artifact?path='+path,raw=True)==(root/path).read_bytes()
        other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'other-report-123','name':'Other'})['token']
        for suffix in ('/report/results','/report/publications','/report/artifact?path=E-report/build/report.html'):
            api.call('GET',api.prefix+suffix,expect=404,extra_headers={'Authorization':'Bearer '+other})
        post_id=post_step(waiting)['publication']['id']
        api.call('GET',api.prefix+'/report/results?publication_id='+post_id,expect=404)
        api.call('GET',api.prefix+'/report/artifact?path=../0-caseDict/caseDict',expect=404)
        done('immutable_report_downloads_match_published_bytes_and_enforce_ownership_and_kind')
        immutable=root/'.vibeflow/publications'/original_id/'files/E-report/build/report.html'
        original=immutable.read_bytes()
        try:
            immutable.write_bytes(original+b'broken')
            api.call('GET',api.prefix+'/report/results',expect=409)
        finally:
            immutable.write_bytes(original)
        done('damaged_immutable_report_cannot_be_read_as_successful_output')

        api.put('E-report/plot.svg','<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50"><rect width="100" height="50" fill="red"/></svg>')
        manual='# Report draft\n\nManual source with formula $x^2$ and preserved content.\n\n![Plot](plot.svg)\n'
        api.put('E-report/report.md',manual)
        image=root/'E-report/plot.svg'
        original_image=image.read_bytes()
        try:
            image.write_text('<svg xmlns="http://www.w3.org/2000/svg"><image href="../external.png"/></svg>',encoding='utf-8')
            api.call('POST',api.prefix+'/report:build',{},expect=400)
        finally:
            image.write_bytes(original_image)
        done('SVG_with_unfrozen_nested_dependency_is_rejected_before_report_job_admission')
        peer=IsolatedServer('report-handler-peer')
        peer.token=api.token
        peer.prefix=api.prefix
        def start_peer():
            peer.log=(peer.root/'api.log').open('a',encoding='utf-8')
            peer.proc=subprocess.Popen([sys.executable,'-m','uvicorn','controlled_postprocess:app',
                '--app-dir',str(SERVER),'--host','127.0.0.1','--port',str(peer.port),'--no-access-log'],
                cwd=api.root,env=api.env,stdout=peer.log,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                if peer.proc.poll() is not None:
                    raise AssertionError((peer.root/'api.log').read_text(encoding='utf-8'))
                try:
                    if peer.call('GET','/api/health',api_prefix=False).get('ok'): return
                except (OSError,ValueError): pass
                time.sleep(.1)
            raise TimeoutError('Reader API did not start')
        def stop_peer():
            if peer.proc and peer.proc.poll() is None:
                peer.proc.terminate()
                try: peer.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    peer.proc.kill()
                    peer.proc.wait(timeout=5)
            peer.log.close()
        start_peer()
        try:
            def request(client,key=None):
                return client.call('POST',api.prefix+'/report:build',{},extra_headers={'Idempotency-Key':key} if key else None)
            with ThreadPoolExecutor(max_workers=4) as pool:
                submissions=list(pool.map(lambda i:request(api if i%2 else peer,'same-report'),range(4)))
            assert len({row['job_id'] for row in submissions})==1
            api.wait_job(submissions[0]['job_id'])
            peer.wait_job(submissions[0]['job_id'])
            current=api.call('GET',api.prefix+'/report/results')
            assert current['manifest']['mode']=='markdown'
            assert (root/'E-report/report.md').read_text(encoding='utf-8')==manual
            html=(root/'E-report/build/report.html').read_bytes()
            assert b'data:image/svg+xml' in html and b'<math' in html
            with zipfile.ZipFile(root/'E-report/build/report.docx') as archive:
                assert any(name.startswith('word/media/') for name in archive.namelist())
            assert request(peer,'same-report')['job_id']==submissions[0]['job_id']
            history=peer.call('GET',api.prefix+'/report/results?publication_id='+original_id)
            assert not history['publication']['current'] and history['publication']['stale']
            done('cross_API_idempotency_builds_frozen_Markdown_images_math_once_and_keeps_explicit_history')
            stop_peer()
            start_peer()
            assert peer.call('GET',api.prefix+'/report/results')['publication']==current['publication']
            assert peer.call('GET',api.prefix+'/report/results?publication_id='+original_id)['manifest']==result['manifest']
            done('reader_API_restart_retains_current_and_historical_report_identity')

            for mode in ('noop','badhtml','baddocx','badsource','badtoolchain'):
                marker=api.root/f'report-control-{api.case_id}'
                marker.write_text(mode)
                try:
                    failed=api.wait_job(direct()['job_id'],expected='failed')
                finally:
                    marker.unlink(missing_ok=True)
                assert failed['attempt']['reason_code']=='publication_failed'
                assert api.call('GET',api.prefix+'/report/results')['publication']['id']==current['publication']['id']
                done(mode+'_cannot_forge_this_report_even_with_updated_artifact_hashes')

            old=current['publication']['id']
            api.put('E-report/build/report.html',(root/'E-report/build/report.html').read_text(encoding='utf-8'))
            api.call('GET',api.prefix+'/report/results',expect=404)
            assert api.call('GET',api.prefix+'/report/results?publication_id='+old)['publication']['stale']
            api.wait_job(direct()['job_id'])
            current=api.call('GET',api.prefix+'/report/results')
            assert current['publication']['id']!=old
            done('managed_output_edit_invalidates_head_even_for_identical_bytes_and_explicit_rebuild_repairs_it')

            for scenario in ('source_change','image_change','field_change','post_alias_change','head_invalidation','superseded','cancel','late_failure'):
                marker=api.root/f'report-control-{api.case_id}'
                barrier=api.root/f'hold-report-{api.case_id}'
                barrier.with_suffix('.ready').unlink(missing_ok=True)
                barrier.with_suffix('.release').unlink(missing_ok=True)
                marker.write_text('hold_failure' if scenario=='late_failure' else 'hold')
                changed=None
                try:
                    saved=api.call('GET',api.prefix+'/caseDict')['dict_revision']
                    flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'report','run_dir':run_dir,
                        'expected_revision':saved,'parameters':{'report_mode':'markdown'}})
                    wait_file(barrier.with_suffix('.ready'))
                    marker.unlink()
                    if scenario in {'source_change','image_change','field_change','post_alias_change'}:
                        path={'source_change':'E-report/report.md','image_change':'E-report/plot.svg','field_change':run_dir+'/1/T'}.get(scenario)
                        if path is None:
                            path=api.call('GET',api.prefix+'/postprocess/results')['result_dir']+'/metrics.json'
                        changed=root/path
                        previous=changed.read_bytes()
                        changed.write_bytes(previous+b'\nchanged')
                    elif scenario=='head_invalidation':
                        post=api.call('GET',api.prefix+'/postprocess/results')
                        path=post['result_dir']+'/metrics.json'
                        api.put(path,(root/path).read_text(encoding='utf-8'))
                    elif scenario in {'superseded','late_failure'}:
                        winner=request(peer)
                    else:
                        cancel(flow)
                finally:
                    marker.unlink(missing_ok=True)
                    barrier.with_suffix('.release').touch()
                if scenario in {'superseded','late_failure'}:
                    # The newer validator must wait for the held native writer.
                    peer.wait_job(winner['job_id'])
                terminal=wait(flow,status='cancelled' if scenario=='cancel' else 'succeeded' if scenario=='superseded' else 'failed')
                step=next(s for s in terminal['steps'] if s['key']=='report')
                if scenario in {'source_change','image_change','field_change','post_alias_change','head_invalidation'}:
                    api.wait_job(step['attempts'][0]['job_id'],expected='failed')
                    assert api.call('GET',api.prefix+'/report/results')['publication']['id']==current['publication']['id']
                    assert api.call('GET',api.prefix+'/report/results')['publication']['stale']
                    if changed: changed.write_bytes(previous)
                    else:
                        api.wait_job(api.call('POST',api.prefix+'/postprocess:run',{'run_dir':run_dir})['job_id'])
                elif scenario in {'superseded','late_failure'}:
                    if scenario=='superseded': assert step['publication']['status']=='superseded'
                    else: api.wait_job(step['attempts'][0]['job_id'],expected='failed')
                    assert api.call('GET',api.prefix+'/report/results')['publication']['id']==winner['publication_id']
                else:
                    api.wait_job(step['attempts'][0]['job_id'],expected='cancelled')
                    assert not any(row['current'] and row['attempt_id']==step['attempts'][0]['id']
                        for row in api.call('GET',api.prefix+'/report/publications'))
                current=api.call('GET',api.prefix+'/report/results')
                done('held_report_'+scenario+'_preserves_persistent_publication_fences_and_previous_output')
        finally:
            stop_peer()
        root=new_case('confirmed report producer')
        flow=through_solver(submit())
        confirm(flow,'confirm_D')
        waiting=wait(flow,'confirm_E')
        marker=api.root/f'report-control-{api.case_id}'
        marker.write_text('prepare_failure')
        try:
            response=confirm(waiting,'confirm_E')
            assert response['workflow_dispatch']['status']=='pending'
            assert api.call('GET',f'/cases/{api.case_id}')['workflow_state']=='E'
        finally:
            marker.unlink(missing_ok=True)
        api.wait_job(api.call('POST',api.prefix+'/postprocess:run',{'run_dir':run_dir})['job_id'])
        api.call('POST',f'/workflow-runs/{flow["id"]}:dispatch',{'expected_version':detail(flow)['version']},expect=409)
        assert not next(s for s in detail(flow)['steps'] if s['key']=='report')['attempts']
        cancel(flow)
        fresh=wait(submit(),status='succeeded')
        api.wait_job(next(s for s in fresh['steps'] if s['key']=='report')['attempts'][0]['job_id'])
        done('E_confirmation_survives_prepare_failure_but_report_retry_cannot_switch_postprocess_producer')
        # The next workflow starts directly from confirmed E and prepares user
        # Markdown outside its transaction. Cancelling it must stop that request,
        # leave no report attempt, and permit a later explicit fresh workflow.
        api.put('E-report/report.md','# Workflow native preparation\n')
        held=api.root/('hold-preparation-'+str(api.case_id));held.touch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(submit,report_mode='markdown')
            marker=api.root/('preparing-'+str(api.case_id)+'.json')
            wait_file(marker)
            request_id=json.loads(marker.read_text())['id']
            tool=api.root/('tool-'+request_id+'.json');wait_file(tool)
            from app.services import process_identity_native as identity
            tool_birth=identity.observe(json.loads(tool.read_text())['pid'])
            runs=api.call('GET',api.prefix+'/workflow-runs')
            active=next(row for row in runs if row['status']=='running')
            api.put('E-report/preparation-concurrent.bin','write before cancellation')
            cancel(active)
            try:future.result(timeout=20)
            except AssertionError as error:assert 'got 409' in str(error),str(error)
            else:raise AssertionError('A cancelled preparation unexpectedly submitted a report')
        wait_file(marker.with_suffix('.result'))
        request=json.loads(marker.with_suffix('.result').read_text())
        assert request['phase']=='cancelled' and request['cleanup_verified'] is True
        assert identity.probe(tool_birth)['state']=='exited'
        assert not next(s for s in detail(active)['steps'] if s['key']=='report')['attempts']
        held.unlink()
        # Avoid reusing the isolated result marker for the next successful request.
        marker.unlink();marker.with_suffix('.result').unlink()
        resumed=wait(submit(report_mode='markdown'),status='succeeded')
        api.wait_job(next(s for s in resumed['steps'] if s['key']=='report')['attempts'][0]['job_id'])
        done('workflow_cancellation_stops_native_preparation_without_Job_and_explicit_fresh_dispatch_succeeds')
        root=new_case('postprocess published during report admission')
        flow=through_solver(submit('postprocess'))
        barrier=api.root/f'report-admission-{api.case_id}'
        barrier.touch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(direct,'report-admission-race')
            try:
                wait_file(barrier.with_suffix('.ready'))
                confirm(flow,'confirm_D')
                waiting=wait(flow,'confirm_E')
            finally:
                barrier.unlink(missing_ok=True)
                barrier.with_suffix('.release').touch()
            submitted=pending.result(timeout=30)
        api.wait_job(submitted['job_id'])
        assert api.call('GET',api.prefix+'/report/results')['publication']['id']==submitted['publication_id']
        confirm(waiting,'confirm_E')
        done('postprocess_published_while_report_admission_waits_cannot_downgrade_to_legacy_build')
        result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
        (api.root/'report-handler-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        return result


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='report-handler-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_report_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_report_[0-9a-f]{32}',name)
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
    print('REPORT_HANDLER_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':
    asyncio.run(main())
