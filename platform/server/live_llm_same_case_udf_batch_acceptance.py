"""Actual same-case UDF members, complete visibility and queried-source freshness."""
import ast,asyncio,copy,hashlib,json,sys,tempfile,uuid
from functools import partial
from pathlib import Path
from urllib.parse import quote
from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main as live_main,put,write
from live_llm_failure_fallback_acceptance import wait_for,kill_owned_api
from live_llm_workflow_batch_redis_acceptance import FIXTURE as REDIS_FIXTURE,peer as redis_peer
from live_llm_mpi_workflow_acceptance import vectors
from workflow_batch_redis_support import Broker,Workers
from testsupport import SERVER

EXTRA=r'''
from app.models import ArtifactPublication
from app.services.publication_inputs import resolve,PublicationInputStale
from app.services import job_worker_identity
from app.routers.jobs import _read_batch,_load_batch_rows
@app.get('/api/v1/testing/case-query-publication/{identifier}')
async def query_publication(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.get(ArtifactPublication,identifier)
 if row is None:raise HTTPException(404)
 case=await get_owned_case(row.case_id,user,db,write=False)
 try:await resolve(db,case,[{'publication_id':row.id,'require_current':True}])
 except PublicationInputStale as error:return {'fresh':False,'detail':str(error)}
 return {'fresh':True}
@app.get('/api/v1/cases/{case_id}/testing/same-case-batch/{identifier}')
async def same_case_batch(case_id:int,identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=False);manifest=_read_batch(case,identifier)
 jobs=await _load_batch_rows(db,manifest);result=[]
 for job in jobs:
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
  delivery=await job_delivery.intent(db,job,attempt)
  result.append({'job_id':job.id,'attempt_id':attempt.id,'phase':attempt.phase,'mode':delivery['mode'],
   'worker':job_worker_identity.document(job,attempt)['process'] if attempt.claim_token else None,
   'started_at':attempt.started_at.isoformat() if attempt.started_at else None,
   'ended_at':attempt.ended_at.isoformat() if attempt.ended_at else None})
 return {'jobs':result}
'''
FIXTURE=REDIS_FIXTURE+EXTRA


def peer(api,store,broker,role):
    value=redis_peer(api,store,broker,role)
    path=value.root/'redis_peer.py';code=path.read_text(encoding='utf8')+EXTRA
    ast.parse(code);path.write_text(code,encoding='utf8')
    return value


def scenario(api,*,broker,store=None,**_):
    (api.root/'same-case-udf-entry-source.py').write_bytes(Path(__file__).read_bytes())
    api.create_case('Actual same-case UDF source and batch bridge');value=copy.copy(api)
    source=peer(api,store,broker,'source');recovery=peer(api,store,broker,'recovery')
    for server in [source,recovery]:server.case_id=value.case_id;server.prefix=value.prefix
    workers=Workers(api,broker,store);checks=[];shared='C-run/udf-a/custom/nu.inc'
    def record(v):checks.append(v);(api.root/'same-case-udf-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    def drained(server,jid):
        server.wait_job(jid,timeout=300)
        wait_for(lambda:server.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Same-case writer did not drain')
        wait_for(lambda:not server.call('GET','/jobs/'+str(jid))['completion_pending'],'Same-case aliases did not settle',180)
    def fresh(server,pubs,expected):
        for pub in pubs:
            observed=server.call('GET','/testing/case-query-publication/'+pub)
            assert observed['fresh']==expected,observed
    try:
        with source:
            original=source.call('GET',source.prefix+'/caseDict')
            body=set_block(original['content'],'mesh',{'source':'blockMesh','qualityTargets':{'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
            body=set_block(body,'numericalMethod',{'solver':'icoFoam'})
            write(source,'PUT',source.prefix+'/caseDict',{'content':body,'expected_revision':original['dict_revision']})
            for path,body in cavity_2d().items():put(source,path,body)
            body='import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n';ast.parse(body)
            seeded=put(source,'A-geo/geometry.py',body)
            if seeded.get('auto_preview_job'):drained(source,seeded['auto_preview_job'])
            revision=source.call('GET',source.prefix+'/caseDict')['dict_revision']
            flow=source.call('POST',source.prefix+'/workflow-runs',{'target':'mesh','expected_revision':revision,
                'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':180,'max_log_bytes':16777216}})
            for key in ['confirm_B','confirm_C']:
                def waiting():
                    current=source.call('GET','/workflow-runs/'+flow['id'])
                    assert current['status'] not in {'failed','cancelled','interrupted'},current
                    step=next(s for s in current['steps'] if s['key']==key)
                    return (current,step) if step['status']=='awaiting_confirmation' else None
                current,step=wait_for(waiting,'Baseline mesh did not reach '+key,300)
                for producer in current['steps']:
                    if producer['status']=='succeeded':
                        for attempt in producer['attempts']:drained(source,attempt['job_id'])
                write(source,'POST',source.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
                    'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
            source.call('POST',source.prefix+'/udf/scaffold',{'name':'myFunctionObject','objective':'Actual same-case compiled UDF batch'})
            cpp='UDF/myFunctionObject/myFunctionObject.C'
            original=source.call('GET',source.prefix+'/raw?path='+quote(cpp),raw=True).decode()
            assert 'P0_UDF_OK' in original
            put(source,cpp,original.replace('P0_UDF_OK','UDF_SAME_CASE_REAL'))
            built=source.call('POST',source.prefix+'/udf:build',{'name':'myFunctionObject'})
            drained(source,built['job_id'])
            build_job=source.call('GET','/jobs/'+str(built['job_id']))
            build_pub=build_job['attempt']['outputs']['publication']['id']
            build=api.call('GET','/testing/workflow-publication/'+build_pub)['manifest']
            build_native=build['descriptor']['receipt']['native']
            assert build_native['uid']!=0 and set(build_native['steps'])=={'build','load'}
            assert build_native['compiler_dependencies']['policy']['query_schema']=='vibeflow.udf-file-queries.v2'
            library='UDF/myFunctionObject/libmyFunctionObject.so'
            library_sha=build['files'][library]['sha256']
            assert source.call('GET',source.prefix+'/raw?path='+quote(library),raw=True)[:4]==b'\x7fELF'
            for name in ['udf-a','udf-b']:
                made=write(source,'POST',source.prefix+'/runs:derive',{'base':'C-run/case-1','name':name})
                assert made['created']=='C-run/'+name
                for field in ['U','p']:
                    assert source.call('GET',source.prefix+'/raw?path='+quote('C-run/'+name+'/0/'+field),raw=True)==cavity_2d()['C-run/case-1/0/'+field].encode()
                control=cavity_2d()['C-run/case-1/constant/transportProperties']
                control=control.replace('nu              nu [0 2 -1 0 0 0 0] 0.01;',
                    '#include "../../udf-a/custom/nu.inc"\n#includeIfPresent "../../../Ref/optional-udf.inc"')
                assert '#include ' in control;put(source,'C-run/'+name+'/constant/transportProperties',control)
            include='nu nu [0 2 -1 0 0 0 0] 0.01;\n';put(source,shared,include);put(source,'Ref/unqueried-udf.txt','unused baseline\n')
            api.call('POST','/testing/batch-quota',{'job_concurrent':2})
            request={'operations':[{'kind':'udf','run_dir':'C-run/'+name,'name':'myFunctionObject','timeout_seconds':180} for name in ['udf-a','udf-b']],
                'max_concurrent':2};key=uuid.uuid4().hex
            old_jobs=[j['id'] for j in source.call('GET',source.prefix+'/jobs')]
            invalid=copy.deepcopy(request);invalid['operations'][1]['run_dir']='C-run/missing-member'
            source.call('POST',source.prefix+'/jobs:batch',invalid,expect=400,extra_headers={'Idempotency-Key':uuid.uuid4().hex})
            assert [j['id'] for j in source.call('GET',source.prefix+'/jobs')]==old_jobs
            batch=source.call('POST',source.prefix+'/jobs:batch',request,expect=202,extra_headers={'Idempotency-Key':key})
            assert len(batch['operations'])==2
            jobs=[j['id'] for j in batch['jobs']];assert len(set(jobs))==2
            workers.start();worker_ids=workers.identities();maximum=0
            def terminal():
                nonlocal maximum
                delivery=api.call('GET',value.prefix+'/testing/same-case-batch/'+batch['id'])
                maximum=max(maximum,sum(j['phase'] not in {'queued','succeeded','failed','cancelled','interrupted'} for j in delivery['jobs']))
                current=source.call('GET',source.prefix+'/job-batches/'+batch['id'])
                if current['status']=='failed':
                    (api.root/'failed-same-case-udf-batch.json').write_text(json.dumps({'batch':current,
                        'jobs':[source.call('GET','/jobs/'+str(jid)) for jid in jobs]},indent=2),encoding='utf8')
                    raise AssertionError('Actual same-case UDF batch failed; retained full job evidence')
                return current if current['status']=='succeeded' else None
            final=wait_for(terminal,'Actual same-case UDF batch did not finish',360)
            assert maximum==2
            publications=[];fields=[];proofs=[]
            for jid,name in zip(jobs,['udf-a','udf-b']):
                drained(source,jid);job=source.call('GET','/jobs/'+str(jid));pub=job['attempt']['outputs']['publication'];publications.append(pub['id'])
                assert job['attempt']['outputs']['completion']['alias_error'] is None,job['attempt']['outputs']['completion']
                manifest=api.call('GET','/testing/workflow-publication/'+pub['id'])['manifest'];d=manifest['descriptor'];native=d['receipt']['native']
                assert d['handler']=='udf.v1' and set(native['steps'])=={'load','solve'} and native['uid']!=0
                assert native['library']['sha256']==library_sha and d['source']['files'][library]['sha256']==library_sha
                solve_log=source.call('GET',source.prefix+'/raw?path='+quote('C-run/'+name+'/udf-solve.log'),raw=True).decode()
                assert 'UDF_SAME_CASE_REAL' in solve_log and 'UDF_LIBRARY_LOADED' in source.call('GET',source.prefix+'/raw?path='+quote('C-run/'+name+'/udf-load.log'),raw=True).decode()
                assert 'UDF/myFunctionObject/udf-load.log' not in d['output_files']
                assert d['configuration']['control_text'].encode()==source.call('GET',source.prefix+'/raw?path='+quote('C-run/'+name+'/system/controlDict'),raw=True)
                assert d['source']['schema']=='vibeflow.case-file-queries.v1' and native['case_queries']['evidence']['events']>0
                assert 'Ref/unqueried-udf.txt' in d['origin_source']['files'] and 'Ref/unqueried-udf.txt' not in d['source']['files']
                assert d['source']['files'][shared]['sha256']==hashlib.sha256(include.encode()).hexdigest()
                assert d['source']['entries']['Ref/optional-udf.inc']=='missing'
                fields.append(vectors(source.call('GET',source.prefix+'/raw?path='+quote('C-run/'+name+'/0.5/U'),raw=True)))
                proofs.append({'job_id':jid,'publication_id':pub['id'],'actual_cells':400,'actual_library_sha256':library_sha,'actual_loaded_marker':True,'independent_load_log':True,'query_events':native['case_queries']['evidence']['events'],
                    'query_bytes':native['case_queries']['evidence']['bytes'],'complete_seal_retained':True,'actual_cross_run_include':True,'actual_optional_miss':True})
            relative=max(abs(a-b) for row,other in zip(*fields) for a,b in zip(row,other));assert relative<=1e-5
            fresh(source,publications,True)
            path='C-run/udf-a/0/U';original=cavity_2d()['C-run/case-1/0/U']
            put(source,path,original+'\n// actual one-member input edit\n')
            fresh(source,[publications[0]],False);fresh(source,[publications[1]],True)
            put(source,path,original);fresh(source,publications,True)
            put(source,'Ref/unqueried-udf.txt','unrelated file changed after both members published\n');fresh(source,publications,True)
            put(source,'Ref/optional-udf.inc','// optional file now exists\n');fresh(source,publications,False)
            write(source,'DELETE',source.prefix+'/file?path='+quote('Ref/optional-udf.inc'),None);fresh(source,publications,True)
            put(source,shared,include.replace('0.01','0.02'));fresh(source,publications,False)
            put(source,shared,include);fresh(source,publications,True)
            delivery=api.call('GET',value.prefix+'/testing/same-case-batch/'+batch['id'])
            assert all(j['mode']=='redis' and j['phase']=='succeeded' and j['worker'] in worker_ids for j in delivery['jobs'])
            history=source.call('GET',source.prefix+'/job-batches/'+batch['id']);before=[j['id'] for j in source.call('GET',source.prefix+'/jobs')]
            pid=source.call('GET','/testing/workflow-api-owner')['pid'];kill_owned_api(source,pid)
            record({'stage':'actual_same_case_UDF_batch','batch_id':batch['id'],'actual_members':proofs,'maximum_claims':maximum,
                'actual_workers':worker_ids,'actual_delivery':delivery,'velocity_absolute_error':relative,'half_batch_rolled_back':True,
                'independent_source_freshness':True,'unqueried_change_accepted':True,'optional_file_addition_stale':True,
                'one_member_input_does_not_stale_other':True,'actual_UDF_build_job':built['job_id'],'actual_build_library_sha256':library_sha,
                'queried_cross_run_change_stale':True,'source_API_killed':pid})
        if store:assert not (recovery.root/'s3-cache').exists()
        with recovery:
            assert recovery.call('GET',recovery.prefix+'/job-batches/'+batch['id'])==history
            assert recovery.call('POST',recovery.prefix+'/jobs:batch',request,expect=202,extra_headers={'Idempotency-Key':key})['id']==batch['id']
            assert [j['id'] for j in recovery.call('GET',recovery.prefix+'/jobs')]==before
            fresh(recovery,publications,True);workers.stop()
            record({'stage':'restart_without_replay','original_jobs_preserved':True,'same_key_same_batch':True,'freshness_preserved':True,'independent_empty_S3_cache':bool(store)})
        report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,'workers':workers.receipts,
            'scope':'Same-case registered UDF batch and complete-source queried policy. Serial typed members and professional running-cancel/death matrix remain.'}
        (api.root/'same-case-udf-batch.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        api.create_case('Independent same-case UDF evidence readback');return report
    except BaseException as error:
        (api.root/'failed-same-case-udf-full.json').write_text(json.dumps({'type':type(error).__name__,
            'detail':str(error)[:2000],'jobs':[value.call('GET','/jobs/'+str(j['id'])) for j in value.call('GET',value.prefix+'/jobs')]},indent=2),encoding='utf8')
        raise
    finally:
        primary=sys.exc_info()[0];cleanup_errors=[]
        try:
            for job in value.call('GET',value.prefix+'/jobs'):
                if job['status'] in {'queued','running'}:
                    if job.get('phase') not in {'succeeded','failed','cancelled','interrupted'}:
                        value.call('POST','/jobs/'+str(job['id'])+':cancel',{})
                    wait_for(lambda:value.call('GET','/jobs/'+str(job['id']))['status'] not in {'queued','running'},
                        'Owned UDF batch cleanup did not settle',120)
        except Exception as error:cleanup_errors.append({'jobs':type(error).__name__})
        finally:
            try:workers.stop()
            except Exception as error:cleanup_errors.append({'workers':type(error).__name__})
            (api.root/'same-case-udf-worker-stops.json').write_text(json.dumps(workers.receipts,indent=2),encoding='utf8')
            (api.root/'same-case-udf-cleanup.json').write_text(json.dumps({'errors':cleanup_errors,'original_error_preserved':primary is not None},indent=2),encoding='utf8')
        if cleanup_errors and primary is None:raise RuntimeError('Owned UDF batch cleanup failed; inspect cleanup receipt')


async def main():
    root=Path(tempfile.mkdtemp(prefix='same-case-udf-redis-',dir=SERVER.parents[1]/'tmp'));broker=Broker(root);passed=False
    print('SAME_CASE_UDF_OWNED_REDIS '+str(root),flush=True)
    try:
        await broker.start()
        await live_main(selected_override=partial(scenario,broker=broker),fixture_override=FIXTURE,
            store_volume_max=16,environment_overrides=broker.settings());passed=True
    finally:
        await broker.stop()
        (root/'result.json').write_text(json.dumps({'passed':passed,'mock_llm_calls':0,'actual_Redis_births':broker.births,
            'actual_Redis_stops':broker.stops},indent=2),encoding='utf8')
if __name__=='__main__':asyncio.run(main())
