"""Actual MPI/UDF workflow cancellation, sealed retry and prefix integrity.

Native compilation, CDLL loading, two-rank MPI and solver fields are real.
Explicit projection faults change SQL metadata only; no inference is invoked.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testsupport import IsolatedServer, SERVER
from cae_fixtures import cavity_2d
from workflow_query_acceptance import APP as BASE_APP
from workflow_resume_acceptance import postgres, sha, ROOT
from workflow_solver_resume_acceptance import selected as with_storage

EXTRA='''
from app.services.workflow_queries import object_record
@app.post('/api/v1/test/professional-prefix/{identifier}')
async def prefix_fault(identifier:str,body:dict,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        run=await owned(db,identifier,user)
        step=await db.scalar(select(WorkflowStep).where(WorkflowStep.run_id==run.id,WorkflowStep.step_key=='udf-build'))
        saved=Path.cwd()/('prefix-original-'+step.id+'.json')
        if body['action']=='change_generation':
            assert not saved.exists();saved.write_text(json.dumps({'output':step.output_manifest_json,
                'updated_at':step.updated_at.isoformat()}),encoding='utf8')
            output=object_record(step.output_manifest_json,'actual prior build')
            output['publication']['generation']+=1;step.output_manifest_json=json.dumps(output)
        elif body['action']=='restore_exact':
            original=json.loads(saved.read_text(encoding='utf8'));step.output_manifest_json=original['output']
            step.updated_at=datetime.fromisoformat(original['updated_at'])
        else:raise ValueError('Unknown explicit metadata fault')
        await db.commit();return {'SQL_metadata_only':True,'native_execution_started':False}
'''
APP=BASE_APP+EXTRA

INSPECT='''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]);import process_identity_native as identity
request=json.load(sys.stdin)
if 'probe' in request:print(json.dumps([identity.probe(value) for value in request['probe']]))
else:
 root=request['root'];result=[];seen=set();pending=[root['pid']]
 if identity.probe(root)['state']!='match':pending=[]
 while pending:
  pid=pending.pop()
  if pid in seen:continue
  seen.add(pid);assert len(seen)<128
  try:
   record=identity.observe(pid);base=Path('/proc')/str(pid);comm=(base/'comm').read_text().strip()
   for task in (base/'task').iterdir():
    try:pending.extend(map(int,(task/'children').read_text().split()))
    except FileNotFoundError:pass
   if identity.probe(record)['state']=='match':result.append({'identity':record,'comm':comm})
  except (FileNotFoundError,ProcessLookupError):pass
 print(json.dumps(result))
'''


def native_inspect(distribution,request):
    from app.services.owned_process import wsl_path
    result=subprocess.run(['wsl.exe','-d',distribution,'--exec','python3','-I','-S','-c',INSPECT,
        wsl_path(SERVER/'app/services')],input=json.dumps(request).encode(),capture_output=True,timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==0,'Actual native identity observation failed'
    return json.loads(result.stdout)


def verify(args,database_url=None,store=None):
    from app.services.windows_process_scope import ProcessHandle
    from app.services.casedict import set_block
    mode=args.mode;label='workflow-'+mode+'-retry-'+('S3-' if store else 'FS-')+('PG' if database_url else 'SQLite')
    write_interval=getattr(args,'write_interval',100)
    api=IsolatedServer(label,app='professional_fixture:app',database_url=database_url)
    prefix='import os\nos.environ.update('+repr(store.settings())+')\n' if store else ''
    (api.root/'professional_fixture.py').write_text(prefix+APP,encoding='utf8')
    url=database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix()
    report={'passed':False,'mode':mode,'backend':('S3+' if store else 'FS+')+('PG' if database_url else 'SQLite'),
        'real_model_calls':0,'mock_llm_calls':0,'checks':[],'actual_native_jobs':[],
        'developer_database_modified':False,'review_sha256':sha(args.review),'write_interval':write_interval}
    entered=False;handle=None
    original_open=api.opener.open
    def measured(request,*positional,**kwargs):
        if store:kwargs['timeout']=120
        started=time.monotonic()
        try:return original_open(request,*positional,**kwargs)
        finally:
            with (api.root/'HTTP-timings.jsonl').open('a',encoding='utf8') as stream:
                stream.write(json.dumps({'method':request.get_method(),'path':request.selector,'seconds':round(time.monotonic()-started,4)})+'\n')
    api.opener.open=measured

    def query(sql,params=None):
        async def read():
            engine=create_async_engine(url)
            try:
                async with engine.connect() as connection:return [list(row) for row in await connection.execute(text(sql),params or {})]
            finally:await engine.dispose()
        return asyncio.run(read())

    def save():
        (api.root/'professional-progress.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf8')

    def stopped(identifier,expected='succeeded'):
        if identifier not in report['actual_native_jobs']:report['actual_native_jobs'].append(identifier)
        deadline=time.monotonic()+360
        while time.monotonic()<deadline:
            value=api.call('GET','/jobs/'+str(identifier));attempt=value.get('attempt') or {}
            if value['status']==expected and attempt.get('phase')==expected and (attempt.get('process_stop') or {}).get('verified') is True:
                (api.root/('job-'+str(identifier)+'-stopped.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
                return value
            if value.get('completion_error'):raise RuntimeError('Actual finalization error: '+value['completion_error'])
            if value['status'] in {'failed','interrupted'} and value['status']!=expected:
                (api.root/'failed-native-job.json').write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
                raise AssertionError('Actual professional task failed: '+str(identifier))
            time.sleep(.15)
        raise TimeoutError('Actual professional task did not settle: '+str(identifier))

    def workflow(identifier,key=None,status=None):
        deadline=time.monotonic()+360
        while time.monotonic()<deadline:
            run=api.call('GET','/workflow-runs/'+identifier)
            for step in run['steps']:
                for attempt in step['attempts']:
                    if attempt['job_id'] not in report['actual_native_jobs']:report['actual_native_jobs'].append(attempt['job_id'])
            if key:
                step=next(item for item in run['steps'] if item['key']==key)
                if step['status']==status:return run,step
            elif run['status']==status:return run
            if run['status'] in {'failed','interrupted','cancelled'} and status!=run['status']:
                (api.root/'failed-workflow.json').write_text(json.dumps(run,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
                raise AssertionError('Actual professional workflow ended before '+str(key or status))
            time.sleep(.15)
        raise TimeoutError('Actual professional workflow did not reach '+str(key or identifier)+':'+str(status))

    def confirm(identifier,key):
        run,step=workflow(identifier,key,'awaiting_confirmation')
        for producer in run['steps']:
            if producer['status']=='succeeded':
                for attempt in producer['attempts']:stopped(attempt['job_id'])
        return api.call('POST',api.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
            'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})

    def work(identifier):
        value=api.call('GET','/jobs/'+str(identifier));manifest=value['attempt']['workspace']
        case_root=api.root/'files'/manifest['storage']['prefix'] if store else next((api.root/'files/workspaces').glob('*/cases/'+str(api.case_id)))
        return case_root/manifest['run_path']

    def resume(identifier,body,key):
        deadline=time.monotonic()+40
        while True:
            result=api.call('POST','/workflow-runs/'+identifier+':resume',body,expect=(202,409),extra_headers={'Idempotency-Key':key})
            if 'id' in result:return result
            if result.get('detail',{}).get('code') not in {'storage_quota_busy','validation_busy'}:
                raise AssertionError('Actual registered retry refused: '+json.dumps(result))
            if time.monotonic()>deadline:raise TimeoutError('Registered retry admission stayed busy; original key retained')
            time.sleep(.2)

    try:
        api.__enter__();entered=True;birth=api.call('GET','/test/query-birth');handle=ProcessHandle.query(birth);report['API_birth']=birth
        api.create_case('Actual '+mode+' workflow retry')
        saved=api.call('GET',api.prefix+'/caseDict')
        content=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':
            {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
        content=set_block(content,'numericalMethod',{'solver':'icoFoam'})
        content=set_block(content,'postProcessing',{'runDir':'C-run/case-1'})
        api.call('PUT',api.prefix+'/caseDict',{'content':content,'expected_revision':saved['dict_revision']})
        for path,content in cavity_2d().items():
            if path.endswith('/controlDict'):
                content=content.replace('deltaT          0.005;','deltaT          0.0005;').replace('writeInterval   20;','writeInterval   '+str(write_interval)+';')
            if mode=='mpi' and path.endswith('/blockMeshDict'):
                content=content.replace('(20 20 1)','(40 40 1)')
            api.put(path,content)
        parameters={'geometry_mode':'preview','solve_mode':mode}
        if mode=='udf':
            name='myFunctionObject';parameters['udf_name']=name
            api.call('POST',api.prefix+'/udf/scaffold',{'name':name,'objective':'Actual workflow sealed retry'})
            cpp='UDF/'+name+'/myFunctionObject.C'
            original=api.call('GET',api.prefix+'/raw?path='+cpp,raw=True).decode();assert 'P0_UDF_OK' in original
            api.put(cpp,original.replace('P0_UDF_OK','N6_UDF_RETRY_ACTUAL'))
        else:parameters['subdomains']=2
        geometry=api.put('A-geo/geometry.py',"import cadquery as cq\ndef build_geometry(ctx):return {'cube':cq.Workplane('XY').box(1,1,1)}\n")
        if geometry.get('auto_preview_job'):stopped(geometry['auto_preview_job'])
        flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'solve','run_dir':'C-run/case-1','parameters':parameters,
            'expected_revision':api.call('GET',api.prefix+'/caseDict')['dict_revision'],
            'budget':{'timeout_seconds':300,'max_log_bytes':16777216}},extra_headers={'Idempotency-Key':str(uuid.uuid4())})
        confirm(flow['id'],'confirm_B');confirm(flow['id'],'confirm_C')
        running,source_step=workflow(flow['id'],'solve','running');source_job=source_step['attempts'][-1]['job_id']
        deadline=time.monotonic()+150;observed=[];distribution=None
        while not observed:
            current=api.call('GET','/jobs/'+str(source_job))
            if current['status'] not in {'queued','running'}:
                (api.root/'source-ended-before-observation.json').write_text(json.dumps(current,indent=2)+'\n',encoding='utf8')
                raise AssertionError('Native solver ended before live process observation')
            for child in (current['attempt'].get('child_processes') or {}).get('children',{}).values():
                native=(child.get('identity') or {}).get('native')
                if child['phase']=='running' and child['purpose']==mode+'_run' and native:
                    distribution=native['distribution'];members=native_inspect(distribution,{'root':native['process']})
                    wanted=[row for row in members if row['comm']=='icoFoam']
                    if len(wanted)>=(2 if mode=='mpi' else 1):observed=wanted;break
            if time.monotonic()>deadline:raise TimeoutError('Actual professional solver processes were not observed')
            if not observed:time.sleep(.05)
        live=api.call('GET','/workflow-runs/'+flow['id'])
        api.call('POST','/workflow-runs/'+flow['id']+':cancel',{'expected_version':live['version']})
        source_done=stopped(source_job,'cancelled');old=workflow(flow['id'],status='cancelled')
        exits=native_inspect(distribution,{'probe':[row['identity'] for row in observed]})
        assert all(row['state']=='exited' for row in exits),exits
        report['actual_source_native_processes']=observed;report['observed_native_process_exits']=exits
        old_rows={table:query('SELECT * FROM '+table+' WHERE '+column+'=:id ORDER BY id',{'id':flow['id'] if table in {'workflow_runs','workflow_steps'} else source_job})
            for table,column in [('workflow_runs','id'),('workflow_steps','run_id'),('jobs','id'),('job_attempts','job_id')]}
        choice=api.call('GET','/workflow-runs/'+flow['id']+'/resume-plan')
        selected=next(step for step in choice['steps'] if step.get('key')=='solve' and step['retry_step']['available'])
        assert not selected['checkpoint']['available']
        body={key:choice[key] for key in ('expected_version','expected_revision')}
        body.update({key:selected[key] for key in ('source_step_id','expected_step_version','source_attempt_id','expected_attempt_version')})
        body.update(mode='retry_step',confirmed=True)
        count=query('SELECT COUNT(*) FROM jobs')[0][0]
        error=api.call('POST','/workflow-runs/'+flow['id']+':resume',dict(body,mode='checkpoint',checkpoint_time='0.005'),
            expect=409,extra_headers={'Idempotency-Key':str(uuid.uuid4())})
        assert error['detail']['code']=='checkpoint_required' and query('SELECT COUNT(*) FROM jobs')[0][0]==count
        build_pub=None
        if mode=='udf':
            build=next(step for step in old['steps'] if step['key']=='udf-build')
            built=stopped(build['attempts'][0]['job_id']);build_pub=built['attempt']['outputs']['publication']
            assert 'attempt_id' not in build_pub
            api.call('POST','/test/professional-prefix/'+flow['id'],{'action':'change_generation'})
            denied=api.call('GET','/workflow-runs/'+flow['id']+'/resume-plan')
            issue=next(step for step in denied['steps'] if step['source_step_id']==body['source_step_id'])
            assert not issue['retry_step']['available'] and issue['reason']['code']=='evidence_invalid'
            rejected=api.call('POST','/workflow-runs/'+flow['id']+':resume',body,expect=409,extra_headers={'Idempotency-Key':str(uuid.uuid4())})
            assert rejected['detail']['code']=='evidence_invalid' and query('SELECT COUNT(*) FROM jobs')[0][0]==count
            api.call('POST','/test/professional-prefix/'+flow['id'],{'action':'restore_exact'})
        report['checks'].append({'kind':'actual_native_cancelled_and_checkpoint_mode_explicitly_unsupported',
            'source_workflow':flow['id'],'source_job':source_job,'source_handler':source_done['attempt']['handler']['key'],
            'two_rank_MPI':mode=='mpi','actual_UDF_build_and_load':mode=='udf','metadata_projection_fault_refused':mode=='udf'})
        publications_before=query("SELECT id FROM artifact_publications WHERE status='committed' ORDER BY id")
        heads_before=query('SELECT case_id,kind,active_publication_id FROM artifact_publication_heads ORDER BY case_id,kind')
        save();key=str(uuid.uuid4());new=resume(flow['id'],body,key)
        solve=next(step for step in new['steps'] if step['key']=='solve');new_job=solve['attempts'][0]['job_id']
        if args.expect_trace_limit:
            done=stopped(new_job,'failed');failed_run=workflow(new['id'],status='failed')
            assert done['attempt']['reason_code']=='resource_limit'
            children=done['attempt']['child_processes']['children'].values()
            assert any(c['purpose']=='mpi_run' and c['stop']['verified'] and c['stop']['root_exit_code']==98 for c in children)
            assert query("SELECT id FROM artifact_publications WHERE status='committed' ORDER BY id")==publications_before
            assert query('SELECT case_id,kind,active_publication_id FROM artifact_publication_heads ORDER BY case_id,kind')==heads_before
            assert done['attempt']['outputs']['publication']['status']=='failed'
            assert done['attempt']['retry_of_attempt_id']==source_done['attempt']['id']
            assert resume(flow['id'],body,key)['id']==new['id'] and query('SELECT COUNT(*) FROM jobs')[0][0]==count+1
            report['checks'].append({'kind':'actual_original_64MiB_trace_limit_classified',
                'job_id':new_job,'reason_code':done['attempt']['reason_code'],'native_resource_exit':98,
                'no_new_committed_publication':True,'publication_heads_unchanged':True,
                'workflow_status':failed_run['status'],'original_budget_unchanged':True})
            report['expected_resource_failure_verified']=True
            report['functional_passed']=True
            return report,api.root
        done=stopped(new_job);waiting,_=workflow(new['id'],'confirm_D','awaiting_confirmation')
        assert done['attempt']['handler']['key']==source_done['attempt']['handler']['key']
        assert done['attempt']['number']==2 and done['attempt']['root_job_id']==source_job
        assert done['attempt']['retry_of_attempt_id']==source_done['attempt']['id']
        assert done['attempt']['outputs']['publication']['status']=='committed'
        assert query('SELECT COUNT(*) FROM jobs')[0][0]==count+1
        assert resume(flow['id'],body,key)['id']==new['id'] and query('SELECT COUNT(*) FROM jobs')[0][0]==count+1
        if mode=='udf':
            prefix=next(step for step in waiting['steps'] if step['key']=='udf-build')
            assert prefix['execution_mode']=='reuse' and not prefix['attempts'] and prefix['publication']['id']==build_pub['id']
            reference=waiting['resumption']['reused']['udf-build']
            assert reference['attempt_id']==built['attempt']['id'] and reference['id']==build_pub['id']
            libraries=query("SELECT id FROM jobs WHERE type='udf'");assert len(libraries)==1
            library=work(new_job)/'UDF/myFunctionObject/libmyFunctionObject.so'
            assert library.read_bytes()[:4]==b'\x7fELF'
            log=api.call('GET',api.prefix+'/raw?path=C-run/case-1/udf-solve.log',raw=True)
            assert b'N6_UDF_RETRY_ACTUAL' in log
        import numpy as np
        import vtk
        from vtk.util.numpy_support import vtk_to_numpy
        native=work(new_job)/'C-run/case-1';reader=vtk.vtkOpenFOAMReader();reader.SetFileName(str(native/'case-1.foam'))
        reader.CreateCellToPointOff();reader.UpdateInformation();reader.EnableAllCellArrays();reader.UpdateTimeStep(.5)
        cells=1600 if mode=='mpi' else 400
        grid=reader.GetOutput().GetBlock(0);assert grid.GetNumberOfCells()==cells
        fields={name:vtk_to_numpy(grid.GetCellData().GetArray(name)).copy() for name in ('U','p')}
        for name,a in fields.items():assert np.isfinite(a).all() and float(np.max(np.abs(a)))>0
        confirm(new['id'],'confirm_D');workflow(new['id'],status='succeeded')
        after={table:query('SELECT * FROM '+table+' WHERE '+column+'=:id ORDER BY id',{'id':flow['id'] if table in {'workflow_runs','workflow_steps'} else source_job})
            for table,column in [('workflow_runs','id'),('workflow_steps','run_id'),('jobs','id'),('job_attempts','job_id')]}
        assert old_rows==after
        report['checks'].append({'kind':'actual_registered_sealed_retry_and_explicit_D_completion','new_workflow':new['id'],
            'new_job':new_job,'old_history_unchanged':True,'new_job_count':1,'UDF_no_recompile':mode=='udf',
            'original_build_publication':build_pub,'native_cells':cells,'finite_nonzero_fields':list(fields),
            'field_max_abs':{name:float(np.max(np.abs(a))) for name,a in fields.items()},'actual_ELF':mode=='udf'})
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
        (api.root/'professional-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf8')
        print('WORKFLOW_PROFESSIONAL_RESULT '+str(api.root/'professional-result.json'),flush=True)
    return report,api.root


def selected(args,database_url=None):return with_storage(args,database_url,verify_callback=verify)


def run():
    sys.path.insert(0,str(ROOT/'platform/tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--mode',choices=['mpi','udf'],required=True);parser.add_argument('--postgres',action='store_true');parser.add_argument('--s3',action='store_true')
    parser.add_argument('--write-interval',type=int,choices=[10,100],default=100,
        help='Every 10 (100 output times) or 100 (10 output times) steps; numerical step and endpoint stay fixed')
    parser.add_argument('--expect-trace-limit',action='store_true',help='Require an actual original64MiB trace resource failure, without reducing limits')
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf8'))
    if args.expect_trace_limit:assert args.mode=='mpi' and args.write_interval==10
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    if args.postgres:return asyncio.run(postgres(args,verify_callback=selected))
    report,_=selected(args);return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
