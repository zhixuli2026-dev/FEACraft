"""Actual same-case serial members, complete visibility and queried-source freshness."""
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
    (api.root/'same-case-serial-entry-source.py').write_bytes(Path(__file__).read_bytes())
    api.create_case('Actual same-case serial source and batch bridge');value=copy.copy(api)
    source=peer(api,store,broker,'source');recovery=peer(api,store,broker,'recovery')
    for server in [source,recovery]:server.case_id=value.case_id;server.prefix=value.prefix
    workers=Workers(api,broker,store);checks=[];shared='C-run/serial-a/custom/nu.inc'
    def record(v):checks.append(v);(api.root/'same-case-serial-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
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
            workers.start()
            worker_ids=workers.identities()
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
            outputs = ['log.icoFoam', 'postProcessing/probes/0/U', 'processors2/0/U', 'processors4_0-3/0/p']
            for path in outputs:
                put(source, 'C-run/case-1/' + path, 'source output\n')
            put(source, 'C-run/case-1/system/log.config', 'source configuration\n')
            for name in ['serial-a','serial-b']:
                made=write(source,'POST',source.prefix+'/runs:derive',{'base':'C-run/case-1','name':name})
                assert made['created']=='C-run/'+name
                for path in outputs:
                    source.call('GET', source.prefix+'/raw?path='+quote(made['created']+'/'+path), expect=404)
                assert source.call('GET', source.prefix+'/raw?path='+quote(made['created']+'/system/log.config'), raw=True) == b'source configuration\n'
                source.call('POST', source.prefix+'/runs:derive', {'base':'C-run/case-1','name':name}, expect=409)
                for field in ['U','p']:
                    assert source.call('GET',source.prefix+'/raw?path='+quote('C-run/'+name+'/0/'+field),raw=True)==cavity_2d()['C-run/case-1/0/'+field].encode()
                control=cavity_2d()['C-run/case-1/constant/transportProperties']
                control=control.replace('nu              nu [0 2 -1 0 0 0 0] 0.01;',
                    '#include "../../serial-a/custom/nu.inc"\n#includeIfPresent "../../../Ref/optional-serial.inc"')
                assert '#include ' in control;put(source,'C-run/'+name+'/constant/transportProperties',control)
            for path in outputs:
                assert source.call('GET', source.prefix+'/raw?path='+quote('C-run/case-1/'+path), raw=True) == b'source output\n'
            record({'stage':'derived_inputs_and_collision','excluded_outputs':outputs,'source_outputs_preserved':True,
                    'nested_log_configuration_preserved':True,'duplicate_targets_returned_409':['serial-a','serial-b']})
            include='nu nu [0 2 -1 0 0 0 0] 0.01;\n';put(source,shared,include);put(source,'Ref/unqueried-serial.txt','unused baseline\n')
            api.call('POST','/testing/batch-quota',{'job_concurrent':2})
            request={'operations':[{'kind':'serial','run_dir':'C-run/'+name,'solver':'icoFoam','timeout_seconds':180} for name in ['serial-a','serial-b']],
                'max_concurrent':2};key=uuid.uuid4().hex
            old_jobs=[j['id'] for j in source.call('GET',source.prefix+'/jobs')]
            invalid=copy.deepcopy(request);invalid['operations'][1]['run_dir']='C-run/missing-member'
            source.call('POST',source.prefix+'/jobs:batch',invalid,expect=400,extra_headers={'Idempotency-Key':uuid.uuid4().hex})
            assert [j['id'] for j in source.call('GET',source.prefix+'/jobs')]==old_jobs
            batch=source.call('POST',source.prefix+'/jobs:batch',request,expect=202,extra_headers={'Idempotency-Key':key})
            assert len(batch['operations'])==2
            jobs=[j['id'] for j in batch['jobs']];assert len(set(jobs))==2
            maximum=0
            def terminal():
                nonlocal maximum
                delivery=api.call('GET',value.prefix+'/testing/same-case-batch/'+batch['id'])
                maximum=max(maximum,sum(j['phase'] not in {'queued','succeeded','failed','cancelled','interrupted'} for j in delivery['jobs']))
                current=source.call('GET',source.prefix+'/job-batches/'+batch['id'])
                if current['status']=='failed':
                    (api.root/'failed-same-case-serial-batch.json').write_text(json.dumps({'batch':current,
                        'jobs':[source.call('GET','/jobs/'+str(jid)) for jid in jobs]},indent=2),encoding='utf8')
                    raise AssertionError('Actual same-case serial batch failed; retained full job evidence')
                return current if current['status']=='succeeded' else None
            final=wait_for(terminal,'Actual same-case serial batch did not finish',360)
            assert maximum==2
            publications=[];fields=[];proofs=[]
            for jid,name in zip(jobs,['serial-a','serial-b']):
                drained(source,jid);job=source.call('GET','/jobs/'+str(jid));pub=job['attempt']['outputs']['publication'];publications.append(pub['id'])
                assert job['attempt']['outputs']['completion']['alias_error'] is None,job['attempt']['outputs']['completion']
                manifest=api.call('GET','/testing/workflow-publication/'+pub['id'])['manifest'];d=manifest['descriptor'];native=d['query_receipt']
                assert d['handler']=='prepared-solve.v1' and d['solver']=='icoFoam'
                toolchain=json.loads(source.call('GET',source.prefix+'/raw?path='+quote('C-run/'+name+'/solve-toolchain.json'),raw=True))
                assert toolchain['solver']=='icoFoam' and toolchain['case_queries']['strace']['sha256']
                assert native['execution_id']==d['execution_id'] and d['prepared_configuration']['retired_files']=={}
                assert job['attempt']['handler']['key']=='solve.openfoam.prepared' and job['attempt']['process_stop']['verified']
                assert d['source']['schema']=='vibeflow.prepared-serial-source.v1' and native['case_queries']['evidence']['events']>0
                queried=d['source']['native_case'];assert queried['schema']=='vibeflow.case-file-queries.v1'
                assert 'Ref/unqueried-serial.txt' in d['origin_source']['files'] and 'Ref/unqueried-serial.txt' not in queried['files']
                assert queried['files'][shared]['sha256']==hashlib.sha256(include.encode()).hexdigest()
                assert queried['entries']['Ref/optional-serial.inc']=='missing'
                fields.append(vectors(source.call('GET',source.prefix+'/raw?path='+quote('C-run/'+name+'/0.5/U'),raw=True)))
                assert len(fields[-1])==400
                proofs.append({'job_id':jid,'publication_id':pub['id'],'actual_cells':400,'actual_serial_worker':True,'actual_solver_executable':toolchain['executable'],'actual_strace_sha256':toolchain['case_queries']['strace']['sha256'],'query_events':native['case_queries']['evidence']['events'],
                    'query_bytes':native['case_queries']['evidence']['bytes'],'complete_seal_retained':True,'actual_cross_run_include':True,'actual_optional_miss':True})
            relative=max(abs(a-b) for row,other in zip(*fields) for a,b in zip(row,other));assert relative<=1e-5
            fresh(source,publications,True)
            path='C-run/serial-a/0/U';original=cavity_2d()['C-run/case-1/0/U']
            put(source,path,original+'\n// actual one-member input edit\n')
            fresh(source,[publications[0]],False);fresh(source,[publications[1]],True)
            put(source,path,original);fresh(source,publications,True)
            put(source,'Ref/unqueried-serial.txt','unrelated file changed after both members published\n');fresh(source,publications,True)
            put(source,'Ref/optional-serial.inc','// optional file now exists\n');fresh(source,publications,False)
            write(source,'DELETE',source.prefix+'/file?path='+quote('Ref/optional-serial.inc'),None);fresh(source,publications,True)
            put(source,shared,include.replace('0.01','0.02'));fresh(source,publications,False)
            put(source,shared,include);fresh(source,publications,True)
            delivery=api.call('GET',value.prefix+'/testing/same-case-batch/'+batch['id'])
            assert all(j['mode']=='redis' and j['phase']=='succeeded' and j['worker'] in worker_ids for j in delivery['jobs'])
            # Explicitly run again in the same two directories. The original
            # complete seal contains old results; only private known outputs retire.
            old_publications=list(publications)
            replacement=source.call('POST',source.prefix+'/jobs:batch',request,expect=202,extra_headers={'Idempotency-Key':uuid.uuid4().hex})
            def replacement_terminal():
                current=source.call('GET',source.prefix+'/job-batches/'+replacement['id'])
                assert current['status']!='failed','Actual replacement serial batch failed'
                return current if current['status']=='succeeded' else None
            wait_for(replacement_terminal,'Actual replacement serial batch did not finish',360)
            replacement_publications=[]
            for jid,name,old_pub in zip([j['id'] for j in replacement['jobs']],['serial-a','serial-b'],old_publications):
                drained(source,jid);job=source.call('GET','/jobs/'+str(jid))
                assert job['attempt']['outputs']['completion']['alias_error'] is None
                pub=job['attempt']['outputs']['publication']['id'];assert pub!=old_pub
                replacement_publications.append(pub)
                d=api.call('GET','/testing/workflow-publication/'+pub)['manifest']['descriptor']
                assert d['prepared_configuration']['retired_files'] and d['deleted_files']=={}
                assert any('case-query-retire.' in r['path'] for r in d['query_receipt']['case_queries']['evidence']['records'])
                assert 'SERIAL_RETIRED '+str(len(d['prepared_configuration']['retired_files'])) in source.call('GET','/jobs/'+str(jid)+'/log?offset=0')['chunk']
                assert len(vectors(source.call('GET',source.prefix+'/raw?path='+quote('C-run/'+name+'/0.5/U'),raw=True)))==400
                fresh(source,[pub],True)
            record({'stage':'explicit_fresh_serial_replacement','old_complete_seal_retained':True,'private_retirement_actually_traced':True,
                'two_new_attempts_and_publications':True,'new_batch_id':replacement['id']})
            publications=replacement_publications
            history=source.call('GET',source.prefix+'/job-batches/'+batch['id']);before=[j['id'] for j in source.call('GET',source.prefix+'/jobs')]
            pid=source.call('GET','/testing/workflow-api-owner')['pid'];kill_owned_api(source,pid)
            record({'stage':'actual_same_case_SERIAL_batch','batch_id':batch['id'],'actual_members':proofs,'maximum_claims':maximum,
                'actual_workers':worker_ids,'actual_delivery':delivery,'velocity_absolute_error':relative,'half_batch_rolled_back':True,
                'independent_source_freshness':True,'unqueried_change_accepted':True,'optional_file_addition_stale':True,
                'one_member_input_does_not_stale_other':True,
                'queried_cross_run_change_stale':True,'source_API_killed':pid})
        if store:assert not (recovery.root/'s3-cache').exists()
        with recovery:
            assert recovery.call('GET',recovery.prefix+'/job-batches/'+batch['id'])==history
            assert recovery.call('POST',recovery.prefix+'/jobs:batch',request,expect=202,extra_headers={'Idempotency-Key':key})['id']==batch['id']
            assert [j['id'] for j in recovery.call('GET',recovery.prefix+'/jobs')]==before
            fresh(recovery,publications,True);workers.stop()
            record({'stage':'restart_without_replay','original_jobs_preserved':True,'same_key_same_batch':True,'freshness_preserved':True,'independent_empty_S3_cache':bool(store)})
        report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,'workers':workers.receipts,
            'scope':'Same-case registered serial batch, explicit fresh rerun and complete-source queried policy. Professional running-cancel/death matrix and complete native recovery remain.'}
        (api.root/'same-case-serial-batch.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        api.create_case('Independent same-case serial evidence readback');return report
    except BaseException as error:
        (api.root/'failed-same-case-serial-full.json').write_text(json.dumps({'type':type(error).__name__,
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
                        'Owned serial batch cleanup did not settle',120)
        except Exception as error:cleanup_errors.append({'jobs':type(error).__name__})
        finally:
            try:workers.stop()
            except Exception as error:cleanup_errors.append({'workers':type(error).__name__})
            (api.root/'same-case-serial-worker-stops.json').write_text(json.dumps(workers.receipts,indent=2),encoding='utf8')
            (api.root/'same-case-serial-cleanup.json').write_text(json.dumps({'errors':cleanup_errors,'original_error_preserved':primary is not None},indent=2),encoding='utf8')
        if cleanup_errors and primary is None:raise RuntimeError('Owned serial batch cleanup failed; inspect cleanup receipt')


async def main():
    root=Path(tempfile.mkdtemp(prefix='same-case-serial-redis-',dir=SERVER.parents[1]/'tmp'));broker=Broker(root);passed=False
    print('SAME_CASE_SERIAL_OWNED_REDIS '+str(root),flush=True)
    try:
        await broker.start()
        await live_main(selected_override=partial(scenario,broker=broker),fixture_override=FIXTURE,
            store_volume_max=16,environment_overrides=broker.settings());passed=True
    finally:
        await broker.stop()
        (root/'result.json').write_text(json.dumps({'passed':passed,'mock_llm_calls':0,'actual_Redis_births':broker.births,
            'actual_Redis_stops':broker.stops},indent=2),encoding='utf8')
if __name__=='__main__':asyncio.run(main())
