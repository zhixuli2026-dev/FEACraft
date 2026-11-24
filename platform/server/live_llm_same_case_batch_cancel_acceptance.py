"""Actual mixed prepared-run cancellation, API death and explicit fresh batch.

Uses real Redis workers, native OpenFOAM processes and publication heads. The
outer live wrapper asks the authorized DeepSeek model to read observed evidence.
"""
import ast,asyncio,copy,hashlib,json,math,re,sys,tempfile,uuid
import time
import httpx
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from urllib.parse import quote

from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main as live_main,put,write
from live_llm_failure_fallback_acceptance import wait_for,kill_owned_api
import live_llm_same_case_udf_batch_acceptance as prepared
from live_llm_udf_cancel_recovery_acceptance import native_inspect
from workflow_batch_redis_support import Broker,Workers
from testsupport import SERVER

EXTRA=r'''
from app.models import ArtifactPublicationHead,DurableEvent
from app import storage
import hashlib
@app.post('/api/v1/cases/{case_id}/testing/prepared-cancel-heads')
async def prepared_cancel_heads(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=False);result=[]
 assert len(body['members'])==3
 for item in body['members']:
  row=await db.get(ArtifactPublicationHead,(case.id,item['kind']))
  path=item['run_dir']+'/0.0001/U'
  result.append({'kind':item['kind'],'run_dir':item['run_dir'],'head':row.active_publication_id if row else None,
   'field_sha256':hashlib.sha256(storage.get_bytes(case.s3_prefix,path)).hexdigest() if storage.exists(case.s3_prefix,path) else None})
 return result
@app.get('/api/v1/cases/{case_id}/testing/prepared-cancel-events/{batch_id}')
async def prepared_cancel_events(case_id:int,batch_id:str,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=False)
 rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.case_id==case.id,DurableEvent.type=='job.batch_cancel_requested').order_by(DurableEvent.seq)))
 return [{'seq':e.seq,'payload':json.loads(e.payload_json)} for e in rows if json.loads(e.payload_json)['batch_id']==batch_id]
@app.get('/api/v1/testing/prepared-never-claimed/{job_id}')
async def prepared_never_claimed(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db,write=False)
 attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
 return {'never_claimed':job.status=='cancelled' and attempt.phase=='cancelled' and attempt.claim_token is None
  and attempt.started_at is None and json.loads(attempt.process_identity_json)=={}
  and not json.loads(attempt.child_processes_json).get('children')}
@app.get('/api/v1/cases/{case_id}/testing/prepared-edit-events')
async def prepared_edit_events(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=False)
 rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.case_id==case.id,DurableEvent.type=='artifact.invalidated').order_by(DurableEvent.seq)))
 return [{'seq':row.seq,'payload':json.loads(row.payload_json)} for row in rows]
'''
FIXTURE=prepared.FIXTURE+EXTRA


def peer(api,store,broker,role):
    value=prepared.peer(api,store,broker,role);path=value.root/'redis_peer.py'
    code=path.read_text(encoding='utf8')+EXTRA;ast.parse(code);path.write_text(code,encoding='utf8')
    value.case_id=api.case_id;value.prefix=api.prefix
    return value


def field(data):
    match=re.search(r'internalField\s+nonuniform\s+List<vector>\s+(\d+)\s*\((.*?)\)\s*;',data.decode(),re.S)
    assert match,'Actual solver did not write an explicit vector field'
    rows=[tuple(map(float,row.split())) for row in re.findall(r'\(([^()]*)\)',match.group(2))]
    assert len(rows)==int(match.group(1))==10000
    assert all(len(row)==3 and all(math.isfinite(x) and abs(x)<5 for x in row) for row in rows)
    return rows


def scenario(api,*,broker,store=None,**_):
    (api.root/'same-case-batch-cancel-entry-source.py').write_bytes(Path(__file__).read_bytes())
    api.create_case('Actual same-case mixed batch running cancellation');value=copy.copy(api)
    source=peer(api,store,broker,'source');observer=peer(api,store,broker,'observer');recovery=peer(api,store,broker,'recovery')
    workers=Workers(api,broker,store);checks=[];batches=[];observations=[]
    modes=['mpi','udf','serial'];runs={mode:'C-run/cancel-'+mode for mode in modes}
    inputs=cavity_2d();control_path='C-run/case-1/system/controlDict'
    short=inputs[control_path].replace('endTime         0.5;','endTime         0.0001;').replace(
        'deltaT          0.005;','deltaT          0.00001;').replace('writeInterval   20;','writeInterval   10;')
    long=short.replace('endTime         0.0001;','endTime         1000000;').replace('writeInterval   10;','writeInterval   1000000000;')
    assert short!=inputs[control_path] and long!=short
    def record(item):
        checks.append(item);(api.root/'same-case-batch-cancel-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
        print('SAME_CASE_CANCEL_STAGE '+item['stage'],flush=True)
    def drain(server,jid,expected='succeeded'):
        server.wait_job(jid,expected=expected,timeout=360)
        wait_for(lambda:drained(server,jid),'Prepared member did not drain',120)
        wait_for(lambda:not server.call('GET','/jobs/'+str(jid))['completion_pending'],'Prepared member completion did not settle',180)
        return server.call('GET','/jobs/'+str(jid))
    def drained(server,jid):
        return (server.call('GET','/testing/workflow-job-drain/'+str(jid))['verified']
            or server.call('GET','/testing/prepared-never-claimed/'+str(jid))['never_claimed'])
    def request(order,cap):
        operations=[]
        for mode in order:
            item={'kind':mode,'run_dir':runs[mode],'timeout_seconds':300,'max_log_bytes':67108864}
            if mode=='mpi':item.update(solver='icoFoam',subdomains=2)
            if mode=='serial':item.update(solver='icoFoam')
            if mode=='udf':item.update(name='myFunctionObject')
            operations.append(item)
        return {'operations':operations,'max_concurrent':cap}
    def submit(server,body,key):
        endpoint=value.prefix+'/jobs:batch';started=time.monotonic()
        def trace(stage,**fields):
            with (api.root/'batch-admission-timings.jsonl').open('a',encoding='utf8') as stream:
                stream.write(json.dumps({'stage':stage,'endpoint':endpoint,'operations':len(body['operations']),
                    'client_timeout_seconds':180 if store else 30,'elapsed_ms':round(1000*(time.monotonic()-started)),**fields})+'\n')
        trace('request_started')
        try:
            if store:
                # A complete-case three-member S3 preparation can outlast the
                # generic test client's 30s observation window. Preserve the
                # same endpoint, request/key and all server-side validation.
                with httpx.Client(base_url=server.base+'/api/v1',trust_env=False,
                        headers={'Authorization':'Bearer '+server.token},timeout=httpx.Timeout(180,connect=10)) as client:
                    response=client.post(endpoint,json=body,headers={'Idempotency-Key':key})
                    assert response.status_code==202,('Actual batch admission refused',response.status_code,response.text[:1000])
                    batch=response.json()
            else:
                batch=server.call('POST',endpoint,body,expect=202,extra_headers={'Idempotency-Key':key})
            trace('response_received',status=202,batch_id=batch['id'])
        except BaseException as error:
            trace('request_failed',error_type=type(error).__name__);raise
        batches.append((batch,body,key));return batch
    def completed(server,batch):
        def observed():
            current=server.call('GET',value.prefix+'/job-batches/'+batch['id'])
            assert current['status'] not in {'failed','cancelled'},'Actual prepared batch failed; inspect saved jobs'
            return current if current['status']=='succeeded' else None
        return wait_for(observed,'Actual prepared batch did not finish',480)
    def proofs(server,batch):
        results=[];fields=[]
        for mode,item in zip(modes,batch['jobs']):
            job=drain(server,item['id']);pub=job['attempt']['outputs']['publication']
            assert pub['status']=='committed' and job['attempt']['outputs']['completion']['alias_error'] is None
            manifest=server.call('GET','/testing/workflow-publication/'+pub['id'])['manifest'];descriptor=manifest['descriptor']
            assert descriptor['handler']=={'mpi':'mpi.v1','udf':'udf.v1','serial':'prepared-solve.v1'}[mode]
            assert descriptor['origin_source']['files'] and descriptor['output_files']
            assert descriptor['source']['schema']==('vibeflow.prepared-serial-source.v1' if mode=='serial' else 'vibeflow.case-file-queries.v1')
            data=server.call('GET',value.prefix+'/raw?path='+quote(runs[mode]+'/0.0001/U'),raw=True)
            fields.append(field(data))
            results.append({'mode':mode,'job_id':job['id'],'publication_id':pub['id'],'kind':pub['kind'],
                'run_dir':runs[mode],'actual_cells':10000,'final_time':0.0001,'field_sha256':hashlib.sha256(data).hexdigest(),
                'complete_origin_files':len(descriptor['origin_source']['files'])})
        error=max(abs(x-y) for other in fields[1:] for row,other_row in zip(fields[0],other) for x,y in zip(row,other_row))
        assert error<=1e-5,error
        return results,error
    def head(server,members):
        return server.call('POST',value.prefix+'/testing/prepared-cancel-heads',{'members':members})
    def actual(server,jid,mode,library_sha):
        def observed():
            job=server.call('GET','/jobs/'+str(jid))
            if job['status']=='queued':return None
            assert job['status']=='running',(mode,job['status'],job['phase'])
            if mode=='serial':ident=job['attempt']['process_identity']
            else:
                active=[child for child in job['attempt']['child_processes']['children'].values()
                    if child['purpose']==('mpi_run' if mode=='mpi' else 'udf_run') and child['phase']=='running']
                if not active:return None
                assert len(active)==1;ident=active[0]['identity']
            if not ident or not ident.get('native'):return None
            native=ident['native'];members=native_inspect(native['distribution'],{'root':native['process']})
            wanted=[m for m in members if m['comm']=='icoFoam']
            if len(wanted)!=(2 if mode=='mpi' else 1):return None
            assert any(m['comm']=='strace' for m in members)
            if mode in {'mpi','udf'}:
                assert all(m['identity']['uid']==65534 for m in members)
                assert all(m['uid_map'].split()==['65534','65534','1'] for m in wanted)
            if mode=='udf':
                libraries=wanted[0]['loaded_UDF_libraries']
                if not libraries:return None  # Executable exists before dlopen completes.
                assert any(lib['sha256']==library_sha for lib in libraries),'Actual UDF loaded a different library'
            return {'mode':mode,'job_id':jid,'distribution':native['distribution'],'members':members,
                'actual_solver_pids':[m['identity']['pid'] for m in wanted]}
        result=wait_for(observed,'Actual prepared '+mode+' solver was not observed',90)
        observations.append(result);return result
    def cancel(server,batch,running,queued,old_head,head_members):
        delivery=server.call('GET',value.prefix+'/testing/same-case-batch/'+batch['id'])['jobs']
        assert all(row['worker'] in workers.identities() and row['mode']=='redis' for row in delivery if row['job_id'] in running)
        assert all(row['worker'] is None and row['phase']=='queued' and row['started_at'] is None for row in delivery if row['job_id'] in queued)
        endpoint=value.prefix+'/job-batches/'+batch['id']+':cancel'
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(s.call,'POST',endpoint,{}) for s in [server,observer]]
            replies=[future.result() for future in futures]
        assert all(reply['status']=='cancelled' for reply in replies)
        for jid in [*running,*queued]:
            job=drain(server,jid,'cancelled');assert job['attempt']['phase']=='cancelled'
            if jid in queued:assert job['attempt']['process_identity'] is None
        for observed in observations:
            if observed['job_id'] not in running:continue
            probes=native_inspect(observed['distribution'],{'probe':[row['identity'] for row in observed['members']]})
            assert all(row['state']=='exited' for row in probes)
            observed['actual_exit_probes']=probes
        after=server.call('GET',value.prefix+'/testing/same-case-batch/'+batch['id'])['jobs']
        assert all(row['worker'] is None and row['started_at'] is None for row in after if row['job_id'] in queued)
        events=server.call('GET',value.prefix+'/testing/prepared-cancel-events/'+batch['id'])
        assert len(events)==1 and set(events[0]['payload']['jobs'])==set(running+queued),events
        after_head=head(server,head_members)
        (api.root/('cancel-head-comparison-'+batch['id']+'.json')).write_text(json.dumps(
            {'before_cancel':old_head,'after_cancel':after_head},indent=2),encoding='utf8')
        assert after_head==old_head,'Cancellation changed its pre-cancel heads or old fields'
        settled=server.call('GET',value.prefix+'/job-batches/'+batch['id'])
        server.call('POST',endpoint,{})
        assert server.call('GET',value.prefix+'/job-batches/'+batch['id'])==settled
        record({'stage':'actual_cancel_'+str(len(running))+'_running_'+str(len(queued))+'_queued','batch_id':batch['id'],
            'running':running,'queued_never_claimed':queued,'actual_delivery':delivery,'one_cancel_event':events,
            'old_heads_and_fields_preserved':True,'concurrent_API_cancellation':True})
        return settled
    try:
        with source,observer:
            original=source.call('GET',value.prefix+'/caseDict')
            body=set_block(original['content'],'mesh',{'source':'blockMesh','qualityTargets':{'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
            body=set_block(body,'numericalMethod',{'solver':'icoFoam'})
            write(source,'PUT',value.prefix+'/caseDict',{'content':body,'expected_revision':original['dict_revision']})
            for path,body in inputs.items():
                if path==control_path:body=short
                if path.endswith('/blockMeshDict'):body=body.replace('(20 20 1)','(100 100 1)')
                put(source,path,body)
            seeded=put(source,'A-geo/geometry.py','import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n')
            if seeded.get('auto_preview_job'):drain(source,seeded['auto_preview_job'])
            revision=source.call('GET',value.prefix+'/caseDict')['dict_revision']
            flow=source.call('POST',value.prefix+'/workflow-runs',{'target':'mesh','expected_revision':revision,
                'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':180,'max_log_bytes':16777216}})
            for key in ['confirm_B','confirm_C']:
                def waiting():
                    current=source.call('GET','/workflow-runs/'+flow['id']);assert current['status'] not in {'failed','cancelled','interrupted'}
                    step=next(s for s in current['steps'] if s['key']==key)
                    return (current,step) if step['status']=='awaiting_confirmation' else None
                current,step=wait_for(waiting,'Actual mesh did not reach '+key,300)
                for producer in current['steps']:
                    if producer['status']=='succeeded':
                        for attempt in producer['attempts']:drain(source,attempt['job_id'])
                write(source,'POST',value.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
                    'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
            source.call('POST',value.prefix+'/udf/scaffold',{'name':'myFunctionObject','objective':'Actual mixed prepared cancellation'})
            built=source.call('POST',value.prefix+'/udf:build',{'name':'myFunctionObject'});build=drain(source,built['job_id'])
            library=source.call('GET',value.prefix+'/raw?path='+quote('UDF/myFunctionObject/libmyFunctionObject.so'),raw=True)
            assert library[:4]==b'\x7fELF';library_sha=hashlib.sha256(library).hexdigest()
            for mode in modes:
                created=write(source,'POST',value.prefix+'/runs:derive',{'base':'C-run/case-1','name':'cancel-'+mode})
                assert created['created']==runs[mode]
            api.call('POST','/testing/batch-quota',{'job_concurrent':2});workers.start()
            baseline=submit(source,request(modes,2),uuid.uuid4().hex);completed(source,baseline)
            baseline_proofs,baseline_error=proofs(source,baseline)
            members=[{k:r[k] for k in ['kind','run_dir']} for r in baseline_proofs];old_head=head(source,members)
            assert [row['head'] for row in old_head]==[row['publication_id'] for row in baseline_proofs]
            record({'stage':'actual_three_mode_baseline','batch_id':baseline['id'],'actual_proofs':baseline_proofs,
                'velocity_absolute_error':baseline_error,'library_sha256':library_sha})
            for mode in modes:put(source,runs[mode]+'/system/controlDict',long)
            prepared_head=head(source,members)
            for mode,before,after in zip(modes,old_head,prepared_head):
                assert after['field_sha256']==before['field_sha256']
                assert after['head']==(None if mode=='udf' else before['head'])
            edit_events=source.call('GET',value.prefix+'/testing/prepared-edit-events')
            assert any(e['payload']['publication_id']==baseline_proofs[1]['publication_id'] and
                e['payload']['reason']=='output_alias_edited' for e in edit_events),edit_events
            # UDF publishes its integrated controlDict as an output. Explicitly
            # editing that alias makes the current result a draft by design;
            # cancellation must preserve this actual pre-cancel state. Retain
            # and verify every original immutable publication separately.
            original_manifests={item['publication_id']:source.call('GET','/testing/workflow-publication/'+item['publication_id'])
                for item in baseline_proofs}
            record({'stage':'explicit_input_edit_before_cancellation','baseline_heads':old_head,'prepared_heads':prepared_head,
                'actual_invalidation_events':edit_events,'old_fields_preserved':True,'old_immutable_publications_readable':True})
            old_head=prepared_head
            mixed=submit(source,request(modes,2),uuid.uuid4().hex);ids=[j['id'] for j in mixed['jobs']]
            actual(source,ids[0],'mpi',library_sha);actual(source,ids[1],'udf',library_sha)
            first_history=cancel(source,mixed,ids[:2],ids[2:],old_head,members)
            # The earlier phase exercises queued serial cancellation. This phase
            # observes the actual canonical serial solver before cancellation.
            serial=submit(source,request(['serial','mpi'],1),uuid.uuid4().hex);ids=[j['id'] for j in serial['jobs']]
            actual(source,ids[0],'serial',library_sha)
            second_history=cancel(source,serial,ids[:1],ids[1:],old_head,members)
            original_histories={mixed['id']:first_history,serial['id']:second_history}
            pid=source.call('GET','/testing/workflow-api-owner')['pid'];kill_owned_api(source,pid)
            record({'stage':'actual_source_API_terminated','pid':pid,'original_histories':original_histories})
        if store:assert not (recovery.root/'s3-cache').exists()
        with recovery:
            for identifier,history in original_histories.items():
                assert recovery.call('GET',value.prefix+'/job-batches/'+identifier)==history
            assert head(recovery,members)==old_head
            for identifier,manifest in original_manifests.items():
                assert recovery.call('GET','/testing/workflow-publication/'+identifier)==manifest
            before=[j['id'] for j in recovery.call('GET',value.prefix+'/jobs')]
            for batch,body,key in batches:
                assert recovery.call('POST',value.prefix+'/jobs:batch',body,expect=202,extra_headers={'Idempotency-Key':key})['id']==batch['id']
            assert [j['id'] for j in recovery.call('GET',value.prefix+'/jobs')]==before
            for mode in modes:put(recovery,runs[mode]+'/system/controlDict',short)
            fresh=submit(recovery,request(modes,2),uuid.uuid4().hex);completed(recovery,fresh)
            new_proofs,new_error=proofs(recovery,fresh)
            assert all(new['publication_id']!=old['publication_id'] and new['job_id']!=old['job_id'] for old,new in zip(baseline_proofs,new_proofs))
            assert [r['head'] for r in head(recovery,members)]==[r['publication_id'] for r in new_proofs]
            for identifier,history in original_histories.items():assert recovery.call('GET',value.prefix+'/job-batches/'+identifier)==history
            record({'stage':'explicit_fresh_three_mode_batch_completed','batch_id':fresh['id'],'actual_proofs':new_proofs,
                'velocity_absolute_error':new_error,'old_history_and_idempotency_preserved':True,'independent_empty_S3_cache':bool(store)})
        workers.stop()
        report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,
            'actual_native_observations':observations,'worker_stops':workers.receipts,
            'scope':'Same-case prepared MPI/UDF/serial actual running and queued batch cancellation, original heads and fields, API death, explicit new batch. Worker-death/direct-root recovery and other fault scopes remain separate.'}
        (api.root/'same-case-batch-cancel.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        api.create_case('Independent actual mixed prepared cancellation evidence readback');return report
    except BaseException as error:
        try:jobs=[value.call('GET','/jobs/'+str(j['id'])) for j in value.call('GET',value.prefix+'/jobs')]
        except Exception as observed:jobs=[{'inspection_error':type(observed).__name__}]
        (api.root/'failed-same-case-batch-cancel.json').write_text(json.dumps({'type':type(error).__name__,
            'detail':str(error)[:2500],'jobs':jobs,'observations':observations},indent=2),encoding='utf8');raise
    finally:
        primary=sys.exc_info()[0];errors=[]
        try:
            targets=[job for job in value.call('GET',value.prefix+'/jobs') if job['status'] in {'queued','running'}
                or job['phase'] not in {'succeeded','failed','cancelled','interrupted'} or job['completion_pending']]
            # Request every owned cancellation before awaiting any single writer.
            # One missing receipt must not prevent cleanup of the other members.
            for job in targets:
                if job['phase'] not in {'succeeded','failed','cancelled','interrupted'}:
                    try:value.call('POST','/jobs/'+str(job['id'])+':cancel',{})
                    except Exception as error:errors.append({'cancel_job':job['id'],'error':type(error).__name__})
            for job in targets:
                try:
                    def settled():
                        observed=value.call('GET','/jobs/'+str(job['id']))
                        return observed['phase'] in {'succeeded','failed','cancelled','interrupted'} and not observed['completion_pending'] and drained(value,job['id'])
                    wait_for(settled,'Prepared batch cleanup did not drain',180)
                except Exception as error:errors.append({'drain_job':job['id'],'error':type(error).__name__})
        except Exception as error:errors.append({'jobs':type(error).__name__})
        finally:
            try:workers.stop()
            except Exception as error:errors.append({'workers':type(error).__name__})
            (api.root/'same-case-batch-cancel-cleanup.json').write_text(json.dumps({'errors':errors,
                'primary_preserved':primary is not None,'worker_stops':workers.receipts},indent=2),encoding='utf8')
        if errors and primary is None:raise RuntimeError('Actual prepared cancellation cleanup failed; inspect receipt')


async def main():
    root=Path(tempfile.mkdtemp(prefix='same-case-batch-cancel-redis-',dir=SERVER.parents[1]/'tmp'));broker=Broker(root);passed=False
    print('SAME_CASE_BATCH_CANCEL_OWNED_REDIS '+str(root),flush=True)
    try:
        await broker.start()
        await live_main(selected_override=partial(scenario,broker=broker),fixture_override=FIXTURE,
            store_volume_max=16,environment_overrides=broker.settings());passed=True
    finally:
        try:await broker.stop()
        finally:(root/'result.json').write_text(json.dumps({'passed':passed,'mock_llm_calls':0,
            'actual_Redis_births':broker.births,'actual_Redis_stops':broker.stops},indent=2),encoding='utf8')

if __name__=='__main__':asyncio.run(main())
