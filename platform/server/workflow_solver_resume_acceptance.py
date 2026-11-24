"""Actual OpenFOAM workflow checkpoint chain and uninterrupted numerical comparison.

All geometry, mesh, solver and VTK work is real. No inference is invoked or
substituted. Optional S3 uses an owned disposable SeaweedFS namespace.
"""
import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid
from urllib.parse import quote
import zipfile

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testsupport import IsolatedServer, SERVER
from cae_fixtures import cavity_2d
from workflow_query_acceptance import APP
from workflow_resume_acceptance import postgres, sha, ROOT


def verify(args, database_url=None, store=None):
    from app.services.windows_process_scope import ProcessHandle
    from app.services.casedict import set_block
    api=IsolatedServer('workflow-checkpoint-'+('S3-' if store else 'FS-')+('PG' if database_url else 'SQLite'),
        app='checkpoint_fixture:app',database_url=database_url)
    prefix='import os\nos.environ.update('+repr(store.settings())+')\n' if store else ''
    (api.root/'checkpoint_fixture.py').write_text(prefix+APP,encoding='utf8')
    url=database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix()
    report={'passed':False,'backend':('S3+' if store else 'FS+')+('PostgreSQL' if database_url else 'SQLite'),
        'real_model_calls':0,'mock_llm_calls':0,'checks':[],'actual_native_jobs':[],
        'developer_database_modified':False,'review_sha256':sha(args.review)}
    entered=False;handle=None
    through_report=getattr(args,'through_report',False)
    restored_report=None
    if store:
        original_open=api.opener.open
        def measured(request,*positional,**kwargs):
            kwargs['timeout']=120;started=time.monotonic()
            try:return original_open(request,*positional,**kwargs)
            finally:
                with (api.root/'HTTP-timings.jsonl').open('a',encoding='utf8') as stream:
                    stream.write(json.dumps({'method':request.get_method(),'path':request.selector,
                        'seconds':round(time.monotonic()-started,4),'client_timeout_seconds':120})+'\n')
        api.opener.open=measured

    def query(sql,params=None):
        async def read():
            engine=create_async_engine(url)
            try:
                async with engine.connect() as connection:
                    return [list(row) for row in await connection.execute(text(sql),params or {})]
            finally:await engine.dispose()
        return asyncio.run(read())

    def save():
        (api.root/'checkpoint-progress.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf8')

    def stopped(identifier,expected='succeeded',timeout=240):
        if identifier not in report['actual_native_jobs']:report['actual_native_jobs'].append(identifier)
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            value=api.call('GET','/jobs/'+str(identifier));attempt=value.get('attempt') or {}
            if value['status']==expected and attempt.get('phase')==expected and (attempt.get('process_stop') or {}).get('verified') is True:
                (api.root/('job-'+str(identifier)+'-stopped.json')).write_text(
                    json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
                return value
            if value.get('completion_error'):raise RuntimeError('Actual finalization error: '+value['completion_error'])
            if value['status'] in {'failed','interrupted'} and value['status']!=expected:
                raise AssertionError('Native task failed: '+str(identifier)+' '+str(attempt.get('reason_code')))
            time.sleep(.15)
        raise TimeoutError('Actual native task did not settle: '+str(identifier))

    def workflow(identifier,stage=None,status=None):
        deadline=time.monotonic()+240
        while time.monotonic()<deadline:
            run=api.call('GET','/workflow-runs/'+identifier)
            for step in run['steps']:
                for attempt in step['attempts']:
                    if attempt['job_id'] not in report['actual_native_jobs']:report['actual_native_jobs'].append(attempt['job_id'])
            if stage:
                step=next(item for item in run['steps'] if item['key']==stage)
                if step['status']==status:return run,step
            elif run['status']==status:return run
            if run['status'] in {'failed','interrupted','cancelled'} and status!=run['status']:
                raise AssertionError('Actual workflow ended: '+json.dumps(run)[:1400])
            time.sleep(.15)
        raise TimeoutError('Actual workflow did not reach '+str(stage or identifier)+':'+str(status))

    def confirm(identifier,key):
        run,step=workflow(identifier,key,'awaiting_confirmation')
        for producer in run['steps']:
            if producer['status']=='succeeded':
                for attempt in producer['attempts']:stopped(attempt['job_id'])
        return api.call('POST',api.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
            'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})

    def native_work(identifier):
        value=api.call('GET','/jobs/'+str(identifier));manifest=value['attempt']['workspace']
        case_root=(api.root/'files'/manifest['storage']['prefix'] if store else
            next((api.root/'files/workspaces').glob('*/cases/'+str(api.case_id))))
        return case_root/manifest['run_path']

    def cancel_at(identifier,job_id,label):
        root=native_work(job_id);checkpoint=root/'C-run/case-1'/label;deadline=time.monotonic()+120
        while not all((checkpoint/name).is_file() for name in ('U','p')):
            current=api.call('GET','/jobs/'+str(job_id))
            if current['status'] not in {'queued','running'}:raise AssertionError('Solver ended before checkpoint '+label)
            if time.monotonic()>deadline:raise TimeoutError('Real solver did not write checkpoint '+label)
            time.sleep(.02)
        current=api.call('GET','/workflow-runs/'+identifier)
        api.call('POST','/workflow-runs/'+identifier+':cancel',{'expected_version':current['version']})
        done=stopped(job_id,'cancelled');workflow(identifier,status='cancelled')
        return root,done

    def restore_body(identifier,label):
        plan=api.call('GET','/workflow-runs/'+identifier+'/resume-plan')
        item=next(step for step in plan['steps'] if step.get('key')=='solve' and step['checkpoint']['available'])
        body={key:plan[key] for key in ('expected_version','expected_revision')}
        body.update({key:item[key] for key in ('source_step_id','expected_step_version','source_attempt_id','expected_attempt_version')})
        body.update(mode='checkpoint',checkpoint_time=label,confirmed=True)
        return body

    def resume(identifier,body,key):
        deadline=time.monotonic()+40
        while True:
            value=api.call('POST','/workflow-runs/'+identifier+':resume',body,expect=(202,409),extra_headers={'Idempotency-Key':key})
            if 'id' in value:
                for step in value['steps']:
                    for attempt in step['attempts']:
                        if attempt['job_id'] not in report['actual_native_jobs']:report['actual_native_jobs'].append(attempt['job_id'])
                return value
            if value.get('detail',{}).get('code') not in {'storage_quota_busy','validation_busy'}:
                raise AssertionError('Actual native continuation refused: '+json.dumps(value))
            if time.monotonic()>deadline:raise TimeoutError('Original-key checkpoint admission remained busy')
            time.sleep(.2)

    def old_rows(identifier,job_id):
        return {name:query(sql,{'run':identifier,'job':job_id}) for name,sql in {
            'run':'SELECT * FROM workflow_runs WHERE id=:run','steps':'SELECT * FROM workflow_steps WHERE run_id=:run ORDER BY id',
            'job':'SELECT * FROM jobs WHERE id=:job','attempt':'SELECT * FROM job_attempts WHERE job_id=:job'}.items()}

    try:
        api.__enter__();entered=True;birth=api.call('GET','/test/query-birth');handle=ProcessHandle.query(birth);report['API_birth']=birth
        api.create_case('Actual workflow OpenFOAM checkpoint')
        saved=api.call('GET',api.prefix+'/caseDict')
        content=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':
            {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
        content=set_block(content,'numericalMethod',{'solver':'icoFoam'})
        post={'runDir':'C-run/case-1'}
        if through_report:
            post.update(fields={'p':{'unit':'"m^2/s^2"'}},dataExtraction={
                'volumeAverages':{'mean':{'field':'p','time':.5}},
                'pointProbes':{'center':{'fields':['p'],'points':[.05,.05,.005],'times':[.5]}}})
        content=set_block(content,'postProcessing',post)
        api.call('PUT',api.prefix+'/caseDict',{'content':content,'expected_revision':saved['dict_revision']})
        for name,content in cavity_2d().items():
            if name.endswith('/controlDict'):
                content=content.replace('deltaT          0.005;','deltaT          0.0005;').replace('writeInterval   20;','writeInterval   10;')
            api.put(name,content)
        dimensions='.1,.1,.01' if through_report else '1,1,1'
        geometry=api.put('A-geo/geometry.py',"import cadquery as cq\ndef build_geometry(ctx):return {'cube':cq.Workplane('XY').box("+dimensions+",centered=(False,False,False))}\n")
        if geometry.get('auto_preview_job'):stopped(geometry['auto_preview_job'])
        control=api.call('GET',api.prefix+'/raw?path=C-run/case-1/system/controlDict',raw=True)
        flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'report' if through_report else 'solve','run_dir':'C-run/case-1',
            'expected_revision':api.call('GET',api.prefix+'/caseDict')['dict_revision'],
            'budget':{'timeout_seconds':200,'max_log_bytes':16777216}},extra_headers={'Idempotency-Key':str(uuid.uuid4())})
        confirm(flow['id'],'confirm_B');confirm(flow['id'],'confirm_C')
        _,solve=workflow(flow['id'],'solve','running');source_job=solve['attempts'][-1]['job_id']
        source_work,source_done=cancel_at(flow['id'],source_job,'0.005')
        original_rows=old_rows(flow['id'],source_job)
        body=restore_body(flow['id'],'0.005');before=query('SELECT COUNT(*) FROM jobs')[0][0]
        for label in ('0','0.4999'):
            rejected=api.call('POST','/workflow-runs/'+flow['id']+':resume',dict(body,checkpoint_time=label),
                expect=409,extra_headers={'Idempotency-Key':str(uuid.uuid4())})
            assert rejected['detail']['code']=='checkpoint_invalid',rejected
        assert query('SELECT COUNT(*) FROM jobs')[0][0]==before
        report['checks'].append({'kind':'real_400_cell_solver_cancelled_and_unwritten_checkpoint_refused',
            'source_run':flow['id'],'source_job':source_job,'source_stop':source_done['attempt']['process_stop']})
        if store:
            retained=api.root/('retained-source-'+source_done['attempt']['id'])
            assert source_work.resolve().is_relative_to((api.root/'files').resolve()) and not retained.exists()
            source_work.rename(retained);assert not source_work.exists()
            report['source_cache_moved_without_deleting_evidence']=True
        key=str(uuid.uuid4());first=resume(flow['id'],body,key)
        first_job=next(item['job_id'] for step in first['steps'] if step['key']=='solve' for item in step['attempts'])
        first_work,first_done=cancel_at(first['id'],first_job,'0.01')
        assert first_done['attempt']['number']==2 and first_done['attempt']['root_job_id']==source_job
        assert first_done['attempt']['handler']['key']=='solve.openfoam.resume'
        assert resume(flow['id'],body,key)['id']==first['id']
        first_rows=old_rows(first['id'],first_job)
        second_body=restore_body(first['id'],'0.01');second=resume(first['id'],second_body,str(uuid.uuid4()))
        second_job=next(item['job_id'] for step in second['steps'] if step['key']=='solve' for item in step['attempts'])
        completed=stopped(second_job);waiting,_=workflow(second['id'],'confirm_D','awaiting_confirmation')
        assert completed['attempt']['number']==3 and completed['attempt']['root_job_id']==source_job
        assert completed['attempt']['retry_of_attempt_id']==first_done['attempt']['id']
        new_work=native_work(second_job)
        checkpoint=json.loads((new_work/'C-run/case-1/resume-checkpoint.json').read_text(encoding='utf8'))
        assert checkpoint['time']==.01 and checkpoint['source']['attempt_id']==first_done['attempt']['id']
        for name,record in checkpoint['files'].items():
            assert hashlib.sha256((new_work/name).read_bytes()).hexdigest()==record['sha256']
        results=api.call('GET',api.prefix+'/solve/results');evidence=results['evidence']
        assert evidence['configuration']['start_time']==.01 and evidence['parsed_log']['times'][0]>.01
        assert evidence['summary']['last_time']==.5 and not results['publication']['stale']
        assert completed['attempt']['outputs']['publication']['status']=='committed'
        assert api.call('GET',api.prefix+'/raw?path=C-run/case-1/system/controlDict',raw=True)==control
        if store:
            assert checkpoint['archive_source']['kind']=='verified_object_outputs'
            assert not source_work.exists() and not list((api.root/'files/.attempt-sources').glob('owned-*'))
        report['checks'].append({'kind':'two_actual_workflow_checkpoint_continuations','first_run':first['id'],
            'second_run':second['id'],'first_job':first_job,'second_job':second_job,'same_request_family':source_job,
            'native_start_times':[.005,.01],'first_executed_time':evidence['parsed_log']['times'][0],
            'last_time':evidence['summary']['last_time'],'stage_D_requires_confirmation':True,'checkpoint_files':len(checkpoint['files'])})
        save()
        if args.confirm_resumed:
            confirm(second['id'],'confirm_D')
            if through_report:
                confirm(second['id'],'confirm_E')
            final_resumed=workflow(second['id'],status='succeeded')
            report['checks'][-1]['resumed_workflow_completed_after_explicit_D_confirmation']=True
            if through_report:
                post_step=next(step for step in final_resumed['steps'] if step['key']=='postprocess')
                report_step=next(step for step in final_resumed['steps'] if step['key']=='report')
                post_job=stopped(post_step['attempts'][-1]['job_id'])
                report_job=stopped(report_step['attempts'][-1]['job_id'])
                solved_id=completed['attempt']['outputs']['publication']['id']
                post_id=post_job['attempt']['outputs']['publication']['id']
                assert solved_id in {p['publication_id'] for p in post_job['attempt']['workspace']['publications']}
                assert post_id in {p['publication_id'] for p in report_job['attempt']['workspace']['publications']}
                document=api.call('GET',api.prefix+'/report/results')
                identifier=report_job['attempt']['outputs']['publication']['id']
                assert document['publication']['id']==identifier and not document['publication']['stale']
                downloads={}
                for path,expected in document['manifest']['artifacts'].items():
                    data=api.call('GET',api.prefix+'/report/artifact?publication_id='+identifier+'&path='+quote(path,safe=''),raw=True)
                    assert hashlib.sha256(data).hexdigest()==expected['sha256']
                    if path.endswith('.html'):assert b'<html' in data.lower()
                    if path.endswith('.docx'):
                        with zipfile.ZipFile(io.BytesIO(data)) as archive:assert b'<w:document' in archive.read('word/document.xml')
                    (api.root/('resumed-'+Path(path).name)).write_bytes(data)
                    downloads[path]={'sha256':expected['sha256'],'bytes':len(data)}
                assert any(p.endswith('.html') for p in downloads) and any(p.endswith('.docx') for p in downloads)
                restored_report={'run_id':second['id'],'solve_job_id':second_job,'solve_publication_id':solved_id,
                    'postprocess_job_id':post_job['id'],'postprocess_publication_id':post_id,
                    'report_job_id':report_job['id'],'report_publication_id':identifier,'downloads':downloads}
                report['resumed_report']=restored_report
                (api.root/'resumed-report-workflow.json').write_text(json.dumps(final_resumed,indent=2)+'\n',encoding='utf8')
                save()
            # Explicit owner actions restore C for an ordinary fresh solve.
            # No geometry/mesh is regenerated and no approval is implicit.
            for stage in ('A','B','C'):api.advance(stage)
        else:
            api.call('POST','/workflow-runs/'+second['id']+':cancel',{'expected_version':waiting['version']})
        # A new ordinary workflow at C executes the unchanged controls from
        # startTime=0. It cannot inherit a prior continuation's checkpoint.
        baseline=api.call('POST',api.prefix+'/workflow-runs',{'target':'solve','run_dir':'C-run/case-1',
            'expected_revision':api.call('GET',api.prefix+'/caseDict')['dict_revision'],
            'budget':{'timeout_seconds':200,'max_log_bytes':16777216}},extra_headers={'Idempotency-Key':str(uuid.uuid4())})
        fresh_job=next(item['job_id'] for step in baseline['steps'] for item in step['attempts'])
        fresh=stopped(fresh_job);assert fresh['attempt']['handler']['key']=='solve.openfoam'
        assert fresh['attempt']['number']==1 and fresh['attempt']['root_job_id']==fresh_job
        fresh_work=native_work(fresh_job)
        if through_report:
            fresh_evidence=api.call('GET',api.prefix+'/solve/results')['evidence']['parsed_log']
            assert fresh_evidence['summary']['last_time']==.5 and fresh_evidence['summary']['time_points']==1000
            residuals=fresh_evidence['residuals'].get('Ux') or fresh_evidence['residuals'].get('U')
            assert residuals and residuals[-1]<residuals[0]
            report['fresh_1000_step_comparison']={'last_time':.5,'time_points':1000,'Ux_residual_drop_ratio':residuals[0]/residuals[-1]}
        import numpy as np
        import vtk
        from vtk.util.numpy_support import vtk_to_numpy
        def fields(root):
            reader=vtk.vtkOpenFOAMReader();reader.SetFileName(str(root/'C-run/case-1/case-1.foam'))
            reader.CreateCellToPointOff();reader.UpdateInformation();reader.EnableAllCellArrays();reader.UpdateTimeStep(.5)
            grid=reader.GetOutput().GetBlock(0);assert grid.GetNumberOfCells()==400
            return {name:vtk_to_numpy(grid.GetCellData().GetArray(name)).copy() for name in ('U','p')}
        resumed_fields,fresh_fields=fields(new_work),fields(fresh_work);differences={}
        for name in resumed_fields:
            a,b=resumed_fields[name],fresh_fields[name];assert np.isfinite(a).all() and np.isfinite(b).all()
            differences[name]=float(np.max(np.abs(a-b)));assert np.allclose(a,b,rtol=2e-3,atol=2e-5),(name,differences[name])
        assert original_rows==old_rows(flow['id'],source_job) and first_rows==old_rows(first['id'],first_job)
        assert api.call('GET','/workflow-runs/'+second['id'])['resumption']['source_run_id']==first['id']
        confirm(baseline['id'],'confirm_D');workflow(baseline['id'],status='succeeded')
        report['checks'].append({'kind':'actual_fresh_solver_numerical_comparison_and_original_histories_unchanged',
            'fresh_workflow':baseline['id'],'fresh_job':fresh_job,'cells':400,'finite_fields':['U','p'],
            'max_abs_differences':differences,'rtol':2e-3,'atol':2e-5,'fresh_handler':'solve.openfoam'})
        if through_report:
            for path,expected in restored_report['downloads'].items():
                data=api.call('GET',api.prefix+'/report/artifact?publication_id='+restored_report['report_publication_id']+'&path='+quote(path,safe=''),raw=True)
                assert hashlib.sha256(data).hexdigest()==expected['sha256']
            report['resumed_report_preserved_after_fresh_comparison']=True
        report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1800],
            'frames':[{'file':Path(row.filename).name,'line':row.lineno} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                if hasattr(api,'case_id'):
                    for job in api.call('GET',api.prefix+'/jobs'):
                        if job['id'] not in report['actual_native_jobs']:report['actual_native_jobs'].append(job['id'])
                for identifier in report['actual_native_jobs']:
                    value=api.call('GET','/jobs/'+str(identifier));phase=(value.get('attempt') or {}).get('phase')
                    if value['status'] in {'queued','running'} or phase not in {'succeeded','failed','cancelled','interrupted'}:
                        api.call('POST','/jobs/'+str(identifier)+':cancel');stopped(identifier,'cancelled')
                    else:stopped(identifier,value['status'])
                report['all_actual_native_stops_verified']=True
            except BaseException as error:report['cleanup_failure']=type(error).__name__
            api.__exit__(None if report.get('functional_passed') else RuntimeError,None,None)
        if handle:handle.wait_exit(15);handle.close();report['API_exit_verified']=True
        report['passed']=bool(report.get('functional_passed') and report.get('API_exit_verified') and 'cleanup_failure' not in report)
        (api.root/'checkpoint-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf8')
        print('WORKFLOW_CHECKPOINT_RESULT '+str(api.root/'checkpoint-result.json'),flush=True)
    return report,api.root


def selected(args,database_url=None,*,verify_callback=None):
    callback=verify_callback or verify
    if not args.s3:return callback(args,database_url)
    from s3testsupport import ObjectStore
    from app.services.windows_process_scope import ProcessHandle
    from app.services.process_identity_native import observe
    store=ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed',volume_max=16)
    print('WORKFLOW_CHECKPOINT_STORE '+str(store.root),flush=True)
    receipt={'passed':False};handle=None
    try:
        with store:
            handle=ProcessHandle.query(observe(store.proc.pid));result=callback(args,database_url,store)
            receipt.update(passed=result[0]['passed'],API_evidence=str(result[1]))
    finally:
        if handle:handle.wait_exit(15);handle.close();receipt['Windows_owner_exit_verified']=True
        code="""import json,pathlib,sys
r=pathlib.Path(sys.argv[1]);o=json.loads((r/'owner.json').read_text());p=pathlib.Path('/proc')/str(o['pid'])
if p.exists():
 s=(p/'stat').read_text().rsplit(')',1)[1].split()
 assert s[19]!=o['start'] or p.stat().st_uid!=o['uid'],'Original isolated store remains alive'
print('NATIVE_STORE_EXIT_VERIFIED')
"""
        check=subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c',code,store.linux],
            capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        assert check.returncode==0 and b'NATIVE_STORE_EXIT_VERIFIED' in check.stdout
        receipt['native_owner_exit_verified']=True
        (store.root/'workflow-checkpoint-store-result.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf8')
    return result


def run():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review',type=Path,required=True);parser.add_argument('--postgres',action='store_true');parser.add_argument('--s3',action='store_true')
    parser.add_argument('--confirm-resumed',action='store_true')
    parser.add_argument('--through-report',action='store_true',help='Complete the checkpoint continuation through registered postprocessing and report')
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf8'))
    if args.through_report and not args.confirm_resumed:parser.error('--through-report requires --confirm-resumed')
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    if args.postgres:return asyncio.run(postgres(args,verify_callback=selected))
    report,_=selected(args);return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
