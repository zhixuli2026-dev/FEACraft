"""Legacy persisted Jobs/logs/batches and durable metadata through real HTTP APIs."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import uuid

from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic

FIXTURE = '''
import json,uuid
from fastapi import Depends
from app.main import app
from app.db import get_db
from app.deps import get_current_user
from app.models import Job,JobAttempt,WorkflowRun,WorkflowStep
from app.routers.workspaces import get_owned_case
from app.services.quota import ensure_user_org
from app.services.durable_events import now
from app.storage import put_text
@app.post('/api/v1/fixture/{case_id}')
async def seed(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(case_id,user,db)
    org=await ensure_user_org(db,user)
    jobs=[]
    for status in ('succeeded','failed','cancelled','interrupted'):
        job=Job(case_id=case.id,type='shell',command='legacy '+status,status=status,
            exit_code=0 if status=='succeeded' else 7,ended_at=now(),log_s3_key='old-logs/'+status+'.log')
        db.add(job);await db.flush();jobs.append(job)
        put_text(case.s3_prefix,job.log_s3_key,'旧日志 '+status+'\\nEnd\\n')
    missing=Job(case_id=case.id,type='shell',command='legacy missing log',status='failed')
    db.add(missing);await db.flush();jobs.append(missing)
    run=WorkflowRun(case_id=case.id,org_id=org.id,created_by=user.id,target='report',status='running',
        request_hash='a'*64,expected_revision=0,run_dir='C-run/test')
    db.add(run);await db.flush()
    step=WorkflowStep(run_id=run.id,step_key='report',handler='report',status='finalizing')
    db.add(step);await db.flush()
    modern=Job(case_id=case.id,type='shell',command='modern finalizing',status='running')
    db.add(modern);await db.flush();jobs.append(modern)
    attempt=JobAttempt(job_id=modern.id,number=1,step_id=step.id,phase='finalizing')
    db.add(attempt);await db.commit()
    from app.services.job_recovery_notice import rows,record
    await record(db,await rows(db,modern.id),'publication_recovery_required')
    await db.commit()
    batch_id=uuid.uuid4().hex
    manifest=dict(id=batch_id,case_id=case.id,created_at=now().isoformat(),max_concurrent=2,job_ids=[j.id for j in jobs])
    put_text(case.s3_prefix,'.vibeflow/job-batches/'+batch_id+'.json',json.dumps(manifest))
    put_text(case.s3_prefix,'.vibeflow/job-batches/index.json',json.dumps([batch_id]))
    return dict(jobs=[j.id for j in jobs],batch_id=batch_id,run_id=run.id,step_id=step.id,attempt_id=attempt.id)
@app.post('/api/v1/fixture/{case_id}/cross-link/{other_id}')
async def cross_link(case_id:int,other_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    from sqlalchemy import select
    await get_owned_case(case_id,user,db);await get_owned_case(other_id,user,db)
    run=await db.scalar(select(WorkflowRun).where(WorkflowRun.case_id==other_id))
    step=await db.scalar(select(WorkflowStep).where(WorkflowStep.run_id==run.id))
    attempt=await db.scalar(select(JobAttempt).join(Job,JobAttempt.job_id==Job.id).where(Job.case_id==case_id))
    attempt.step_id=step.id;await db.commit()
    return {'ok':True}
'''


def verify(database_url=None):
    server=IsolatedServer('job-query-compat',app='query_fixture:app',database_url=database_url)
    (server.root/'query_fixture.py').write_text(FIXTURE,encoding='utf8')
    checks=[]
    with server as api:
        case=api.create_case('job query compatibility')
        fixture=api.call('POST',api.prefix.replace('/cases/','/fixture/'))
        listed=api.call('GET',api.prefix+'/jobs')
        batch=api.call('GET',api.prefix+'/job-batches/'+fixture['batch_id'])
        batches=api.call('GET',api.prefix+'/job-batches')
        assert batches[0]==batch
        for index,status in enumerate(('succeeded','failed','cancelled','interrupted')):
            job_id=fixture['jobs'][index]
            detail=api.call('GET',f'/jobs/{job_id}')
            for row in (detail,next(j for j in listed if j['id']==job_id),batch['jobs'][index]):
                assert row['status']==status and row['exit_code']==(0 if status=='succeeded' else 7),row
                assert row['attempt'] is None and row['phase'] is None and row['run_id'] is None,row
                assert isinstance(row['ended_at'],(int,float))
            expected='旧日志 '+status+'\nEnd\n'
            assert detail['log_backlog']==expected
            log=api.call('GET',f'/jobs/{job_id}/log')
            assert log==dict(offset=len(expected.encode()),chunk=expected,status=status),log
            assert api.call('GET',f'/jobs/{job_id}/log?offset={log["offset"]}')['chunk']==''
            raw=api.call('GET',f'/jobs/{job_id}/log-stream',raw=True).decode()
            events=[json.loads(line[6:]) for line in raw.splitlines() if line.startswith('data: ')]
            assert ''.join(e.get('text','') for e in events if e['type']=='log')==expected,events
            assert events[-1]['type']=='done' and events[-1]['status']==status,events
            checks.append('legacy_'+status+'_detail_list_batch_poll_SSE')
        missing=api.call('GET',f'/jobs/{fixture["jobs"][4]}')
        assert missing['log_backlog']=='' and missing['ended_at'] is None
        checks.append('legacy_missing_log_and_timestamp')
        job_id=fixture['jobs'][-1]
        detail=api.call('GET',f'/jobs/{job_id}')
        assert detail['recovery']['state']=='requires_attention',detail
        for row in (detail,next(j for j in listed if j['id']==job_id),batch['jobs'][-1]):
            assert row['status']=='running' and row['phase']=='finalizing',row
            assert row['run_id']==fixture['run_id'] and row['step_id']==fixture['step_id'],row
            assert row['attempt']['id']==fixture['attempt_id'] and row['attempt']['number']==1,row
            assert 'claim_token' not in row['attempt'] and 'worker_identity_json' not in row['attempt']
        checks.append('durable_workflow_metadata_without_local_process')
        workspace=api.call('GET','/workspaces')[0]
        other=api.call('POST',f'/workspaces/{workspace["id"]}/cases',{'name':'cross link'})
        api.call('POST',f'/fixture/{other["id"]}')
        api.call('POST',f'/fixture/{case["id"]}/cross-link/{other["id"]}')
        for row in (api.call('GET',f'/jobs/{job_id}'),api.call('GET',api.prefix+'/job-batches/'+fixture['batch_id'])['jobs'][-1]):
            assert row['run_id'] is None and row['step_id'] is None,row
        checks.append('damaged_cross_case_workflow_link_not_disclosed')
        owner_token=api.token
        api.create_case('unrelated owner')
        for path in (f'/jobs/{job_id}',f'/jobs/{job_id}/log',f'/jobs/{job_id}/log-stream',
                f'/cases/{case["id"]}/jobs',f'/cases/{case["id"]}/job-batches',
                f'/cases/{case["id"]}/job-batches/{fixture["batch_id"]}'):
            api.call('GET',path,expect=(403,404))
        api.token=None
        api.call('GET',f'/jobs/{job_id}',expect=401)
        api.token=owner_token
        checks.append('owner_and_anonymous_access_preserved')
    return dict(passed=True,count=len(checks),checks=checks,evidence=str(server.root))


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='job-query-compat-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_query_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('JOB_QUERY_COMPAT_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
