"""Actual mixed MPI/UDF workflows through Redis workers and postprocess."""
import ast,asyncio,copy,json,math,sys,tempfile,time,uuid
from functools import partial
from pathlib import Path
from urllib.parse import quote
from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main as live_main,put,write
from live_llm_failure_fallback_acceptance import wait_for,kill_owned_api
from live_llm_workflow_batch_redis_acceptance import FIXTURE as REDIS_FIXTURE,peer
from live_llm_mpi_workflow_acceptance import vectors
from workflow_batch_redis_support import Broker,Workers
from testsupport import SERVER


def confirmed_dispatch_pending_kind(result):
    """Recognize only known admission waits after a committed confirmation."""
    dispatch=result.get('workflow_dispatch',{})
    if dispatch.get('status')!='pending':return None
    detail=dispatch.get('detail','');kind='capacity' if '并发作业' in detail else None
    if kind is None:
        try:reason=ast.literal_eval(detail)
        except (ValueError,SyntaxError):reason=None
        if isinstance(reason,dict) and reason.get('code') in {'storage_quota_busy','validation_busy'}:kind='storage'
    assert dispatch.get('code')=='dispatch_failed' and kind is not None,result
    return kind

RESOURCE_FIXTURE=r'''
from app.models import DurableEvent
@app.get('/api/v1/testing/professional-resource/{job_id}')
async def professional_resource(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id)
 if job is None:raise HTTPException(404)
 await get_owned_case(job.case_id,user,db,write=False)
 attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
 events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
  DurableEvent.type.in_(['attempt.resource_usage','attempt.storage_usage'])).order_by(DurableEvent.id)))
 return {'job_id':job.id,'attempt_id':attempt.id,'phase':attempt.phase,'reason_code':attempt.reason_code,
  'events':[{'type':event.type,'payload':json.loads(event.payload_json)} for event in events]}

@app.post('/api/v1/testing/professional-admission')
async def professional_admission(user=Depends(get_current_user)):
 import time
 from fastapi import HTTPException
 from app.models import User,OwnerStorageReservation
 from app.services import quota,owner_storage_budget,execution_budget
 from app.services.storage_admission import lock_owner
 from app.services.execution_storage_admission import admit
 results=[]
 async def phase(mode):
  ready=asyncio.Event();release=asyncio.Event();ticket={};errors=[];attempts=0;releaser=None
  async def holder():
   async with SessionLocal() as held:
    if mode=='owner_grant':
     async with owner_storage_budget.hold(held,user.id) as scope:
      ticket.update(id=scope.ticket['id'],reserved_bytes=scope.ticket['size'])
      ready.set();await release.wait()
    else:
     await lock_owner(held,user.id);ready.set();await release.wait()
  async def delayed_release():await asyncio.sleep(.3);release.set()
  async def operation():
   nonlocal attempts,releaser
   attempts+=1
   try:
    async with SessionLocal() as checked:
     await quota.check_storage_quota(checked,await checked.get(User,user.id),1024*1024)
   except HTTPException as error:
    errors.append({'status':error.status_code,'detail':error.detail})
    if mode!='original_deadline' and releaser is None:releaser=asyncio.create_task(delayed_release())
    raise
  task=asyncio.create_task(holder());start=time.monotonic()
  try:
   await asyncio.wait_for(ready.wait(),10)
   if mode=='original_deadline':
    budget=execution_budget.Budget('actual-admission-clock-check',time.monotonic()+.2,1024)
    try:
     with budget.activate():await admit(operation)
    except execution_budget.BudgetExceeded:assert budget.violation=='timeout'
    else:raise AssertionError('Admission renewed an exhausted execution budget')
    assert time.monotonic()-start<2 and not release.is_set()
   else:await admit(operation)
   assert attempts>=2 and errors and all(e['status']==409 and e['detail']['code']=='storage_quota_busy' for e in errors)
  finally:
   release.set()
   if releaser is not None:await releaser
   await task
  if mode=='owner_grant':
   async with SessionLocal() as checked:
    row=await checked.get(OwnerStorageReservation,ticket['id'])
    assert row.state=='settled' and row.remaining_bytes==0
  results.append({'mode':mode,'attempts':attempts,'errors':errors,'seconds':time.monotonic()-start,
   'reservation':ticket,'holder_drained':task.done()})
 await phase('SQL_owner_lock')
 if storage._use_s3():await phase('owner_grant')
 await phase('original_deadline')
 async with SessionLocal() as changed:
  plan=await changed.scalar(select(Plan).where(Plan.code=='free'));original=plan.quotas
  values=json.loads(original);values['storage_mb']=0;plan.quotas=json.dumps(values);await changed.commit()
 attempts=0
 async def full_account():
  nonlocal attempts
  attempts+=1
  async with SessionLocal() as checked:
   await quota.check_storage_quota(checked,await checked.get(User,user.id),1)
 try:
  try:await admit(full_account)
  except HTTPException as error:assert error.status_code==429 and attempts==1
  else:raise AssertionError('A truly full account was admitted')
 finally:
  async with SessionLocal() as changed:
   plan=await changed.scalar(select(Plan).where(Plan.code=='free'));plan.quotas=original;await changed.commit()
 results.append({'mode':'true_quota_exhausted','status':429,'attempts':attempts,'original_plan_restored':True})
 return {'passed':True,'actual_database_and_inventory':True,'no_model_substitution':True,'checks':results}
'''
FIXTURE=REDIS_FIXTURE+RESOURCE_FIXTURE


def scenario(api,*,broker,loop,store=None,**_):
    (api.root/'professional-entry-source.py').write_bytes(Path(__file__).read_bytes())
    actors=[];checks=[];workers=Workers(api,broker,store);run_dir='C-run/case-1';marker='TYPED_BATCH_REDIS_UDF_OK'
    def record(value):
        checks.append(value);(api.root/'workflow-batch-professional-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    workspace=api.call('GET','/workspaces')[0]['id']
    def actor(mode):
        case=api.call('POST','/workspaces/'+str(workspace)+'/cases',{'name':'Actual mixed Redis '+mode})
        value=copy.copy(api);value.case_id=case['id'];value.prefix='/cases/'+str(case['id']);actors.append(value)
        original=value.call('GET',value.prefix+'/caseDict')
        text=set_block(original['content'],'mesh',{'source':'blockMesh','qualityTargets':{'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
        text=set_block(text,'numericalMethod',{'solver':'icoFoam'})
        text=set_block(text,'postProcessing',{'runDir':run_dir,'fields':{'p':{'unit':'m2/s2'}},
            'dataExtraction':{'volumeAverages':{'meanPressure':{'field':'p','time':.5}}}})
        write(value,'PUT',value.prefix+'/caseDict',{'content':text,'expected_revision':original['dict_revision']})
        for path,body in cavity_2d().items():put(value,path,body)
        if mode=='udf':
            value.call('POST',value.prefix+'/udf/scaffold',{'name':'myFunctionObject','objective':'Actual typed Redis functionObject'})
            path='UDF/myFunctionObject/myFunctionObject.C';body=value.call('GET',value.prefix+'/raw?path='+quote(path),raw=True).decode()
            assert 'P0_UDF_OK' in body;put(value,path,body.replace('P0_UDF_OK',marker))
        body='import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n';ast.parse(body)
        seeded=put(value,'A-geo/geometry.py',body)
        if seeded.get('auto_preview_job'):
            jid=seeded['auto_preview_job'];value.wait_job(jid)
            wait_for(lambda:value.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Professional baseline preview did not drain')
        return value
    def waiting(server,rid,key):
        def observed():
            flow=server.call('GET','/workflow-runs/'+rid)
            if flow['status'] in {'failed','interrupted','cancelled'}:
                jobs={str(a['job_id']):server.call('GET','/jobs/'+str(a['job_id'])) for s in flow['steps'] for a in s['attempts']}
                resources={jid:api.call('GET','/testing/professional-resource/'+jid) for jid in jobs}
                (api.root/'failed-professional-workflow.json').write_text(json.dumps({'workflow':flow,'jobs':jobs,'resources':resources},indent=2),encoding='utf8')
                raise AssertionError('Actual professional member failed; see failed-professional-workflow.json')
            step=next(s for s in flow['steps'] if s['key']==key)
            return (flow,step) if step['status']=='awaiting_confirmation' else None
        return wait_for(observed,'Professional member did not reach '+key,300)
    def confirm(server,actor,rid,key):
        flow,step=waiting(server,rid,key)
        for producer in flow['steps']:
            if producer['status']=='succeeded' and producer['attempts']:
                jid=producer['attempts'][-1]['job_id'];server.wait_job(jid,timeout=300)
                wait_for(lambda:server.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Professional producer did not drain')
                wait_for(lambda:not server.call('GET','/jobs/'+str(jid))['completion_pending'],'Professional alias publication did not settle',180)
        result=write(server,'POST',actor.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
            'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
        kind=confirmed_dispatch_pending_kind(result)
        if kind:
            with (api.root/'professional-confirmation-backpressure.jsonl').open('a',encoding='utf8') as stream:
                stream.write(json.dumps({'run_id':rid,'confirmation':key,'kind':kind,'response':result})+'\n')
        return flow
    def publication(server,flow,key):
        step=next(s for s in flow['steps'] if s['key']==key);assert len(step['attempts'])==1
        jid=step['attempts'][0]['job_id'];job=server.wait_job(jid,timeout=300)
        wait_for(lambda:server.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Professional terminal write did not drain')
        wait_for(lambda:not server.call('GET','/jobs/'+str(jid))['completion_pending'],'Professional output aliases did not settle',180)
        pub=job['attempt']['outputs']['publication'];assert pub['status']=='committed'
        return job,server.call('GET','/testing/workflow-publication/'+pub['id'])['manifest']
    source=peer(api,store,broker,'source');recovery=peer(api,store,broker,'recovery')
    for server,role in [(api,'main'),(source,'source'),(recovery,'recovery')]:
        original_open=server.opener.open
        def timed(request,*args,_open=original_open,_role=role,**kwargs):
            started=time.monotonic();failure=None
            try:return _open(request,*args,**kwargs)
            except BaseException as error:failure=type(error).__name__;raise
            finally:
                with (api.root/'professional-http-timings.jsonl').open('a',encoding='utf8') as stream:
                    stream.write(json.dumps({'role':_role,'method':request.get_method(),'path':request.selector,
                        'seconds':time.monotonic()-started,'error':failure})+'\n')
        server.opener.open=timed
    try:
        admission=api.call('POST','/testing/professional-admission',{})
        assert admission['passed']
        (api.root/'professional-admission.json').write_text(json.dumps(admission,indent=2),encoding='utf8')
        mpi,udf=actor('mpi'),actor('udf');api.call('POST','/testing/batch-quota',{'job_concurrent':2})
        members=[]
        for value,mode in [(mpi,'mpi'),(udf,'udf')]:
            parameters={'geometry_mode':'export','solve_mode':mode,**({'subdomains':2} if mode=='mpi' else {'udf_name':'myFunctionObject'})}
            members.append({'key':mode,'case_id':value.case_id,'target':'postprocess','run_dir':run_dir,
                'expected_revision':value.call('GET',value.prefix+'/caseDict')['dict_revision'],'parameters':parameters,
                'budget':{'timeout_seconds':180,'max_log_bytes':16777216}})
        request={'members':members,'max_concurrent':2};key=uuid.uuid4().hex
        with source:
            workers.start();worker_ids=workers.identities()
            batch=source.call('POST','/workflow-batches',request,expect=202,extra_headers={'Idempotency-Key':key})
            ids={m['key']:m['id'] for m in batch['members']}
            for stage in ['confirm_B','confirm_C']:
                for value,mode in [(mpi,'mpi'),(udf,'udf')]:confirm(source,value,ids[mode],stage)
            mpi_flow,_=waiting(source,ids['mpi'],'confirm_D');udf_flow,_=waiting(source,ids['udf'],'confirm_D')
            mpi_job,mpi_manifest=publication(source,mpi_flow,'solve');build_job,build_manifest=publication(source,udf_flow,'udf-build')
            udf_job,udf_manifest=publication(source,udf_flow,'solve')
            mn=mpi_manifest['descriptor']['native']['receipt']['native']
            assert mpi_manifest['descriptor']['handler']=='workflow-mpi.v1' and mpi_manifest['descriptor']['subdomains']==2
            assert set(mn['processors'])=={'processor0','processor1'} and mn['uid']!=0
            assert set(mn['steps'])=={'decompose','solve','reconstruct'} and all(s['returncode']==0 for s in mn['steps'].values())
            bn=build_manifest['descriptor']['native']['receipt']['native'];un=udf_manifest['descriptor']['native']['receipt']['native']
            assert set(bn['steps'])=={'build','load'} and set(un['steps'])=={'load','solve'} and bn['uid']!=0 and un['uid']!=0
            assert bn['library']['sha256']==un['library']['sha256']
            assert marker in source.call('GET',udf.prefix+'/raw?path='+quote(run_dir+'/udf-solve.log'),raw=True).decode()
            first=vectors(source.call('GET',mpi.prefix+'/raw?path='+quote(run_dir+'/0.5/U'),raw=True))
            second=vectors(source.call('GET',udf.prefix+'/raw?path='+quote(run_dir+'/0.5/U'),raw=True))
            assert len(first)==len(second)==400
            relative=max(abs(a-b) for row,other in zip(first,second) for a,b in zip(row,other))/max(1,max(abs(x) for row in first for x in row))
            assert relative<=1e-5,relative
            controls=cavity_2d()
            for value in actors:
                for path,body in controls.items():
                    if '/system/' in path:assert source.call('GET',value.prefix+'/raw?path='+quote(path),raw=True).decode()==body
                graph=source.call('GET',value.prefix+'/dependencies?run_dir='+quote(run_dir));channels={r['kind']:r for r in graph['channels']}
                assert channels['mesh']['status']==channels['solve']['status']=='fresh'
            before={rid:source.call('GET','/workflow-runs/'+rid) for rid in ids.values()}
            delivery=source.call('GET','/testing/batch-delivery/'+batch['id'])
            assert all(j['mode']=='redis' and j['phase']=='succeeded' and j['worker'] in worker_ids for j in delivery['jobs'])
            jobs_before=[[j['id'] for j in value.call('GET',value.prefix+'/jobs')] for value in actors]
            pid=source.call('GET','/testing/workflow-api-owner')['pid'];launcher=kill_owned_api(source,pid)
            record({'stage':'actual_mixed_professional_Redis_producers','batch_id':batch['id'],'MPI_job':mpi_job['id'],
                'UDF_build_job':build_job['id'],'UDF_solve_job':udf_job['id'],'actual_two_nonroot_ranks':True,
                'actual_wmake_CDLL_icoFoam':True,'actual_UDF_marker':marker,'library_sha256':bn['library']['sha256'],
                'actual_cells':400,'velocity_relative_error':relative,'all_jobs_actual_worker_bound':delivery,
                'workers':worker_ids,'live_controls_unchanged':True,'source_API_pid_killed':pid,'verified_launcher_pid':launcher})
        if store:assert not (recovery.root/'s3-cache').exists()
        with recovery:
            for rid,flow in before.items():assert recovery.call('GET','/workflow-runs/'+rid)==flow
            assert jobs_before==[[j['id'] for j in recovery.call('GET',value.prefix+'/jobs')] for value in actors]
            metrics=[]
            for value,mode in [(mpi,'mpi'),(udf,'udf')]:
                confirm(recovery,value,ids[mode],'confirm_D')
                flow,_=waiting(recovery,ids[mode],'confirm_E');post_job,manifest=publication(recovery,flow,'postprocess')
                producer=next(s for s in before[ids[mode]]['steps'] if s['key']=='solve')['publication']['id']
                assert any(r['publication_id']==producer for r in manifest['descriptor']['input_publications'])
                result=json.loads(recovery.call('GET',value.prefix+'/raw?path='+quote('D-post/data/postprocess-'+str(post_job['id'])+'/metrics.json'),raw=True))
                mean=next(r['value'] for r in result['metrics'] if r['name']=='meanPressure.p@0.5')
                assert math.isfinite(mean) and result['readiness']['report_ready']
                if mode=='udf':assert abs(mean-.022259352333744136)<1e-5
                confirm(recovery,value,ids[mode],'confirm_E');metrics.append({'mode':mode,'job_id':post_job['id'],'mean_pressure':mean,'actual_readiness':True})
            final=wait_for(lambda:(b if (b:=recovery.call('GET','/workflow-batches/'+batch['id']))['status']=='succeeded' else None),'Mixed professional batch did not succeed')
            jobs_before=[[j['id'] for j in recovery.call('GET',value.prefix+'/jobs')] for value in actors]
            assert recovery.call('POST','/workflow-batches',request,expect=202,extra_headers={'Idempotency-Key':key})['id']==batch['id']
            assert jobs_before==[[j['id'] for j in recovery.call('GET',value.prefix+'/jobs')] for value in actors]
            delivery=recovery.call('GET','/testing/batch-delivery/'+batch['id'])
            assert all(j['mode']=='redis' and j['phase']=='succeeded' and j['worker'] in worker_ids for j in delivery['jobs'])
            record({'stage':'restart_and_professional_downstream','new_API_no_producer_replay':True,
                'independent_empty_S3_cache':bool(store),'explicit_B_C_D_E':True,'actual_metrics':metrics,
                'final_batch':final,'all_native_jobs_worker_bound':delivery,'same_key_same_batch_no_new_jobs':True})
            workers.stop()
        report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,'workers':workers.receipts,
            'scope':'Mixed independent-case MPI/UDF real worker workflow and actual downstream; cancellation/in-flight worker death and same-case C-run complete strategy remain.'}
        (api.root/'workflow-batch-professional.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        api.create_case('Independent actual mixed professional batch evidence readback');return report
    except BaseException as error:
        snapshots=[]
        for value in actors:
            try:snapshots.extend(value.call('GET','/jobs/'+str(j['id'])) for j in value.call('GET',value.prefix+'/jobs'))
            except Exception as observed:snapshots.append({'inspection_error':type(observed).__name__,'case_id':value.case_id})
        (api.root/'failed-professional-scenario.json').write_text(json.dumps({'type':type(error).__name__,
            'detail':str(error)[:3000],'jobs':snapshots},indent=2),encoding='utf8')
        raise
    finally:
        primary=sys.exc_info()[0];cleanup_errors=[]
        try:
            for value in actors:
                try:
                    for job in value.call('GET',value.prefix+'/jobs'):
                        if job['status'] in {'queued','running'}:
                            # A committed successful Job remains publicly running
                            # while its alias copy settles. A cancellation racing
                            # terminal commit must not require a false rollback.
                            if job.get('phase') not in {'succeeded','failed','cancelled','interrupted'}:
                                value.call('POST','/jobs/'+str(job['id'])+':cancel',{})
                            wait_for(lambda:value.call('GET','/jobs/'+str(job['id']))['status'] not in {'queued','running'},
                                'Owned professional cleanup did not settle',120)
                except Exception as error:cleanup_errors.append({'case_id':value.case_id,'type':type(error).__name__})
        finally:
            try:workers.stop()
            except Exception as error:cleanup_errors.append({'worker_stop':type(error).__name__})
            (api.root/'professional-worker-stops.json').write_text(json.dumps(workers.receipts,indent=2),encoding='utf8')
            (api.root/'professional-cleanup.json').write_text(json.dumps({'errors':cleanup_errors,'original_error_preserved':primary is not None},indent=2),encoding='utf8')
        if cleanup_errors and primary is None:raise RuntimeError('Actual professional scenario cleanup failed; inspect professional-cleanup.json')


async def main():
    root=Path(tempfile.mkdtemp(prefix='typed-batch-professional-redis-',dir=SERVER.parents[1]/'tmp'))
    broker=Broker(root);report={'passed':False,'developer_redis_used':False,'mock_llm_calls':0};passed=False
    print('PROFESSIONAL_BATCH_OWNED_REDIS '+str(root),flush=True)
    try:
        await broker.start()
        await live_main(selected_override=partial(scenario,broker=broker,loop=asyncio.get_running_loop()),fixture_override=FIXTURE,
            store_volume_max=16,environment_overrides=broker.settings());passed=True
    finally:
        try:await broker.stop()
        finally:
            report.update(passed=passed and len(broker.births)==len(broker.stops) and all(s['verified'] for s in broker.stops),
                actual_Redis_births=broker.births,actual_Redis_stops=broker.stops)
            (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
