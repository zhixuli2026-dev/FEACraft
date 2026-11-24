"""Real cavity cancellation, authenticated restart, uninterrupted comparison and real model readback."""
import asyncio,hashlib,json,re,sys,time,uuid
from functools import partial
from pathlib import Path
from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main,repeat

TIMING_FIXTURE=r'''
import cProfile,hashlib,os,time,uuid
from pathlib import Path
from app.services import artifact_publication as measured_publication,publication_alias_objects as measured_alias,publication_source_validation as measured_source,job_execution as measured_execution,solve_handler as measured_solve
measurement_root=Path(__file__).parent
from app.services import attempt_objects as measured_attempts,publication_objects as measured_objects,s3_live_store as measured_store
measurement_repository=Path(measured_execution.__file__).resolve().parents[4]
(measurement_root/'native-resume-source-digests.json').write_text(json.dumps({
 module.__name__:{'path':Path(module.__file__).resolve().relative_to(measurement_repository).as_posix(),
 'sha256':hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
 for module in (measured_publication,measured_alias,measured_source,measured_execution,measured_solve,measured_attempts,measured_objects,measured_store)
},indent=2),encoding='utf8')
def timing_receipt(label,started,error,profile=None):
 value={'stage':label,'seconds':round(time.monotonic()-started,4),'error':error,'profile':profile}
 with (measurement_root/'native-resume-stages.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps(value)+'\n')
def instrument_sync(target,name,profile=False):
 original=getattr(target,name);label=target.__name__+'.'+name
 def observed(*args,**kwargs):
  started=time.monotonic();error=None;profiler=cProfile.Profile() if profile else None
  trace='native-profile-'+uuid.uuid4().hex+'.pstats' if profile else None
  try:return profiler.runcall(original,*args,**kwargs) if profiler else original(*args,**kwargs)
  except BaseException as failure:error=type(failure).__name__;raise
  finally:
   if profiler:profiler.dump_stats(str(measurement_root/trace))
   timing_receipt(label,started,error,trace)
 setattr(target,name,observed)
def instrument_async(target,name):
 original=getattr(target,name);label=getattr(target,'__name__',str(target))+'.'+name
 async def observed(*args,**kwargs):
  started=time.monotonic();error=None
  try:return await original(*args,**kwargs)
  except BaseException as failure:error=type(failure).__name__;raise
  finally:timing_receipt(label,started,error)
 setattr(target,name,observed)
profile_enabled=os.environ.get('VIBEFLOW_ACCEPTANCE_PROFILE')=='1'
(measurement_root/'native-resume-observation.json').write_text(json.dumps({'cprofile_enabled':profile_enabled,
 'timings_enabled':True,'production_timeouts_unchanged':True}),encoding='utf8')
instrument_sync(measured_publication,'verify_immutable_files',profile_enabled)
instrument_sync(measured_alias,'prepare',profile_enabled)
instrument_sync(measured_solve.SolveHandler,'source',profile_enabled)
instrument_async(measured_publication,'verify_files_for_finish')
instrument_async(measured_execution,'_finish_owned')
for stage in ('acquire','inspect','stop_renewal'):instrument_async(measured_source.SourceValidation,stage)

from fastapi import Depends,HTTPException
from sqlalchemy import select
from app.db import get_db
from app.deps import get_current_user
from app.models import Job,JobAttempt,DurableEvent
from app.routers.workspaces import get_owned_case
@app.get('/api/v1/testing/native-budget/{job_id}')
async def observed_native_budget(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id)
 if job is None:raise HTTPException(404)
 await get_owned_case(job.case_id,user,db,write=False)
 attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
 events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
  DurableEvent.type.in_(['attempt.resource_usage','attempt.storage_usage'])).order_by(DurableEvent.id)))
 return {'job_id':job.id,'attempt_id':attempt.id,'phase':attempt.phase,'reason_code':attempt.reason_code,
  'events':[{'type':event.type,'payload':json.loads(event.payload_json)} for event in events]}
'''


def scenario(api,*,store=None,source_cache_mode='attempt',repeated_continuation=False,workflow_continuation=False,
             wall_seconds=120,extra_negative_checks=None,positive_override=None,restart_policy=None,**_):
    case_root=None
    if store is not None:
        original_open=api.opener.open;timings=[]
        def measured(request,*args,**kwargs):
            kwargs['timeout']=120;started=time.monotonic()
            try:return original_open(request,*args,**kwargs)
            finally:
                timings.append({'method':request.get_method(),'path':request.selector,'seconds':round(time.monotonic()-started,3)})
                (api.root/'native-resume-http-timings.json').write_text(json.dumps(timings,indent=2),encoding='utf8')
        api.opener.open=measured
        original_call=api.call
        def observed_call(method,path,*args,**kwargs):
            value=original_call(method,path,*args,**kwargs)
            if method=='GET' and re.fullmatch(r'/jobs/[1-9][0-9]*',path) and isinstance(value,dict):
                (api.root/('native-resume-job-'+path.rsplit('/',1)[1]+'.json')).write_text(json.dumps(value,indent=2),encoding='utf8')
                if value.get('status') in {'failed','interrupted'}:
                    usage=original_call('GET','/testing/native-budget/'+str(value['id']))
                    (api.root/('native-resume-budget-'+str(value['id'])+'.json')).write_text(json.dumps(usage,indent=2),encoding='utf8')
            return value
        api.call=observed_call
    def write(method,path,body,*,key=None):
        headers={'Idempotency-Key':key} if key else {}
        deadline=time.monotonic()+65
        while True:
            result=api.call(method,path,body,expect=(200,409) if store else 200,extra_headers=headers)
            detail=result.get('detail',{})
            if not detail:
                return result
            assert isinstance(detail,dict) and detail.get('code') in {
                'storage_quota_busy','file_sync_busy','validation_busy'},result
            if time.monotonic()>=deadline:
                raise TimeoutError('Native checkpoint setup remained busy at '+path)
            time.sleep(.2)
    saved=api.call('GET',api.prefix+'/caseDict')
    text=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':
        {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
    text=set_block(text,'numericalMethod',{'solver':'icoFoam'})
    text=set_block(text,'postProcessing',{'runDir':'C-run/case-1'})
    if restart_policy is not None:
        text=set_block(text,'execution',{'restartPolicy':restart_policy})
    write('PUT',api.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
    write_steps=50 if store is not None and restart_policy=='latestWritten' else 10
    first_label='0.025' if write_steps==50 else '0.005'
    checkpoint_relative=f'C-run/case-1/{first_label}'
    for name,content in cavity_2d().items():
        if name.endswith('/controlDict'):
            content=content.replace('deltaT          0.005;','deltaT          0.0005;').replace(
                'writeInterval   20;',f'writeInterval   {write_steps};')
        write('PUT',api.prefix+'/file',{'path':name,'content':content})
    geometry=write('PUT',api.prefix+'/file',{'path':'A-geo/geometry.py',
        'content':"import cadquery as cq\ndef build_geometry(ctx):return {'box':cq.Workplane('XY').box(1,1,1)}\n"})
    if geometry.get('auto_preview_job'):api.wait_job(geometry['auto_preview_job'])
    original_control=api.call('GET',api.prefix+'/raw?path=C-run/case-1/system/controlDict',raw=True)
    revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
    flow=write('POST',api.prefix+'/workflow-runs',{'target':'solve','run_dir':'C-run/case-1',
        'expected_revision':revision,'budget':{'timeout_seconds':wall_seconds,'max_log_bytes':16777216}},
        key=uuid.uuid4().hex)
    def detail():return api.call('GET','/workflow-runs/'+flow['id'])
    def wait_step(key,status):
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            value=detail();step=next(s for s in value['steps'] if s['key']==key)
            if step['status']==status:return value,step
            if value['status'] in {'failed','interrupted','cancelled'}:raise AssertionError(json.dumps(value)[:2000])
            time.sleep(.1)
        raise TimeoutError('Native workflow did not reach '+key+':'+status)
    def confirm(key):
        value,step=wait_step(key,'awaiting_confirmation')
        for producer in value['steps']:
            if producer['status']=='succeeded' and producer['attempts']:api.wait_job(producer['attempts'][-1]['job_id'])
        return write('POST',api.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
            'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
    confirm('confirm_B');confirm('confirm_C')
    _,step=wait_step('solve','running');identifier=step['attempts'][-1]['job_id']
    job=api.call('GET','/jobs/'+str(identifier));manifest=job['attempt']['workspace']
    case_root=api.root/'files'/manifest['storage']['prefix'] if store is not None else next((api.root/'files/workspaces').glob('*/cases/'+str(api.case_id)))
    work=case_root/manifest['run_path']
    checkpoint=work/checkpoint_relative
    deadline=time.monotonic()+90
    while not all((checkpoint/name).is_file() for name in ('U','p')):
        current=api.call('GET','/jobs/'+str(identifier))
        if current['status'] not in {'queued','running'}:raise AssertionError('Solver ended before checkpoint cancellation: '+json.dumps(current)[:1000])
        if time.monotonic()>deadline:raise TimeoutError('Actual solver did not write '+first_label+' fields')
        time.sleep(.02)
    current=detail();api.call('POST','/workflow-runs/'+flow['id']+':cancel',{'expected_version':current['version']})
    deadline=time.monotonic()+60
    while True:
        cancelled=api.call('GET','/jobs/'+str(identifier));option=api.call('GET','/jobs/'+str(identifier)+'/operations')
        resume=option['resume']
        if (resume['available'] or resume.get('checkpoint_policy')=='disabled') and cancelled['attempt'].get('process_stop'):break
        if time.monotonic()>deadline:raise TimeoutError('Actual solver cancellation did not drain: '+json.dumps(option))
        time.sleep(.1)
    assert cancelled['status']=='cancelled' and cancelled['attempt']['process_stop']['verified']
    invocation=json.loads((work/'C-run/case-1/solve-invocation.json').read_text(encoding='utf8'))
    assert invocation['state']=='prepared' and invocation['toolchain']['solver']=='icoFoam'
    assert not (work/'C-run/case-1/solve-evidence.json').exists(),'Cancelled partial execution cannot carry completion evidence'
    latest_expected=latest_observed=None
    if restart_policy=='latestWritten':
        from app.services.solve_artifacts import field_header, output_times
        available=output_times(work,'C-run/case-1')
        latest_observed=max(available,key=float)
        for label,paths in sorted(available.items(),key=lambda item:float(item[0]),reverse=True):
            selected={path.name:path for path in paths if path.parent==work/'C-run/case-1'/label}
            if not {'U','p','phi'}.issubset(selected):
                continue
            try:
                for field in selected.values():
                    field_header(field)
            except (OSError,ValueError):
                continue
            latest_expected=label
            break
        assert latest_expected is not None
    source_attempt=option['source_attempt_id'];version=option['source_version']
    body={'action':'resume','source_attempt_id':source_attempt,'source_version':version,'parameters':{'time':first_label}}
    negatives=[]
    def refused(payload):
        value=api.call('POST','/jobs/'+str(identifier)+':repeat',payload,expect=(400,409),extra_headers={'Idempotency-Key':uuid.uuid4().hex})
        detail=value.get('detail');assert not isinstance(detail,dict) or detail.get('code') not in {'storage_quota_busy','validation_busy'},value
        return value
    if restart_policy=='disabled':
        assert not option['resume']['available'] and option['resume']['reason']['code']=='checkpoint_disabled'
        for time_choice in (first_label,'latest'):
            denial=refused({**body,'parameters':{'time':time_choice}})
            assert denial['detail']['code']=='checkpoint_disabled',denial
        report={'passed':True,'policy':restart_policy,'actual_solver':'icoFoam','verified_stop':True,
            'source_attempt':source_attempt,'explicit_and_latest_denied':True,'mock_llm_calls':0}
        (api.root/'solve-resume.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        return report
    negatives.append({'invalid_time':refused({**body,'parameters':{'time':'0'}})})
    negatives.append({'unwritten_time':refused({**body,'parameters':{'time':'0.4999'}})})
    negatives.append({'missing_time':refused({**body,'parameters':{}})})
    for name,label in ((checkpoint_relative+'/U','field'),('C-run/case-1/constant/polyMesh/points','mesh'),
            ('C-run/case-1/0/p','initial_field'),('C-run/case-1/solve-invocation.json','invocation')):
        path=work/name;original=path.read_bytes()
        remote_key=manifest['storage']['prefix']+'/'+manifest['run_path']+'/'+name if store else None
        try:
            if store:store.client.put_object(Bucket=store.bucket,Key=remote_key,Body=original+b'\n// changed archived native evidence\n')
            else:path.write_bytes(original+b'\n// deliberately changed isolated native evidence\n')
            negatives.append({label:refused(body)})
        finally:
            if store:store.client.put_object(Bucket=store.bucket,Key=remote_key,Body=original)
            else:path.write_bytes(original)
    if extra_negative_checks is not None:
        negatives.extend(extra_negative_checks(api,work,body,refused))
    cache_lost=False
    if store:
        remote_key=manifest['storage']['prefix']+'/'+manifest['run_path']+'/'+checkpoint_relative+'/U'
        original=(work/checkpoint_relative/'U').read_bytes()
        try:
            store.client.delete_object(Bucket=store.bucket,Key=remote_key)
            negatives.append({'missing_archived_field':refused(body)})
        finally:store.client.put_object(Bucket=store.bucket,Key=remote_key,Body=original)
        outputs_key=manifest['storage']['prefix']+'/'+cancelled['attempt']['outputs']['object_store']['manifest_path']
        response=store.client.get_object(Bucket=store.bucket,Key=outputs_key)
        try:original=response['Body'].read()
        finally:response['Body'].close()
        try:
            store.client.put_object(Bucket=store.bucket,Key=outputs_key,Body=original+b' ')
            negatives.append({'damaged_archive_receipt':refused(body)})
        finally:store.client.put_object(Bucket=store.bucket,Key=outputs_key,Body=original)
        assert source_cache_mode in {'attempt','run','partial'}
        if source_cache_mode=='partial':
            input_key=manifest['storage']['prefix']+'/'+manifest['inputs_path']+'/C-run/case-1/0/p'
            original=(case_root/manifest['inputs_path']/'C-run/case-1/0/p').read_bytes()
            try:
                store.client.put_object(Bucket=store.bucket,Key=input_key,Body=original+b'changed remote sealed input')
                negatives.append({'damaged_archived_input':refused(body)})
                store.client.delete_object(Bucket=store.bucket,Key=input_key)
                negatives.append({'missing_archived_input':refused(body)})
            finally:store.client.put_object(Bucket=store.bucket,Key=input_key,Body=original)
            manifest_key=manifest['storage']['prefix']+'/.vibeflow/attempts/'+manifest['attempt_id']+'/manifest.json'
            response=store.client.get_object(Bucket=store.bucket,Key=manifest_key)
            try:original_manifest=response['Body'].read()
            finally:response['Body'].close()
            try:
                altered=json.loads(original_manifest);altered['source']={**altered['source'],'case_id':-1}
                store.client.put_object(Bucket=store.bucket,Key=manifest_key,Body=json.dumps(altered).encode())
                negatives.append({'archived_input_manifest_differs':refused(body)})
            finally:store.client.put_object(Bucket=store.bucket,Key=manifest_key,Body=original_manifest)
        target=(api.root/'lost-source-cache').resolve()
        source=(work if source_cache_mode in {'run','partial'} else work.parent).resolve();owned=api.root.resolve()
        assert source.is_relative_to(owned/'files') and target.is_relative_to(owned) and not target.exists()
        source.rename(target);assert not work.exists();cache_lost=True
        if source_cache_mode=='partial':
            # Explicit continuation uses a separate authenticated archive view;
            # the incomplete local seal and its unknown contents stay untouched.
            sealed=case_root/manifest['inputs_path']/'C-run/case-1/0/p'
            sealed.unlink()
            corrupt=case_root/manifest['inputs_path']/'C-run/case-1/system/controlDict'
            corrupt.write_bytes(b'ACTUAL_DAMAGED_LOCAL_CONTROL')
            unknown=case_root/manifest['inputs_path']/'unknown-local-evidence.txt'
            unknown.write_bytes(b'RETAIN_UNKNOWN_LOCAL_EVIDENCE')
            disk_manifest=case_root/('.vibeflow/attempts/'+manifest['attempt_id']+'/manifest.json')
            disk_manifest.write_bytes(b'ACTUAL_DAMAGED_LOCAL_MANIFEST')
    if positive_override is not None:
        return positive_override(api,store=store,case_root=case_root,work=work,manifest=manifest,
            source_job=identifier,source_attempt=source_attempt,original_control=original_control,flow_id=flow['id'])
    workflow_requests={}
    def continue_from(job_id,run_id,body,key):
        if not workflow_continuation:
            return repeat(api,job_id,body,key)
        path=f'/workflow-runs/{run_id}'
        if key not in workflow_requests:
            plan=api.call('GET',path+'/resume-plan')
            row=next(step for step in plan['steps'] if step.get('source_attempt_id')==body['source_attempt_id'])
            assert row['checkpoint']['available']
            payload={name:plan[name] for name in ('expected_revision','expected_version')}
            payload.update({name:row[name] for name in ('source_step_id','expected_step_version','source_attempt_id',
                                                        'expected_attempt_version')})
            payload.update(mode='checkpoint',checkpoint_time=body['parameters']['time'],confirmed=True)
            workflow_requests[key]=payload
        payload=workflow_requests[key]
        deadline=time.monotonic()+60
        while True:
            value=api.call('POST',path+':resume',payload,expect=(202,409),extra_headers={'Idempotency-Key':key})
            if 'id' in value:break
            assert value.get('detail',{}).get('code') in {'storage_quota_busy','validation_busy'},value
            if time.monotonic()>deadline:raise TimeoutError('Workflow checkpoint admission remained busy')
            time.sleep(.2)
        assert value['resumption']['source_run_id']==run_id and value['resumption']['mode']=='checkpoint'
        previous=next(step for step in value['steps'] if step['key']=='solve')['attempts']
        assert len(previous)==1
        return {'id':previous[0]['job_id'],'workflow_id':value['id']}

    start_time=float(first_label)
    if restart_policy=='latestWritten':
        assert option['resume']['supports_latest'] and option['resume']['checkpoint_policy']==restart_policy
        start_time=float(latest_expected)
        body['parameters']['time']='latest'
    key=uuid.uuid4().hex;resumed=continue_from(identifier,flow['id'],body,key)
    parent_flow=flow['id']
    parent_job=identifier;parent_attempt=source_attempt;attempt_number=2;chain=None
    if repeated_continuation:
        first_resume=resumed['id'];live=api.call('GET','/jobs/'+str(first_resume))
        first_work=case_root/live['attempt']['workspace']['run_path'];second_checkpoint=first_work/'C-run/case-1/0.01'
        deadline=time.monotonic()+90
        while not all((second_checkpoint/name).is_file() for name in ('U','p')):
            current=api.call('GET','/jobs/'+str(first_resume))
            if current['status'] not in {'queued','running'}:raise AssertionError('First continuation ended before its second checkpoint')
            if time.monotonic()>deadline:raise TimeoutError('First real continuation did not write 0.01 fields')
            time.sleep(.02)
        if workflow_continuation:
            parent_flow=resumed['workflow_id']
            current=api.call('GET','/workflow-runs/'+parent_flow)
            api.call('POST','/workflow-runs/'+parent_flow+':cancel',{'expected_version':current['version']})
        else:
            api.call('POST','/jobs/'+str(first_resume)+':cancel')
        deadline=time.monotonic()+60
        while True:
            stopped=api.call('GET','/jobs/'+str(first_resume));choices=api.call('GET','/jobs/'+str(first_resume)+'/operations')
            if stopped['attempt'].get('process_stop') and choices['resume']['available']:break
            if time.monotonic()>deadline:raise TimeoutError('Cancelled continuation did not drain for another continuation')
            time.sleep(.1)
        assert stopped['status']=='cancelled' and stopped['attempt']['process_stop']['verified']
        assert stopped['attempt']['number']==2 and stopped['attempt']['root_job_id']==identifier
        parent_job=first_resume;parent_attempt=choices['source_attempt_id'];attempt_number=3;start_time=.01
        body={'action':'resume','source_attempt_id':parent_attempt,'source_version':choices['source_version'],'parameters':{'time':'0.01'}}
        key=uuid.uuid4().hex;resumed=continue_from(parent_job,parent_flow,body,key)
        chain={'first_resume_job':first_resume,'first_resume_attempt':parent_attempt,'first_resume_cancelled_and_drained':True,
            'second_checkpoint_time':.01,'first_checkpoint_time':.005,'expected_attempt_number':3,'same_root_job':identifier}
        (api.root/'repeated-continuation-progress.json').write_text(json.dumps(chain,indent=2),encoding='utf8')
    done=api.wait_job(resumed['id'],timeout=wall_seconds+60)
    assert done['attempt']['handler']['key']=='solve.openfoam.resume'
    assert done['attempt']['number']==attempt_number and done['attempt']['root_job_id']==identifier
    assert done['attempt']['retry_of_attempt_id']==parent_attempt and done['attempt']['operation']['action']=='resume'
    assert done['attempt']['outputs']['publication']['status']=='committed'
    assert done['attempt']['process_stop']['verified']
    replay=continue_from(parent_job,parent_flow,body,key)
    assert replay['id']==resumed['id']
    if workflow_continuation:
        assert replay['workflow_id']==resumed['workflow_id']
    else:
        assert not replay['created']
    new_work=case_root/done['attempt']['workspace']['run_path']
    data=json.loads((new_work/'C-run/case-1/resume-checkpoint.json').read_text(encoding='utf8'))
    assert data['time']==start_time and data['source']['attempt_id']==parent_attempt
    for name,row in data['files'].items():assert hashlib.sha256((new_work/name).read_bytes()).hexdigest()==row['sha256']
    assert api.call('GET',api.prefix+'/raw?path=C-run/case-1/system/controlDict',raw=True)==original_control
    if store:
        assert data['archive_source']['kind']=='verified_object_outputs'
        assert not (work/checkpoint_relative/'U').exists(),'Hydrated sealed inputs cannot masquerade as restored outputs'
        assert not list((api.root/'files/.attempt-sources').glob('owned-*')),'Owned output view leaked after sealing'
        if source_cache_mode in {'run','partial'}:assert not work.exists(),'Archived continuation must not fabricate the old runtime'
        if source_cache_mode=='partial':
            assert not sealed.exists() and corrupt.read_bytes()==b'ACTUAL_DAMAGED_LOCAL_CONTROL'
            assert unknown.read_bytes()==b'RETAIN_UNKNOWN_LOCAL_EVIDENCE'
            assert disk_manifest.read_bytes()==b'ACTUAL_DAMAGED_LOCAL_MANIFEST'
    evidence=api.call('GET',api.prefix+'/solve/results');proof=evidence['evidence']
    assert proof['configuration']['start_time']==start_time and proof['parsed_log']['times'][0]>start_time
    assert proof['summary']['last_time']==.5 and not evidence['publication']['stale']
    assert detail()['status']=='cancelled' and api.call('GET','/jobs/'+str(identifier))['status']=='cancelled'
    if chain:
        assert api.call('GET','/jobs/'+str(chain['first_resume_job']))['status']=='cancelled'
    (api.root/'solve-resume-progress.json').write_text(json.dumps({
        'native_resume_committed':True,'comparison_complete':False,'source_cache_lost':cache_lost,
        'source_job':identifier,'source_attempt':source_attempt,'resume_job':resumed['id'],
        'publication':evidence['publication'],'configuration':proof['configuration'],
        'first_executed_time':proof['parsed_log']['times'][0],'last_executed_time':proof['summary']['last_time'],
        'actual_solver':'icoFoam','negative_checks':negatives,'mock_llm_calls':0},indent=2),encoding='utf8')
    # Run the same case freshly and compare numerical final fields, not just HTTP status.
    if workflow_continuation:
        path='/workflow-runs/'+resumed['workflow_id']
        deadline=time.monotonic()+60
        while True:
            current=api.call('GET',path)
            if current['status']=='awaiting_confirmation':break
            if time.monotonic()>deadline:raise TimeoutError('Continued workflow did not reach the real approval step')
            time.sleep(.15)
        assert next(step for step in current['steps'] if step['key']=='confirm_D')['status']=='awaiting_confirmation'
        api.call('POST',path+':cancel',{'expected_version':current['version']})
    option=api.call('GET','/jobs/'+str(resumed['id'])+'/operations')
    assert option['rerun']['handler']['key']=='solve.openfoam'
    rerun={'action':'rerun','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version'],
        'expected_revision':option['expected_revision'],'expected_event_sequence':option['expected_event_sequence'],
        'parameters':{'run_dir':'C-run/case-1'}}
    baseline=repeat(api,resumed['id'],rerun,uuid.uuid4().hex);baseline=api.wait_job(baseline['id'],timeout=wall_seconds+60)
    assert baseline['attempt']['handler']['key']=='solve.openfoam' and baseline['attempt']['number']==1
    fresh_work=case_root/baseline['attempt']['workspace']['run_path']
    import numpy as np
    import vtk
    from vtk.util.numpy_support import vtk_to_numpy
    def fields(root):
        reader=vtk.vtkOpenFOAMReader();reader.SetFileName(str(root/'C-run/case-1/case-1.foam'))
        reader.CreateCellToPointOff();reader.UpdateInformation();reader.EnableAllCellArrays();reader.UpdateTimeStep(.5)
        grid=reader.GetOutput().GetBlock(0);assert grid.GetNumberOfCells()==400
        return {name:vtk_to_numpy(grid.GetCellData().GetArray(name)).copy() for name in ('U','p')}
    a,b=fields(new_work),fields(fresh_work);differences={}
    for name in a:
        assert np.isfinite(a[name]).all() and np.isfinite(b[name]).all()
        differences[name]=float(np.max(np.abs(a[name]-b[name])))
        assert np.allclose(a[name],b[name],rtol=2e-3,atol=2e-5),(name,differences[name])
    report={'passed':True,'mock_llm_calls':0,'real_model_requests':0,'actual_solver':'icoFoam','actual_cells':400,
        'backend':'s3' if store else 'fs','source_cache_lost':cache_lost,'verified_archive_source':bool(store),
        'source_cache_loss_mode':source_cache_mode if store else None,
        'test_wall_budget_seconds':wall_seconds,'native_write_interval_steps':write_steps,
        'source_workflow':flow['id'],'source_job':identifier,'source_attempt':source_attempt,'resume_job':resumed['id'],
        'fresh_comparison_job':baseline['id'],'checkpoint_time':start_time,'first_executed_time':proof['parsed_log']['times'][0],
        'last_executed_time':proof['summary']['last_time'],'checkpoint_files':len(data['files']),
        'old_cancelled_history_preserved':True,'original_live_controls_unchanged':True,'original_checkpoint_bytes_unchanged':True,
        'new_attempt_same_lineage':True,'resume_publication_committed':True,'strict_idempotency':True,
        'finite_fields_match_uninterrupted':True,'field_max_abs_differences':differences,'negative_checks':negatives,
        'resume_native_toolchain':{key:invocation['toolchain'][key] for key in ('solver','openfoam','executable_sha256','distro')}}
    if chain:report['repeated_continuation']=chain
    if restart_policy is not None:
        report['policy']=restart_policy
        report['requested_checkpoint']=body['parameters']['time']
        if latest_expected is not None:
            report['latest_observed_directory']=latest_observed
            report['latest_complete_expected']=latest_expected
    if workflow_continuation:
        report['workflow_continuations']={'source':flow['id'],'last':resumed['workflow_id'],'manual_D_gate_verified':True}
    if store and source_cache_mode=='partial':
        report['partial_input_cache_recovered_in_independent_view']=True
        report['old_missing_corrupt_manifest_and_unknown_cache_preserved']=True
    (api.root/'solve-resume.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':
    restart_policy=None
    for flag,policy in (('--latest-written','latestWritten'),('--disabled-restart','disabled')):
        if flag in sys.argv:
            sys.argv.remove(flag)
            if restart_policy is not None:
                raise SystemExit('Select one checkpoint policy')
            restart_policy=policy
    workflow='--workflow-checkpoint' in sys.argv
    if workflow:
        sys.argv.remove('--workflow-checkpoint')
    store_factory=None
    if '--authenticated-s3' in sys.argv:
        sys.argv.remove('--authenticated-s3')
        if '--s3' not in sys.argv:
            raise SystemExit('--authenticated-s3 requires --s3')
        from s3testsupport import ObjectStore
        store_factory=partial(ObjectStore,authenticated=True,volume_max=16,
            executable=Path(__file__).resolve().parents[2]/'third_party/seaweed-4.52-runtime/weed')
    wall_seconds=240 if restart_policy=='latestWritten' else 120
    asyncio.run(main(selected_override=partial(scenario,restart_policy=restart_policy,workflow_continuation=workflow,
                                                wall_seconds=wall_seconds),
                     fixture_override=TIMING_FIXTURE,store_volume_max=16,store_factory=store_factory))
