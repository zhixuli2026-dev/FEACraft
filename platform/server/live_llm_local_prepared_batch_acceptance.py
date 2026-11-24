"""Actual local BatchState delivery, native cancellation, restart and readback.

The independent delivery scanners are stopped through their real lifecycle.
Calls to start_job are observed and forwarded unchanged. Native and model
results are never substituted; the common wrapper uses the real DeepSeek API.
"""
import ast,asyncio,copy,hashlib,json,sys,time,uuid
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from urllib.parse import quote

from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_failure_fallback_acceptance import wait_for,kill_owned_api
from live_llm_mpi_workflow_acceptance import FIXTURE as MPI_FIXTURE,vectors
from live_llm_udf_cancel_recovery_acceptance import native_inspect
from live_llm_workflow_batch_professional_acceptance import confirmed_dispatch_pending_kind
from testsupport import IsolatedServer


EXTRA=r'''
import asyncio,hashlib,inspect,json
from pathlib import Path
from app.models import Plan,DurableEvent,JobSubmissionRequest,ArtifactPublicationHead
from app.services import batch_jobs,job_delivery,jobrunner,job_worker_identity
from app import storage
from app.routers.jobs import _read_batch,_load_batch_rows
local_start_calls=[]
local_original_start=jobrunner.start_job
async def local_observed_start(*args,**kwargs):
 bound=inspect.signature(local_original_start).bind(*args,**kwargs);bound.apply_defaults()
 values=bound.arguments
 row={'job_id':values['job_id'],'spec':{'command':values['command'],'case_prefix':values['case_s3_prefix'],
  **{key:values[key] for key in ['runtime','argv','timeout_seconds','max_log_bytes','evidence_run_dir']}}}
 local_start_calls.append(row)
 try:return await local_original_start(*args,**kwargs)
 except Exception as error:
  row['error']={'type':type(error).__name__,'code':getattr(error,'code',None),'message':str(error)}
  raise
 finally:
  (Path(__file__).parent/'local-actual-launches.json').write_text(json.dumps(local_start_calls,indent=2),encoding='utf8')
jobrunner.start_job=local_observed_start

@app.on_event('startup')
async def stop_independent_local_delivery_scanner():
 app.state.delivery_stop.set();await app.state.delivery_task

@app.get('/api/v1/testing/local-batch-role')
async def local_batch_role(user=Depends(get_current_user)):
 from app.services.distributed_queue import distributed_enabled
 return {'pid':os.getpid(),'delivery_stopped':app.state.delivery_task.done(),
  'distributed':distributed_enabled()}

@app.post('/api/v1/testing/local-batch-quota')
async def local_batch_quota(body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 amount=body['job_concurrent'];assert type(amount) is int and amount in {1,2}
 plan=await db.scalar(select(Plan).where(Plan.code=='free'));values=json.loads(plan.quotas)
 values['job_concurrent']=amount;plan.quotas=json.dumps(values);await db.commit()
 return {'job_concurrent':amount}

@app.get('/api/v1/cases/{case_id}/testing/local-batch/{identifier}')
async def local_batch_evidence(case_id:int,identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=False);manifest=_read_batch(case,identifier)
 rows=await _load_batch_rows(db,manifest);by_id={row.id:row for row in rows}
 rows=[by_id[identifier] for identifier in manifest['job_ids']];result=[]
 def document(row):
  return {column.name:(value.isoformat() if hasattr(value,'isoformat') else value)
   for column in row.__table__.columns for value in [getattr(row,column.name)]}
 for job in rows:
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
  submission=await db.scalar(select(JobSubmissionRequest).where(JobSubmissionRequest.job_id==job.id))
  spec=json.loads(attempt.input_manifest_json)['execution_spec'];delivery=await job_delivery.intent(db,job,attempt)
  calls=[row for row in local_start_calls if row['job_id']==job.id]
  result.append({'job_id':job.id,'status':job.status,'attempt_id':attempt.id,'phase':attempt.phase,
   'claimed':attempt.claim_token is not None,'mode':delivery['mode'],'spec':spec,
   'submission_spec_equal':json.loads(submission.execution_spec_json)==spec,
   'launch_calls':calls,'worker':job_worker_identity.document(job,attempt)['process'] if attempt.claim_token else None,
   'started_at':attempt.started_at.isoformat() if attempt.started_at else None,
   'SQL_history':{'job':document(job),'attempt':document(attempt),'submission':document(submission)}})
 state=batch_jobs.active_batch(identifier)
 events=list(await db.scalars(select(DurableEvent).where(DurableEvent.case_id==case.id,
  DurableEvent.type=='job.batch_cancel_requested').order_by(DurableEvent.seq)))
 return {'jobs':result,'active_local_batch':state is not None,
  'active_process_jobs':sorted(proc.job_id for proc in state.processes) if state else [],
  'cancel_events':[json.loads(row.payload_json) for row in events if json.loads(row.payload_json)['batch_id']==identifier]}

@app.post('/api/v1/cases/{case_id}/testing/local-batch-heads')
async def local_batch_heads(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=False);result=[]
 for item in body['members']:
  row=await db.get(ArtifactPublicationHead,(case.id,item['kind']));path=item['run_dir']+'/0.5/U'
  result.append({'kind':item['kind'],'head':row.active_publication_id if row else None,
   'field_sha256':hashlib.sha256(storage.get_bytes(case.s3_prefix,path)).hexdigest() if storage.exists(case.s3_prefix,path) else None})
 return result
'''
FIXTURE=MPI_FIXTURE+EXTRA


def peer(api,store,role):
    value=IsolatedServer('local-prepared-'+role,app='local_prepared_peer:app')
    shared={k:v for k,v in api.env.items() if k.startswith('VIBEFLOW_')}
    shared['VIBEFLOW_DISTRIBUTED_JOBS']='false'
    if store:
        shared.update(store.settings());shared['VIBEFLOW_STORAGE_FS_ROOT']=str(value.root/'s3-cache')
    code='import os\nos.environ.update('+repr(shared)+')\nfrom app.main import app\n'+FIXTURE
    ast.parse(code);(value.root/'local_prepared_peer.py').write_text(code,encoding='utf8')
    value.token=api.token;value.case_id=api.case_id;value.prefix=api.prefix
    return value


def scenario(api,*,store=None,expected_legacy_failure=False,**_):
    (api.root/'local-prepared-entry-source.py').write_bytes(Path(__file__).read_bytes())
    api.create_case('Actual local prepared MPI UDF serial batch')
    prefix=api.prefix
    observer=copy.copy(api)
    source=peer(api,store,'source');recovery=peer(api,store,'recovery')
    modes=['mpi','udf','serial'];runs={mode:'C-run/local-'+mode for mode in modes}
    checks=[];batches=[];observations=[];cancelled=[];cancelled_history={}
    short=cavity_2d()['C-run/case-1/system/controlDict']
    long=short.replace('endTime         0.5;','endTime         1000000;').replace('writeInterval   20;','writeInterval   1000000000;')
    assert long!=short
    def record(row):
        checks.append(row);(api.root/'local-prepared-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
        print('LOCAL_PREPARED_STAGE '+row['stage'],flush=True)
    def evidence(server,batch):return server.call('GET',prefix+'/testing/local-batch/'+batch['id'])
    def drain(server,jid,expected='succeeded'):
        server.wait_job(jid,expected=expected,timeout=360)
        def settled():
            row=server.call('GET','/jobs/'+str(jid))
            if row['attempt']['process_identity'] is None and expected=='cancelled':return row
            if server.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'] and not row['completion_pending']:return row
        return wait_for(settled,'Actual local member did not drain',180)
    def request(order,cap=2):
        operations=[]
        for mode in order:
            row={'kind':mode,'run_dir':runs[mode],'timeout_seconds':300,'max_log_bytes':67108864}
            if mode in {'mpi','serial'}:row['solver']='icoFoam'
            if mode=='mpi':row['subdomains']=2
            if mode=='udf':row['name']='myFunctionObject'
            operations.append(row)
        return {'operations':operations,'max_concurrent':cap}
    def submit(server,body,key=None):
        key=key or uuid.uuid4().hex
        # Bound the client observation separately from the unchanged server budget.
        import httpx
        with httpx.Client(base_url=server.base+'/api/v1',trust_env=False,
                headers={'Authorization':'Bearer '+server.token},timeout=httpx.Timeout(180,connect=10)) as client:
            response=client.post(prefix+'/jobs:batch',json=body,headers={'Idempotency-Key':key})
            assert response.status_code==202,('Actual local batch admission refused',response.status_code,response.text[:1200])
            batch=response.json()
        batches.append((batch,body,key));return batch
    def completed(server,batch):
        maximum=0;actual_owner=None
        def observed():
            nonlocal maximum,actual_owner
            proof=evidence(server,batch)
            active=[row for row in proof['jobs'] if row['phase'] not in {'queued','succeeded','failed','cancelled','interrupted'}]
            maximum=max(maximum,len(active))
            if active:
                assert proof['active_local_batch']
                for row in active:
                    assert row['worker']['pid']==source_pid if server is source else row['worker']['pid']==recovery_pid
                actual_owner=proof
            current=server.call('GET',prefix+'/job-batches/'+batch['id'])
            if current['status'] in {'failed','cancelled'}:
                (api.root/'local-prepared-failed-batch.json').write_text(json.dumps({'batch':current,'evidence':proof},indent=2),encoding='utf8')
                raise AssertionError('Actual local prepared batch failed; evidence retained')
            return current if current['status']=='succeeded' else None
        wait_for(observed,'Actual local prepared batch did not finish',480)
        final=evidence(server,batch)
        for row in final['jobs']:
            assert row['mode']=='local' and row['submission_spec_equal'] and row['claimed']
            assert len(row['launch_calls'])==1 and row['launch_calls'][0]['spec']==row['spec']
            assert row['spec']['timeout_seconds']==300 and row['spec']['max_log_bytes']==67108864
        assert actual_owner is not None and maximum<=batch['max_concurrent']
        return final,maximum,actual_owner
    def proofs(server,batch):
        fields=[];result=[]
        for operation,item in zip(batch['operations'],batch['jobs']):
            mode=operation['kind'];job=drain(server,item['id']);pub=job['attempt']['outputs']['publication']
            assert pub['status']=='committed' and job['attempt']['outputs']['completion']['alias_error'] is None
            descriptor=server.call('GET','/testing/workflow-publication/'+pub['id'])['manifest']['descriptor']
            assert descriptor['handler']=={'mpi':'mpi.v1','udf':'udf.v1','serial':'prepared-solve.v1'}[mode]
            if mode=='mpi':assert set(descriptor['receipt']['native']['processors'])=={'processor0','processor1'}
            if mode=='udf':assert set(descriptor['receipt']['native']['steps'])=={'load','solve'}
            raw=server.call('GET',prefix+'/raw?path='+quote(runs[mode]+'/0.5/U'),raw=True);fields.append(vectors(raw))
            result.append({'mode':mode,'job_id':job['id'],'kind':pub['kind'],'publication_id':pub['id'],
                'run_dir':runs[mode],'actual_cells':400,'final_time':.5,'field_sha256':hashlib.sha256(raw).hexdigest()})
        relative=max(abs(a-b) for other in fields[1:] for row,other_row in zip(fields[0],other) for a,b in zip(row,other_row))
        assert relative<=1e-5
        return result,relative
    def heads(server,members):return server.call('POST',prefix+'/testing/local-batch-heads',{'members':members})
    def actual(server,jid,mode,library_sha):
        def observed():
            job=server.call('GET','/jobs/'+str(jid))
            assert job['status'] not in {'failed','cancelled','succeeded'},(mode,job['status'])
            if job['status']=='queued':return None
            if mode=='serial':ident=job['attempt']['process_identity']
            else:
                children=[c for c in job['attempt']['child_processes']['children'].values()
                    if c['purpose']==('mpi_run' if mode=='mpi' else 'udf_run') and c['phase']=='running']
                if not children:return None
                assert len(children)==1;ident=children[0]['identity']
            if not ident or not ident.get('native'):return None
            native=ident['native'];members=native_inspect(native['distribution'],{'root':native['process']})
            solvers=[row for row in members if row['comm']=='icoFoam']
            if len(solvers)!=(2 if mode=='mpi' else 1):return None
            assert any(row['comm']=='strace' for row in members)
            if mode=='udf' and not any(lib['sha256']==library_sha for lib in solvers[0]['loaded_UDF_libraries']):return None
            return {'job_id':jid,'mode':mode,'distribution':native['distribution'],'members':members}
        value=wait_for(observed,'Actual local '+mode+' solver not observed',120);observations.append(value);return value
    primary=None
    try:
        with source:
            roles=[api.call('GET','/testing/local-batch-role'),source.call('GET','/testing/local-batch-role')]
            assert all(row['delivery_stopped'] and not row['distributed'] for row in roles)
            source_pid=roles[1]['pid']
            original=source.call('GET',prefix+'/caseDict')
            body=set_block(original['content'],'mesh',{'source':'blockMesh','qualityTargets':{'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
            body=set_block(body,'numericalMethod',{'solver':'icoFoam'})
            write(source,'PUT',prefix+'/caseDict',{'content':body,'expected_revision':original['dict_revision']})
            for path,body in cavity_2d().items():put(source,path,body)
            seeded=put(source,'A-geo/geometry.py','import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n')
            if seeded.get('auto_preview_job'):drain(source,seeded['auto_preview_job'])
            revision=source.call('GET',prefix+'/caseDict')['dict_revision']
            flow=source.call('POST',prefix+'/workflow-runs',{'target':'mesh','expected_revision':revision,
                'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':180,'max_log_bytes':16777216}})
            for key in ['confirm_B','confirm_C']:
                def waiting():
                    current=source.call('GET','/workflow-runs/'+flow['id']);assert current['status'] not in {'failed','cancelled','interrupted'}
                    step=next(s for s in current['steps'] if s['key']==key)
                    return (current,step) if step['status']=='awaiting_confirmation' else None
                current,step=wait_for(waiting,'Actual baseline did not reach '+key,300)
                for producer in current['steps']:
                    if producer['status']=='succeeded':
                        for attempt in producer['attempts']:drain(source,attempt['job_id'])
                answer=write(source,'POST',prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
                    'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
                pending=confirmed_dispatch_pending_kind(answer)
                if pending:record({'stage':'actual_committed_confirmation_wait','kind':pending,'answer':answer})
            source.call('POST',prefix+'/udf/scaffold',{'name':'myFunctionObject','objective':'Actual local BatchState regression'})
            built=source.call('POST',prefix+'/udf:build',{'name':'myFunctionObject'});drain(source,built['job_id'])
            library=source.call('GET',prefix+'/raw?path='+quote('UDF/myFunctionObject/libmyFunctionObject.so'),raw=True)
            assert library[:4]==b'\x7fELF';library_sha=hashlib.sha256(library).hexdigest()
            for mode in modes:
                created=write(source,'POST',prefix+'/runs:derive',{'base':'C-run/case-1','name':'local-'+mode})
                assert created['created']==runs[mode]
            source.call('POST','/testing/local-batch-quota',{'job_concurrent':2})
            first=submit(source,request(modes))
            if expected_legacy_failure:
                def failed():
                    proof=evidence(source,first)
                    return proof if all(len(row['launch_calls'])==1 and row['launch_calls'][0].get('error') for row in proof['jobs']) else None
                proof=wait_for(failed,'Legacy local batch did not reproduce the actual rejection',60)
                assert all(not row['claimed'] and row['submission_spec_equal'] and len(row['launch_calls'])==1
                    and row['launch_calls'][0]['spec']!=row['spec']
                    and row['launch_calls'][0]['error']['code']=='invalid_request' for row in proof['jobs'])
                (api.root/'local-prepared-legacy-counterexample.json').write_text(json.dumps(
                    {'expected_engineering_failure':True,'actual_registered_modes':modes,'evidence':proof,'mock_llm_calls':0},indent=2),encoding='utf8')
                raise RuntimeError('Expected actual legacy BatchState specification mismatch reproduced for MPI/UDF/serial; zero model calls')
            proof,maximum,owner=completed(source,first);members,error=proofs(source,first)
            record({'stage':'actual_local_three_mode_success','roles':roles,'delivery':proof,'maximum_active':maximum,
                'actual_local_owner':owner,'native_results':members,'relative_field_error':error})
            for mode in modes:put(source,runs[mode]+'/system/controlDict',long)
            for mode in modes:
                order=[mode,*[other for other in modes if other!=mode]];batch=submit(source,request(order,1))
                running=batch['jobs'][0]['id'];queued=[row['id'] for row in batch['jobs'][1:]]
                observed=actual(source,running,mode,library_sha);proof=evidence(source,batch)
                assert proof['active_local_batch'] and proof['active_process_jobs']==[running]
                assert all(row['mode']=='local' for row in proof['jobs'])
                assert all(not row['claimed'] and row['started_at'] is None for row in proof['jobs'] if row['job_id'] in queued)
                before=heads(source,members)
                assert [row['field_sha256'] for row in before]==[row['field_sha256'] for row in members]
                endpoint=prefix+'/job-batches/'+batch['id']+':cancel'
                with ThreadPoolExecutor(max_workers=2) as pool:
                    replies=[future.result() for future in [pool.submit(server.call,'POST',endpoint,{}) for server in [source,api]]]
                assert all(row['status']=='cancelled' for row in replies)
                for job in batch['jobs']:drain(source,job['id'],'cancelled')
                probes=native_inspect(observed['distribution'],{'probe':[row['identity'] for row in observed['members']]})
                assert all(row['state']=='exited' for row in probes);observed['actual_exit_probes']=probes
                final=evidence(source,batch);assert len(final['cancel_events'])==1
                assert set(final['cancel_events'][0]['jobs'])==set([running,*queued])
                assert all(not row['claimed'] and row['started_at'] is None and not row['launch_calls'] for row in final['jobs'] if row['job_id'] in queued)
                assert heads(source,members)==before
                wait_for(lambda:not evidence(source,batch)['active_local_batch'],
                    'Actual local batch process waiters did not complete',120)
                settled=source.call('GET',prefix+'/job-batches/'+batch['id']);source.call('POST',endpoint,{})
                assert source.call('GET',prefix+'/job-batches/'+batch['id'])==settled
                cancelled_history[batch['id']]=[row['SQL_history'] for row in evidence(source,batch)['jobs']]
                (api.root/('local-cancelled-before-'+batch['id']+'.json')).write_text(json.dumps(
                    {'public':settled,'SQL_history':cancelled_history[batch['id']]},indent=2),encoding='utf8')
                cancelled.append(settled)
                record({'stage':'actual_local_'+mode+'_running_cancel','delivery':final,'actual_native':observed,
                    'queued_never_claimed':queued,'old_heads_and_fields_unchanged':True})
            for mode in modes:put(source,runs[mode]+'/system/controlDict',short)
            (api.root/'local-source-kill.json').write_text(json.dumps({'actual_pid':source_pid,
                'launcher_pid':kill_owned_api(source,source_pid),'actual_exit_verified':True}),encoding='utf8')
        with recovery:
            role=recovery.call('GET','/testing/local-batch-role');recovery_pid=role['pid']
            assert role['delivery_stopped'] and not role['distributed']
            for batch,body,key in list(batches):
                replay=recovery.call('POST',prefix+'/jobs:batch',body,expect=202,extra_headers={'Idempotency-Key':key})
                assert replay['id']==batch['id'] and [row['id'] for row in replay['jobs']]==[row['id'] for row in batch['jobs']]
            comparisons=[]
            for old in cancelled:
                after=recovery.call('GET',prefix+'/job-batches/'+old['id'])
                history=[row['SQL_history'] for row in evidence(recovery,old)['jobs']]
                comparison={'before':old,'after':after,'before_SQL_history':cancelled_history[old['id']],
                    'after_SQL_history':history,'public_process_phase_changes':[]}
                (api.root/('local-restart-history-'+old['id']+'.json')).write_text(json.dumps(comparison,indent=2),encoding='utf8')
                assert history==cancelled_history[old['id']],'Actual API restart changed persisted cancelled Job/attempt/submission history'
                normalized=copy.deepcopy(old)
                for previous,current in zip(normalized['jobs'],after['jobs']):
                    if previous['phase']!=current['phase']:
                        # Existing API exposes the local process lifecycle as
                        # completed while registered; a new API has only the
                        # original durable cancelled attempt. Record the sole
                        # view change without accepting any history mutation.
                        assert previous['phase']=='completed' and current['phase']=='cancelled'
                        comparison['public_process_phase_changes'].append({'job_id':current['id'],
                            'before':previous['phase'],'after':current['phase']})
                        previous['phase']=current['phase']
                assert normalized==after,'Actual cancelled public history changed beyond local-process phase projection'
                (api.root/('local-restart-history-'+old['id']+'.json')).write_text(json.dumps(comparison,indent=2),encoding='utf8')
                comparisons.append(comparison)
            replacement=submit(recovery,request(modes));proof,maximum,owner=completed(recovery,replacement)
            fresh,error=proofs(recovery,replacement)
            assert set(row['job_id'] for row in fresh).isdisjoint(row['job_id'] for row in members)
            assert all(new['publication_id']!=old['publication_id'] for new,old in zip(fresh,members))
            record({'stage':'actual_API_exit_idempotency_and_explicit_new_local_batch','role':role,'delivery':proof,
                'maximum_active':maximum,'actual_local_owner':owner,'native_results':fresh,'relative_field_error':error,
                'independent_empty_S3_cache':bool(store),'immutable_history_comparisons':comparisons})
        report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','checks':checks,
            'scope':'Actual non-Redis local BatchState exact-spec delivery, three 400-cell native modes, all three running/queued cancellation, API exit, idempotency and explicit new batch. Real model only performs independent evidence readback.'}
        (api.root/'local-prepared-batch.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        api.create_case('Independent real local prepared batch evidence readback');return report
    except BaseException as error:
        primary=error;raise
    finally:
        errors=[]
        for batch,_,_ in batches:
            try:
                current=observer.call('GET',prefix+'/job-batches/'+batch['id'])
                if current['status'] not in {'succeeded','failed','cancelled'}:
                    observer.call('POST',prefix+'/job-batches/'+batch['id']+':cancel',{})
                    for item in batch['jobs']:drain(observer,item['id'],'cancelled')
            except Exception as error:errors.append({'batch_id':batch['id'],'error':type(error).__name__})
        (api.root/'local-prepared-cleanup.json').write_text(json.dumps({'errors':errors,'primary_preserved':primary is not None},indent=2),encoding='utf8')
        if errors and primary is None:raise RuntimeError('Actual local prepared cleanup failed; inspect receipt')


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16,
        environment_overrides={'VIBEFLOW_DISTRIBUTED_JOBS':'false'}))
