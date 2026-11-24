"""Real workflow API/native checks and explicit SQL read-integrity faults.

No inference is invoked or substituted. Legacy pagination/fault fixtures never
dispatch; registered creation runs actual CadQuery through production APIs.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testsupport import IsolatedServer, SERVER

ROOT=SERVER.parents[1]
APP='''import json,os,uuid
from datetime import datetime,timezone
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import WorkflowRun,WorkflowStep,Job,JobAttempt,DurableEvent,Workspace,Membership
from app.routers.workspaces import get_owned_case
from app.services import workflow_scheduler as scheduler,quota,sharing
from app.services.workflow_execution import owned
from app.services.process_identity_native import observe

@app.get('/api/v1/test/query-birth')
async def birth():return observe(os.getpid())

@app.post('/api/v1/test/query-runs/{case_id}')
async def runs(case_id:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True);result=[]
        for index in range(3):
            # Known legacy graph has no execution contract: the production
            # ready scanner must not execute these metadata-only fixtures.
            run,_=await scheduler.create_run(db,case_id=case_id,user=user,target='geometry',run_dir='C-run/case-1',
                expected_revision=case.dict_revision,parameters=None,idempotency_key=str(uuid.uuid4()))
            run.created_at=datetime(2020,1,1,tzinfo=timezone.utc);result.append(run.id)
        await db.commit();return result

@app.post('/api/v1/test/query-share/{case_id}')
async def share(case_id:int,body:dict,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True);workspace=await db.get(Workspace,case.workspace_id)
        assert workspace.user_id==user.id
        org=await quota.ensure_user_org(db,user)
        member=await db.scalar(select(Membership).where(Membership.user_id==body['user_id'],Membership.org_id==org.id))
        if member is None:member=Membership(user_id=body['user_id'],org_id=org.id,role=body['role']);db.add(member)
        member.role=body['role'];await sharing.set_share(db,workspace,org.id,'member',True);await db.commit()
        return {'role':member.role}

@app.post('/api/v1/test/query-fault/{run_id}')
async def fault(run_id:str,body:dict,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        run=await owned(db,run_id,user);kind=body['kind']
        if kind=='plan':run.plan_json='[]'
        elif kind=='event':
            event=await db.scalar(select(DurableEvent).where(DurableEvent.run_id==run.id).order_by(DurableEvent.seq))
            event.payload_json='[]'
        elif kind=='foreign_attempt':
            other=await get_owned_case(body['other_case_id'],user,db,write=True)
            step=await db.scalar(select(WorkflowStep).where(WorkflowStep.run_id==run.id,WorkflowStep.step_key=='geometry'))
            job=Job(case_id=other.id,type='shell',status='cancelled',command='HISTORICAL_SQL_FAULT_NEVER_EXECUTE')
            db.add(job);await db.flush()
            attempt=JobAttempt(job_id=job.id,root_job_id=job.id,number=1,phase='cancelled',step_id=step.id)
            db.add(attempt);await db.flush();step.active_attempt_id=attempt.id;step.status='running';run.status='running'
        elif kind in {'completion_pending','completion_original'}:
            step=await db.scalar(select(WorkflowStep).where(WorkflowStep.run_id==run.id,WorkflowStep.step_key=='geometry'))
            attempt=await db.get(JobAttempt,step.active_attempt_id)
            saved=Path.cwd()/('original-completion-'+attempt.id+'.json')
            if kind=='completion_pending':
                saved.write_text(attempt.output_manifest_json,encoding='utf8')
                value=json.loads(attempt.output_manifest_json)
                value['completion']={'schema':'vibeflow.job-completion.v1','state':'pending','settled_at':None,'alias_error':None}
                attempt.output_manifest_json=json.dumps(value)
            else:attempt.output_manifest_json=saved.read_text(encoding='utf8')
        else:raise ValueError('Unknown explicit fixture fault')
        await db.commit();return {'fault':kind,'historical_execution_replayed':False}
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(args,database_url=None):
    from app.services.windows_process_scope import ProcessHandle
    api=IsolatedServer('workflow-query-PG' if database_url else 'workflow-query-SQLite',app='query_fixture:app',database_url=database_url)
    (api.root/'query_fixture.py').write_text(APP,encoding='utf8')
    url=database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix()
    report={'passed':False,'backend':'FS+PostgreSQL' if database_url else 'FS+SQLite', 'real_model_calls':0,'mock_llm_calls':0,
        'review_sha256':sha(args.review),'checks':[],'actual_native_jobs':[],'developer_database_modified':False}
    entered=False;handle=None

    def query(sql,params=None):
        async def read():
            engine=create_async_engine(url)
            try:
                async with engine.connect() as connection:return [list(row) for row in await connection.execute(text(sql),params or {})]
            finally:await engine.dispose()
        return asyncio.run(read())

    def snapshot():
        tables=['jobs','job_attempts','workflow_runs','workflow_steps','durable_events','case_event_counters']
        return {name:hashlib.sha256(json.dumps(query('SELECT * FROM '+name+' ORDER BY '+('case_id' if name=='case_event_counters' else 'id')),
            default=str,sort_keys=True).encode()).hexdigest() for name in tables}

    def wait_run(identifier):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            run=api.call('GET','/workflow-runs/'+identifier)
            if run['status']=='awaiting_confirmation':return run
            time.sleep(.1)
        raise TimeoutError('Actual workflow did not reach its original confirmation boundary')

    def create(body,key):
        run=api.call('POST',api.prefix+'/workflow-runs',body,extra_headers={'Idempotency-Key':key})
        jobs=[item['job_id'] for step in run['steps'] for item in step['attempts']]
        assert len(jobs)==1
        for identifier in jobs:
            report['actual_native_jobs'].append(identifier)
            assert api.wait_job(identifier,timeout=180)['attempt']['process_stop']['verified'] is True
        return wait_run(run['id'])

    try:
        api.__enter__();entered=True;birth=api.call('GET','/test/query-birth');handle=ProcessHandle.query(birth);report['API_birth']=birth
        case=api.create_case('Workflow query actual API');owner=api.token
        written=api.put('A-geo/geometry.py','import cadquery as cq\ndef build_geometry(ctx):\n    return {"query_cube":cq.Workplane("XY").box(1,1,1)}\n')
        if written.get('auto_preview_job'):
            report['actual_native_jobs'].append(written['auto_preview_job'])
            assert api.wait_job(written['auto_preview_job'],timeout=180)['attempt']['process_stop']['verified'] is True
        revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
        body={'target':'geometry','expected_revision':revision,'parameters':{'geometry_mode':'preview'},
            'budget':{'timeout_seconds':180,'max_log_bytes':1048576}}
        key=str(uuid.uuid4());run=create(body,key);identifier=run['id'];base='/workflow-runs/'+identifier
        count=query('SELECT COUNT(*) FROM jobs')[0][0]
        assert api.call('POST',api.prefix+'/workflow-runs',body,extra_headers={'Idempotency-Key':key})['id']==identifier
        assert api.call('POST',api.prefix+'/workflow-runs',dict(body,target='mesh'),extra_headers={'Idempotency-Key':key},expect=409)['detail']['code']=='idempotency_conflict'
        assert query('SELECT COUNT(*) FROM jobs')[0][0]==count
        events=[];cursor=0
        while True:
            page=api.call('GET',base+'/events?limit=2&after='+str(cursor));events+=page['events'];cursor=page['next_after']
            if not page['has_more']:break
        expected=query('SELECT id FROM durable_events WHERE case_id=:case AND run_id=:run ORDER BY seq',{'case':case['id'],'run':identifier})
        assert [row['event_id'] for row in events]==[row[0] for row in expected]
        assert all(row['run_id']==identifier and row['case_id']==case['id'] for row in events)
        assert len({row['event_id'] for row in events})==len(events)
        assert api.call('GET',base+'/events?after=9007199254740991',expect=409)['detail']['code']=='event_cursor_ahead'
        api.call('POST','/test/query-fault/'+identifier,{'kind':'completion_pending'})
        blocked=api.call('GET',base+'/events?limit=200');assert blocked['blocked'] and not blocked['has_more']
        api.call('POST','/test/query-fault/'+identifier,{'kind':'completion_original'})
        continued=api.call('GET',base+'/events?after='+str(blocked['next_after'])+'&limit=200')
        assert not continued['blocked'] and continued['events']
        report['checks'].append({'kind':'actual_create_idempotency_and_run_event_pages','run':identifier,'events':len(events),
            'create_replay_no_new_job':True,'explicit_pending_completion_fault_blocked':True,'original_completion_restored':True})
        fixtures=api.call('POST','/test/query-runs/'+str(case['id']),{})
        found=[];cursor=None;first_cursor=None
        while True:
            path=api.prefix+'/workflow-runs-page?limit=1&status=queued&target=geometry'+('' if cursor is None else '&cursor='+cursor)
            page=api.call('GET',path);found += [row['id'] for row in page['items']];cursor=page['next_cursor']
            if first_cursor is None:first_cursor=cursor
            if cursor is None:break
        assert set(found)==set(fixtures) and len(found)==3
        assert api.call('GET',api.prefix+'/workflow-runs-page?cursor=bad!',expect=409)['detail']['code']=='invalid_cursor'
        assert api.call('GET',api.prefix+'/workflow-runs-page?status=cancelled&target=geometry&cursor='+first_cursor,expect=409)['detail']['code']=='invalid_cursor'
        workspace=api.call('GET','/workspaces')[0]
        other=api.call('POST','/workspaces/'+str(workspace['id'])+'/cases',{'name':'Foreign case boundary'})
        assert api.call('GET','/cases/'+str(other['id'])+'/workflow-runs-page?status=queued&target=geometry&cursor='+first_cursor,expect=409)
        assert api.call('GET',api.prefix+'/workflow-runs-page?limit=101',expect=422)
        report['checks'].append({'kind':'three_tied_timestamp_pages_and_case_filter_bound_cursor','metadata_only_run_ids':fixtures})
        foreign=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123'})
        api.token=foreign['token'];assert api.call('GET',base+'/events',expect=404)
        api.token=owner;api.call('POST','/test/query-share/'+str(case['id']),{'user_id':foreign['user']['id'],'role':'viewer'})
        api.token=foreign['token'];assert api.call('GET',base+'/events')['run_id']==identifier
        assert api.call('GET',api.prefix+'/workflow-runs-page')['items']
        assert api.call('GET',base)['id']==identifier
        assert api.call('POST',base+':cancel',{'expected_version':run['version']},expect=404)
        assert api.call('POST',base+':dispatch',{'expected_version':run['version']},expect=404)
        api.token=owner;api.call('POST','/test/query-share/'+str(case['id']),{'user_id':foreign['user']['id'],'role':'member'})
        api.token=foreign['token'];second=create(body,key);assert second['id']!=identifier
        creators=query('SELECT created_by FROM workflow_runs WHERE id IN (:a,:b)',{'a':identifier,'b':second['id']})
        assert len({row[0] for row in creators})==2
        api.token=owner;report['checks'].append({'kind':'viewer_read_only_foreign_hidden_and_actor_scoped_real_creation_key',
            'first_run':identifier,'other_actor_run':second['id']})
        before=snapshot();page=api.call('GET',base+'/events?limit=200');assert page['events']
        assert api.call('GET',base+'/recovery')['run']['id']==identifier
        assert snapshot()==before
        api.call('POST','/test/query-fault/'+fixtures[0],{'kind':'foreign_attempt','other_case_id':other['id']})
        before=snapshot()
        for suffix,method,payload in [('', 'GET',None),(':cancel','POST',{'expected_version':0}),(':dispatch','POST',{'expected_version':0})]:
            value=api.call(method,'/workflow-runs/'+fixtures[0]+suffix,payload,expect=409)
            assert value['detail']['code']=='evidence_invalid'
        assert snapshot()==before
        api.call('POST','/test/query-fault/'+fixtures[1],{'kind':'plan'});before=snapshot()
        assert api.call('GET','/workflow-runs/'+fixtures[1],expect=409)['detail']['code']=='evidence_invalid'
        context=api.call('GET','/workflow-runs/'+fixtures[1]+'/recovery');assert context['action'] is None and context['run']['id']==fixtures[1]
        assert snapshot()==before
        api.call('POST','/test/query-fault/'+fixtures[2],{'kind':'event'});before=snapshot()
        assert api.call('GET','/workflow-runs/'+fixtures[2]+'/events',expect=409)['detail']['code']=='evidence_invalid'
        assert snapshot()==before
        report['checks'].append({'kind':'malformed_plan_event_and_foreign_attempt_refused_without_business_mutation',
            'synthetic_metadata_only':True,'native_execution_replayed':False})
        # Normal cancellation keeps its original version/replay behavior.
        cancelled=api.call('POST',base+':cancel',{'expected_version':api.call('GET',base)['version']})
        assert cancelled['status']=='cancelled'
        report['checks'].append({'kind':'actual_original_confirmation_wait_cancelled','run':identifier})
        report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1800],
            'frames':[{'file':Path(row.filename).name,'line':row.lineno} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                for identifier in report['actual_native_jobs']:
                    job=api.call('GET','/jobs/'+str(identifier))
                    if job['status'] in {'queued','running'}:
                        api.call('POST','/jobs/'+str(identifier)+':cancel');api.wait_job(identifier,timeout=180,expected='cancelled')
                    assert api.call('GET','/jobs/'+str(identifier))['attempt']['process_stop']['verified'] is True
                report['all_actual_native_stops_verified']=True
            except BaseException as error:report['cleanup_failure']=type(error).__name__
            api.__exit__(None if report.get('functional_passed') else RuntimeError,None,None)
        if handle:handle.wait_exit(15);handle.close();report['API_exit_verified']=True
        report['passed']=bool(report.get('functional_passed') and report.get('API_exit_verified') and 'cleanup_failure' not in report)
        (api.root/'query-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print('WORKFLOW_QUERY_RESULT '+str(api.root/'query-result.json'),flush=True)
    return report,api.root


async def postgres(args):
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    selected=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert selected.drivername=='postgresql+asyncpg' and selected.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_workflowqueries_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_workflowqueries_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='workflow-query-PG-owner-',dir=ROOT/'tmp'))
    kwargs=dict(host=selected.host,port=selected.port or 5432,user=selected.username,password=selected.password,database='postgres',timeout=10)
    admin=await asyncpg.connect(**kwargs);created=False;record={'passed':False,'database':name,'developer_database_modified':False}
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=selected.set(database=name).render_as_string(hide_password=False)
        result=await asyncio.to_thread(subprocess.run,[sys.executable,'-m','alembic','-c',str(SERVER/'alembic.ini'),'upgrade','head'],
            cwd=SERVER,env=dict(os.environ,VIBEFLOW_DATABASE_URL=url),capture_output=True)
        (root/'migration.log').write_bytes(result.stdout+result.stderr)
        assert result.returncode==0,'Disposable PostgreSQL schema upgrade failed'
        report,evidence=await asyncio.to_thread(verify,args,url);record.update(passed=report['passed'],evidence=str(evidence),incremental_upgrade_passed=True)
    except BaseException as error:record['failure_type']=type(error).__name__
    finally:
        try:
            if created:await admin.execute('DROP DATABASE "'+name+'"')
        finally:await admin.close()
        if created:
            probe=await asyncpg.connect(**kwargs)
            try:record['temporary_database_absent']=await probe.fetchval('SELECT datname FROM pg_database WHERE datname=$1',name) is None
            finally:await probe.close()
        (root/'result.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf8')
    print('WORKFLOW_QUERY_PG_OWNER_RESULT '+str(root/'result.json'),flush=True)
    return 0 if record['passed'] and record.get('temporary_database_absent') else 1


def run():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True);parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    if args.postgres:return asyncio.run(postgres(args))
    report,_=verify(args);return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
