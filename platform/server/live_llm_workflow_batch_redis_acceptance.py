"""Real owned Redis, arq restarts, typed batch capacity and running cancellation."""
import ast,asyncio,copy,json,os,tempfile,time,uuid
from functools import partial
from pathlib import Path

from redis import Redis
from app.services import process_identity_native as identity
from live_llm_job_repeat_acceptance import main as live_main,put,write
from live_llm_failure_fallback_acceptance import wait_for,kill_owned_api
from live_llm_workflow_batch_cancel_acceptance import FIXTURE as CANCEL_FIXTURE,ancestors
from workflow_batch_redis_support import Broker,Workers
from testsupport import IsolatedServer,SERVER

EXTRA=r'''
import json
from app.models import Plan,WorkflowStep,Job,JobAttempt,Case,Workspace
from app.db import SessionLocal
from app.services import workflow_batches,job_delivery,job_worker_identity

@app.post('/api/v1/testing/batch-quota')
async def actual_batch_quota(body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 amount=body['job_concurrent'];assert type(amount) is int and amount in {1,2}
 plan=await db.scalar(select(Plan).where(Plan.code=='free'));values=json.loads(plan.quotas)
 values['job_concurrent']=amount;plan.quotas=json.dumps(values);await db.commit()
 return {'job_concurrent':amount}

@app.get('/api/v1/testing/batch-delivery/{batch_id}')
async def actual_batch_delivery(batch_id:str,user=Depends(get_current_user),db=Depends(get_db)):
 batch,members=await workflow_batches.owned(db,batch_id,user)
 rows=(await db.execute(select(Job,JobAttempt,WorkflowStep.run_id).select_from(Job)
  .join(JobAttempt,JobAttempt.job_id==Job.id).join(WorkflowStep,WorkflowStep.id==JobAttempt.step_id)
  .where(WorkflowStep.run_id.in_([m.run_id for m in members])).order_by(Job.id))).all()
 result=[]
 for job,attempt,run_id in rows:
  delivery=await job_delivery.intent(db,job,attempt)
  worker=job_worker_identity.document(job,attempt)['process'] if attempt.claim_token else None
  result.append({'job_id':job.id,'case_id':job.case_id,'run_id':run_id,'phase':attempt.phase,'status':job.status,
   'attempt_id':attempt.id,'claimed':attempt.claim_token is not None,'mode':delivery['mode'],
   'worker':worker,'started_at':attempt.started_at.isoformat() if attempt.started_at else None,
   'ended_at':attempt.ended_at.isoformat() if attempt.ended_at else None})
 return {'batch_id':batch.id,'delivery_mode':batch.delivery_mode,'jobs':result}

@app.get('/api/v1/testing/batch-eligibility/{batch_id}')
async def actual_batch_eligibility(batch_id:str,user=Depends(get_current_user),db=Depends(get_db)):
 from app.services.durable_events import lock_case_execution
 from app.services import job_execution,distributed_queue
 batch,members=await workflow_batches.owned(db,batch_id,user)
 owner_id=batch.owner_id;run_ids=[m.run_id for m in members]
 identifiers=list(await db.scalars(select(Job.id).join(JobAttempt,JobAttempt.job_id==Job.id)
  .join(WorkflowStep,WorkflowStep.id==JobAttempt.step_id).where(WorkflowStep.run_id.in_(run_ids))))
 result=[]
 for identifier in identifiers:
  async with SessionLocal() as checked:
   job=await checked.get(Job,identifier);case=await checked.get(Case,job.case_id)
   await lock_case_execution(checked,case.id)
   attempt=await checked.scalar(select(JobAttempt).where(JobAttempt.job_id==identifier).order_by(JobAttempt.number.desc()).limit(1))
   try:
    await job_delivery.require_capacity(checked,case,job,attempt,json.loads(attempt.input_manifest_json)['execution_spec'])
    outcome='eligible'
   except Exception as error:outcome={'type':type(error).__name__,'code':getattr(error,'code',None),'detail':str(error)[:800]}
   result.append({'job_id':identifier,'capacity':outcome})
 cases=list(await db.scalars(select(Case).join(Workspace,Workspace.id==Case.workspace_id).where(Workspace.user_id==owner_id)))
 by_case={c.id:c for c in cases};all_jobs=list(await db.scalars(select(Job).where(Job.case_id.in_(by_case))))
 occupants=[]
 for job in all_jobs:
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
  occupants.append({'job_id':job.id,'case_id':job.case_id,'status':job.status,'phase':attempt.phase,
   'occupying':job_delivery.occupying(by_case[job.case_id],job,attempt),
   'completion_visibility':job_execution.completion_visibility(attempt)})
 return {'capacity':result,'jobs':occupants,'configured_Redis_host':distributed_queue.redis_settings.host,
  'configured_Redis_port':distributed_queue.redis_settings.port}
'''
FIXTURE=CANCEL_FIXTURE+EXTRA


def peer(api,store,broker,role):
    value=IsolatedServer('batch-redis-'+role,app='redis_peer:app')
    shared={k:v for k,v in api.env.items() if k.startswith('VIBEFLOW_')}
    if store:shared.update(store.settings());shared['VIBEFLOW_STORAGE_FS_ROOT']=str(value.root/'s3-cache')
    shared.update(broker.settings());shared['VIBEFLOW_BATCH_TEST_ROLE']=role
    (value.root/'redis_peer.py').write_text('import os\nos.environ.update('+repr(shared)+')\nfrom app.main import app\n'+FIXTURE,encoding='utf8')
    value.token=api.token
    return value


def scenario(api,*,broker,loop,store=None,**_):
    (api.root/'batch-redis-entry-source.py').write_bytes(Path(__file__).read_bytes())
    checks=[];actors=[];releases=[];workers=Workers(api,broker,store)
    client=Redis(host='127.0.0.1',port=broker.port,socket_connect_timeout=2,socket_timeout=2)
    assert client.info('server')['run_id']==broker.births[-1]['run_id']
    def record(value):
        checks.append(value);(api.root/'workflow-batch-redis-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    workspace=api.call('GET','/workspaces')[0]['id']
    def actor(name,hold=False):
        case=api.call('POST','/workspaces/'+str(workspace)+'/cases',{'name':name})
        value=copy.copy(api);value.case_id=case['id'];value.prefix='/cases/'+str(case['id']);actors.append(value)
        marker=api.root/('redis-export-'+str(case['id'])+'.json');release=api.root/('redis-release-'+str(case['id']));releases.append(release)
        body=('import cadquery as cq\nfrom pathlib import Path\nimport json,os,time\n'
            'def build_geometry(ctx):\n shape=cq.Workplane("XY").box(1,2,3)\n if ctx.mode=="export":\n'
            '  Path('+repr(str(marker))+').write_text(json.dumps({"pid":os.getpid(),"actual_shape_built":True}))\n')
        if hold:body+=('  deadline=time.monotonic()+120\n  while not Path('+repr(str(release))+').exists():\n'
            '   if time.monotonic()>deadline:raise TimeoutError("Actual Redis geometry hold expired")\n   time.sleep(.05)\n')
        else:body+='  time.sleep(3)\n'
        body+=' return {"box":shape}\n';ast.parse(body)
        saved=put(value,'A-geo/geometry.py',body)
        if saved.get('auto_preview_job'):
            jid=saved['auto_preview_job'];value.wait_job(jid)
            wait_for(lambda:value.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Actual Redis baseline preview did not drain')
        return value,marker,release
    def body(selected,capacity):
        return {'max_concurrent':capacity,'members':[{'key':'member-'+str(v.case_id),'case_id':v.case_id,'target':'geometry',
            'expected_revision':v.call('GET',v.prefix+'/caseDict')['dict_revision'],'parameters':{'geometry_mode':'export'},
            'budget':{'timeout_seconds':180,'max_log_bytes':16777216}} for v in selected]}
    def snapshot(server,batch):return server.call('GET','/testing/batch-delivery/'+batch['id'])
    def queue_size():return client.zcard('vibeflow:jobs')
    def confirm(server,batch,value):
        summary=next(r for r in batch['members'] if r['case_id']==value.case_id)
        def waiting():
            flow=server.call('GET','/workflow-runs/'+summary['id'])
            assert flow['status'] not in {'failed','interrupted','cancelled'},flow
            return flow if flow['status']=='awaiting_confirmation' else None
        flow=wait_for(waiting,'Real Redis workflow did not reach B',180)
        producer=next(s for s in flow['steps'] if s['key']=='geometry');confirmation=next(s for s in flow['steps'] if s['key']=='confirm_B')
        jid=producer['attempts'][-1]['job_id'];job=server.wait_job(jid)
        wait_for(lambda:server.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Real Redis geometry did not drain')
        manifest=server.call('GET','/testing/workflow-publication/'+producer['publication']['id'])['manifest']
        files=[p for p,r in manifest['files'].items() if p.lower().endswith(('.step','.stp')) and r['size']>100];assert files
        write(server,'POST',value.prefix+'/caseDict:advance',{'state':'B','confirmed':True,
            'expected_revision':confirmation['confirmation_revision'],'workflow_step_id':confirmation['id'],
            'expected_step_version':confirmation['version']})
        return {'job_id':jid,'run_id':flow['id'],'actual_STEP_files':files,'explicit_B':True,'job_status':job['status']}
    source=peer(api,store,broker,'source');recovery=peer(api,store,broker,'recovery')
    try:
        # Saving geometry submits a real Redis preview before the batch exists.
        workers.start()
        first,_,_=actor('Redis original member one');second,_,_=actor('Redis original member two')
        workers.stop()
        api.call('POST','/testing/batch-quota',{'job_concurrent':1})
        with source:
            key=uuid.uuid4().hex;original_body=body([first,second],2)
            original=source.call('POST','/workflow-batches',original_body,expect=202,extra_headers={'Idempotency-Key':key})
            queued=wait_for(lambda:(s if len((s:=snapshot(source,original))['jobs'])==2 else None),'Two original Redis intents not persisted')
            assert queued['delivery_mode']=='redis' and all(r['mode']=='redis' and not r['claimed'] and r['phase']=='queued' for r in queued['jobs'])
            observations=[]
            def original_head():
                amount=queue_size()
                if not observations or observations[-1]!=amount:observations.append(amount)
                return amount==1
            try:wait_for(original_head,'Original single eligible Redis queue head was not enqueued')
            except BaseException:
                diagnostic={'observed_queue_lengths':observations,'current_queue_length':queue_size(),
                    'actual_queued_ids':[v.decode() for v in client.zrange('vibeflow:jobs',0,-1)],
                    'delivery':snapshot(source,original),'eligibility':source.call('GET','/testing/batch-eligibility/'+original['id'])}
                (api.root/'redis-head-failure.json').write_text(json.dumps(diagnostic,indent=2),encoding='utf8')
                raise
            before_jobs=[r['job_id'] for r in queued['jobs']];before_attempts=[r['attempt_id'] for r in queued['jobs']]
            source_pid=source.call('GET','/testing/workflow-api-owner')['pid'];launcher=kill_owned_api(source,source_pid)
            old_birth=broker.births[-1]
            asyncio.run_coroutine_threadsafe(broker.restart(),loop).result(timeout=60)
            assert queue_size()==0
            record({'stage':'actual_API_death_and_Redis_transport_loss','batch_id':original['id'],'original_jobs':before_jobs,
                'original_attempts':before_attempts,'source_API_pid_killed':source_pid,'verified_launcher_pid':launcher,
                'old_Redis':old_birth,'new_Redis':broker.births[-1],'queue_empty_after_real_restart':True})
        if store:assert not (recovery.root/'s3-cache').exists()
        with recovery:
            wait_for(lambda:queue_size()==1,'New API did not restore original SQL delivery intent')
            restored=snapshot(recovery,original)
            assert [r['job_id'] for r in restored['jobs']]==before_jobs and [r['attempt_id'] for r in restored['jobs']]==before_attempts
            assert recovery.call('POST','/workflow-batches',original_body,expect=202,extra_headers={'Idempotency-Key':key})['id']==original['id']
            workers.start();first_workers=workers.identities();observations=[];deadline=time.monotonic()+180
            while True:
                current=snapshot(recovery,original);active=[r for r in current['jobs'] if r['phase'] in {'claimed','starting','running','finalizing','cancelling'}]
                observations.append({'active_jobs':[r['job_id'] for r in active]});assert len(active)<=1
                if all(r['phase']=='succeeded' for r in current['jobs']):break
                assert all(r['phase'] not in {'failed','interrupted','cancelled'} for r in current['jobs']),current
                if time.monotonic()>deadline:raise TimeoutError('Real arq workers did not finish original geometry members')
                time.sleep(.1)
            assert max(len(r['active_jobs']) for r in observations)==1
            assert all(r['worker'] in first_workers for r in current['jobs'])
            ordered=sorted(current['jobs'],key=lambda r:r['started_at']);assert ordered[1]['started_at']>=ordered[0]['ended_at']
            proofs=[confirm(recovery,original,v) for v in [first,second]]
            completed=wait_for(lambda:(b if (b:=recovery.call('GET','/workflow-batches/'+original['id']))['status']=='succeeded' else None),'Restored original Redis batch did not succeed')
            workers.stop();workers.start();second_workers=workers.identities()
            assert all(r not in first_workers for r in second_workers)
            assert snapshot(recovery,original)==current
            assert recovery.call('GET','/workflow-batches/'+original['id'])==completed
            record({'stage':'two_actual_workers_and_owner_capacity','original_jobs_and_attempts_preserved':True,'parent_capacity':2,
                'user_capacity':1,'maximum_observed_claims':1,'actual_claims_nonoverlapping':True,'observations':observations,
                'actual_worker_identities':first_workers,'restarted_worker_identities':second_workers,'actual_geometry_proofs':proofs,
                'completed_history_unchanged_on_worker_restart':recovery.call('GET','/workflow-batches/'+original['id'])==completed,
                'independent_empty_S3_cache':bool(store)})
            api.call('POST','/testing/batch-quota',{'job_concurrent':2})
            a1,m1,_=actor('Redis held batch one',True);a2,m2,_=actor('Redis queued batch two',True)
            heads=[v.call('GET',v.prefix+'/testing/batch-heads') for v in [a1,a2]]
            held=recovery.call('POST','/workflow-batches',body([a1,a2],1),expect=202,extra_headers={'Idempotency-Key':uuid.uuid4().hex})
            wait_for(lambda:m1.exists() or m2.exists(),'Actual arq export did not enter held native body',120)
            assert m1.exists()!=m2.exists();marker=m1 if m1.exists() else m2
            witness=json.loads(marker.read_text());assert witness['actual_shape_built']
            native=identity.observe(witness['pid']);chain=ancestors(witness['pid'])
            assert any(r['pid'] in chain for r in second_workers)
            other,_,_=actor('Redis independent batch can progress')
            independent=recovery.call('POST','/workflow-batches',body([other],1),expect=202,extra_headers={'Idempotency-Key':uuid.uuid4().hex})
            other_proof=confirm(recovery,independent,other)
            held_rows=wait_for(lambda:(s if len((s:=snapshot(recovery,held))['jobs'])==2 else None),'Held batch second Job was not persisted')
            running=[r for r in held_rows['jobs'] if r['phase']=='running'];queued=[r for r in held_rows['jobs'] if r['phase']=='queued']
            assert len(running)==len(queued)==1 and m1.exists()!=m2.exists()
            assert identity.probe(native)['state']=='match'
            answer=api.call('POST','/workflow-batches/'+held['id']+':cancel',{'expected_version':held['version']})
            assert answer['version']==1
            def stopped():
                job=api.call('GET','/jobs/'+str(running[0]['job_id']));attempt=job['attempt']
                if attempt['phase']!='cancelled' or attempt.get('process_stop') is None:return None
                assert attempt['process_stop']['verified'];return job
            cancelled=wait_for(stopped,'Redis worker did not commit verified durable cancellation',90)
            wait_for(lambda:api.call('GET','/testing/workflow-job-drain/'+str(cancelled['id']))['verified'],'Redis cancellation did not drain',90)
            assert identity.probe(native)['state']=='exited'
            never=api.call('GET','/jobs/'+str(queued[0]['job_id']))
            assert never['attempt']['phase']=='cancelled' and never['attempt']['process_identity'] is None and m1.exists()!=m2.exists()
            assert [v.call('GET',v.prefix+'/testing/batch-heads') for v in [a1,a2]]==heads
            assert api.call('POST','/workflow-batches/'+held['id']+':cancel',{'expected_version':held['version']})['version']==1
            (api.root/'redis-batch-cancelled-job.json').write_text(json.dumps(cancelled,indent=2),encoding='utf8')
            record({'stage':'batch_capacity_independent_case_and_worker_cancel','parent_capacity':1,'user_capacity':2,
                'actual_geometry_process':native,'actual_worker_ancestry':chain,'other_batch_completed_while_original_running':True,
                'other_actual_geometry_proof':other_proof,'queued_member_never_started':True,'actual_process_exited':identity.probe(native),
                'old_publication_and_scene_preserved':True,'single_cancel_version':True})
            workers.stop()
        report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,'workers':workers.receipts,
            'scope':'Queued SQL intents survive API death/real Redis restart; two actual workers restarted after completed work; geometry user/batch capacity and running cancellation. MPI/UDF and in-flight worker death remain.'}
        (api.root/'workflow-batch-redis.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        api.create_case('Independent actual Redis workflow batch evidence readback')
        return report
    finally:
        for release in releases:release.touch()
        for v in actors:
            for job in v.call('GET',v.prefix+'/jobs'):
                if job['status'] in {'queued','running'}:
                    v.call('POST','/jobs/'+str(job['id'])+':cancel',{});v.wait_job(job['id'],expected='cancelled',timeout=120)
        workers.stop();client.close()


async def main():
    root=Path(tempfile.mkdtemp(prefix='typed-batch-redis-',dir=SERVER.parents[1]/'tmp'))
    (root/'entry-source.py').write_bytes(Path(__file__).read_bytes())
    (root/'support-source.py').write_bytes(Path(__file__).with_name('workflow_batch_redis_support.py').read_bytes())
    broker=Broker(root);report={'passed':False,'developer_redis_used':False,'mock_llm_calls':0};scenario_passed=False
    print('TYPED_BATCH_OWNED_REDIS '+str(root),flush=True)
    try:
        await broker.start();loop=asyncio.get_running_loop()
        await live_main(selected_override=partial(scenario,broker=broker,loop=loop),fixture_override=FIXTURE,
            store_volume_max=16,environment_overrides=broker.settings())
        scenario_passed=True
    finally:
        try:await broker.stop()
        finally:
            report.update(passed=scenario_passed and len(broker.stops)==len(broker.births) and all(s['verified'] for s in broker.stops),
                actual_Redis_births=broker.births,actual_Redis_stops=broker.stops)
            (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
