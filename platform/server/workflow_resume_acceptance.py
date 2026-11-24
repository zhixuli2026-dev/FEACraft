"""Actual cancelled CadQuery workflow retry and atomic source-family binding.

No inference is invoked, replaced or simulated. Fault checks alter only request
versions and explicit SQL metadata, never native or model responses.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
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
from workflow_query_acceptance import APP

ROOT = SERVER.parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(args, database_url=None):
    from app.services.windows_process_scope import ProcessHandle
    api = IsolatedServer('workflow-resume-PG' if database_url else 'workflow-resume-SQLite',
        app='resume_fixture:app', database_url=database_url)
    (api.root/'resume_fixture.py').write_text(APP, encoding='utf8')
    url = database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix()
    report = {'passed':False, 'backend':'FS+PostgreSQL' if database_url else 'FS+SQLite',
        'real_model_calls':0, 'mock_llm_calls':0, 'checks':[], 'actual_native_jobs':[],
        'review_sha256':sha(args.review), 'developer_database_modified':False}
    entered = False; handle = None

    def query(sql, params=None):
        async def read():
            engine = create_async_engine(url)
            try:
                async with engine.connect() as connection:
                    return [list(row) for row in await connection.execute(text(sql), params or {})]
            finally:
                await engine.dispose()
        return asyncio.run(read())

    def count():
        return query('SELECT COUNT(*) FROM jobs')[0][0], query('SELECT COUNT(*) FROM workflow_runs')[0][0]

    def wait_run(identifier, statuses, timeout=180):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            run = api.call('GET','/workflow-runs/'+identifier)
            if run['status'] in statuses:
                return run
            time.sleep(.2)
        raise TimeoutError('Actual workflow did not reach '+str(statuses))

    def wait_stopped(identifier, expected='succeeded', timeout=180):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            value=api.call('GET','/jobs/'+str(identifier))
            attempt=value.get('attempt') or {}; stop=attempt.get('process_stop') or {}
            if value['status']==expected and attempt.get('phase')==expected and stop.get('verified') is True:
                return value
            if value.get('completion_error'):
                raise RuntimeError('Actual finalization failed: '+value['completion_error'])
            if value['status'] in {'failed','interrupted'} and value['status']!=expected:
                raise AssertionError('Actual native task failed: '+str(identifier))
            time.sleep(.1)
        raise TimeoutError('Actual native completion/stop did not settle: '+str(identifier))

    try:
        api.__enter__(); entered=True
        birth=api.call('GET','/test/query-birth'); handle=ProcessHandle.query(birth); report['API_birth']=birth
        api.create_case('Actual atomic workflow retry'); owner=api.token
        written=api.put('A-geo/geometry.py',
            'import time\nimport cadquery as cq\ndef build_geometry(ctx):\n'
            '    time.sleep(8)\n    return {"resume_cube":cq.Workplane("XY").box(1,1,1)}\n')
        if written.get('auto_preview_job'):
            identifier=written['auto_preview_job']; report['actual_native_jobs'].append(identifier)
            wait_stopped(identifier)
        revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
        run=api.call('POST',api.prefix+'/workflow-runs',{'target':'geometry', 'expected_revision':revision,
            'parameters':{'geometry_mode':'export'}, 'budget':{'timeout_seconds':90,'max_log_bytes':1048576}},
            extra_headers={'Idempotency-Key':str(uuid.uuid4())})
        old_id=run['id']; base='/workflow-runs/'+old_id
        attempt=next(item for step in run['steps'] for item in step['attempts'])
        job_id=attempt['job_id']; report['actual_native_jobs'].append(job_id)
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            status=api.call('GET','/jobs/'+str(job_id))
            if status['attempt']['phase']=='running' and status['attempt'].get('process_identity'):break
            time.sleep(.1)
        else:raise TimeoutError('Actual source worker never entered running')
        report['source_running_observed']={'job_id':job_id,'phase':status['attempt']['phase']}
        current=api.call('GET',base)
        api.call('POST',base+':cancel',{'expected_version':current['version']})
        wait_stopped(job_id,expected='cancelled')
        original=wait_run(old_id,{'cancelled'})
        state=api.call('GET',api.prefix+'/caseDict')
        if state['workflow_state']!='A':api.advance('A')
        plan=api.call('GET',base+'/resume-plan')
        selected=next(step for step in plan['steps'] if step['retry_step']['available'])
        assert not selected['checkpoint']['available']
        body={key:plan[key] for key in ('expected_version','expected_revision')}
        body.update({key:selected[key] for key in ('source_step_id','expected_step_version',
            'source_attempt_id','expected_attempt_version')})
        body.update(mode='retry_step',confirmed=True)
        source_rows={
            'run':query('SELECT * FROM workflow_runs WHERE id=:id',{'id':old_id}),
            'steps':query('SELECT * FROM workflow_steps WHERE run_id=:id ORDER BY id',{'id':old_id}),
            'job':query('SELECT * FROM jobs WHERE id=:id',{'id':job_id}),
            'attempt':query('SELECT * FROM job_attempts WHERE id=:id',{'id':selected['source_attempt_id']})}
        before=count()
        for override, code in [({'expected_version':body['expected_version']+1},'state_conflict'),
                ({'expected_revision':body['expected_revision']+1},'revision_conflict'),
                ({'confirmed':False},'confirmation_required'),
                ({'mode':'checkpoint','checkpoint_time':'1'},'checkpoint_required')]:
            error=api.call('POST',base+':resume',dict(body,**override),expect=409,
                extra_headers={'Idempotency-Key':str(uuid.uuid4())})
            assert error['detail']['code']==code,error
        assert count()==before
        report['checks'].append({'kind':'real_cancelled_source_and_invalid_requests_no_allocation','source_run_id':old_id})
        key=str(uuid.uuid4()); busy=[]
        def submit():
            deadline=time.monotonic()+30
            while True:
                result=api.call('POST',base+':resume',body,expect=(202,409),extra_headers={'Idempotency-Key':key})
                issue=result.get('detail')
                if not issue:
                    return result
                if issue.get('code') not in {'storage_quota_busy','validation_busy'}:
                    raise AssertionError('Actual workflow resume refused: '+json.dumps(result))
                busy.append(issue['code'])
                if time.monotonic()>=deadline:
                    raise TimeoutError('Actual storage admission did not settle; original resume key retained')
                time.sleep(.1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(lambda _:submit(),range(2)))
        assert responses[0]['id']==responses[1]['id']!=old_id
        new_id=responses[0]['id']; new_base='/workflow-runs/'+new_id
        assert count()==(before[0]+1,before[1]+1)
        jobs=[item['job_id'] for step in responses[0]['steps'] for item in step['attempts']]
        assert len(jobs)==1; child_id=jobs[0]; report['actual_native_jobs'].append(child_id)
        done=wait_stopped(child_id)
        assert done['attempt']['process_stop']['verified'] is True
        new=wait_run(new_id,{'awaiting_confirmation'})
        assert new['resumption']['source_run_id']==old_id and new['resumption']['mode']=='retry_step'
        assert new['resumption']['checkpoint_time'] is None
        assert next(step for step in new['steps'] if step['key']=='confirm_B')['status']=='awaiting_confirmation'
        child=query('SELECT id,step_id,root_job_id,retry_of_attempt_id FROM job_attempts WHERE job_id=:id',{'id':child_id})[0]
        parent=query('SELECT root_job_id FROM job_attempts WHERE id=:id',{'id':selected['source_attempt_id']})[0]
        assert child[2]==parent[0] and child[3]==selected['source_attempt_id']
        links=query('SELECT run_id,step_id,attempt_id FROM durable_events WHERE attempt_id=:id AND type IN '
            "('attempt.created','attempt.delivery_requested','workflow.resumed')",{'id':child[0]})
        assert len(links)==3 and all(row==[new_id,child[1],child[0]] for row in links),links
        actual=api.call('GET',new_base+'/events?limit=200')
        assert any(event['type']=='workflow.resumed' for event in actual['events'])
        report['checks'].append({'kind':'concurrent_same_key_one_atomic_run_attempt_and_linked_delivery',
            'new_run_id':new_id,'new_job_id':child_id,'all_three_admission_events_linked':True,
            'actual_publication':done['attempt']['outputs']['publication'],'stage_confirmation_not_automatic':True,
            'same_key_pre_admission_busy_retries':busy})
        assert submit()['id']==new_id and count()==(before[0]+1,before[1]+1)
        lookup=api.call('POST',base+'/recovery-request',{'key':key,'action':'resume','payload':body})
        assert lookup['found'] and lookup['run']['id']==new_id
        assert api.call('POST',base+':resume',dict(body,expected_version=body['expected_version']+1),expect=409,
            extra_headers={'Idempotency-Key':key})['detail']['code']=='idempotency_conflict'
        context=api.call('GET',base+'/recovery')
        assert any(row['id']==new_id for row in context['successors']['items'])
        assert api.call('GET',new_base+'/recovery')['source']['id']==old_id
        api.call('POST',new_base+':cancel',{'expected_version':new['version']})
        assert api.call('POST',base+':resume',body,expect=409,
            extra_headers={'Idempotency-Key':str(uuid.uuid4())})['detail']['code']=='state_conflict'
        after_rows={
            'run':query('SELECT * FROM workflow_runs WHERE id=:id',{'id':old_id}),
            'steps':query('SELECT * FROM workflow_steps WHERE run_id=:id ORDER BY id',{'id':old_id}),
            'job':query('SELECT * FROM jobs WHERE id=:id',{'id':job_id}),
            'attempt':query('SELECT * FROM job_attempts WHERE id=:id',{'id':selected['source_attempt_id']})}
        assert source_rows==after_rows
        report['checks'].append({'kind':'receipt_replay_exact_lookup_lineage_and_original_history_unchanged'})
        foreign=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123'})
        api.token=foreign['token'];api.call('GET',base+'/resume-plan',expect=404)
        api.call('POST',base+':resume',body,expect=404,extra_headers={'Idempotency-Key':key})
        api.token=owner;api.call('POST','/test/query-share/'+str(api.case_id),{'user_id':foreign['user']['id'],'role':'viewer'})
        api.token=foreign['token'];assert api.call('GET',base+'/resume-plan')['source_run_id']==old_id
        api.call('POST',base+':resume',body,expect=404,extra_headers={'Idempotency-Key':key})
        api.token=owner
        report['checks'].append({'kind':'foreign_hidden_and_viewer_cannot_replay_writing_receipt'})
        report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1800],
            'frames':[{'file':Path(row.filename).name,'line':row.lineno} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                if hasattr(api,'case_id'):
                    for job in api.call('GET',api.prefix+'/jobs'):
                        if job['id'] not in report['actual_native_jobs']:
                            report['actual_native_jobs'].append(job['id'])
                for identifier in report['actual_native_jobs']:
                    job=api.call('GET','/jobs/'+str(identifier))
                    phase=(job.get('attempt') or {}).get('phase')
                    if job['status'] in {'queued','running'} or phase not in {'succeeded','failed','cancelled','interrupted'}:
                        api.call('POST','/jobs/'+str(identifier)+':cancel');wait_stopped(identifier,expected='cancelled')
                    else:wait_stopped(identifier,expected=job['status'])
                report['all_actual_native_stops_verified']=True
            except BaseException as error:report['cleanup_failure']=type(error).__name__
            api.__exit__(None if report.get('functional_passed') else RuntimeError,None,None)
        if handle:
            handle.wait_exit(15);handle.close();report['API_exit_verified']=True
        report['passed']=bool(report.get('functional_passed') and report.get('API_exit_verified') and 'cleanup_failure' not in report)
        (api.root/'resume-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print('WORKFLOW_RESUME_RESULT '+str(api.root/'resume-result.json'),flush=True)
    return report, api.root


async def postgres(args, *, verify_callback=None):
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    selected=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert selected.drivername=='postgresql+asyncpg' and selected.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_workflowresume_'+uuid.uuid4().hex
    assert re.fullmatch('tmp_vf_workflowresume_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='workflow-resume-PG-owner-',dir=ROOT/'tmp'))
    kwargs=dict(host=selected.host,port=selected.port or 5432,user=selected.username,password=selected.password,database='postgres',timeout=10)
    admin=await asyncpg.connect(**kwargs);created=False
    record={'passed':False,'database':name,'developer_database_modified':False}
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=selected.set(database=name).render_as_string(hide_password=False)
        result=await asyncio.to_thread(subprocess.run,[sys.executable,'-m','alembic','-c',str(SERVER/'alembic.ini'),'upgrade','head'],
            cwd=SERVER,env=dict(os.environ,VIBEFLOW_DATABASE_URL=url),capture_output=True)
        (root/'migration.log').write_bytes(result.stdout+result.stderr)
        assert result.returncode==0,'Disposable PostgreSQL migration failed'
        report,evidence=await asyncio.to_thread(verify_callback or verify,args,url)
        record.update(passed=report['passed'],evidence=str(evidence),incremental_upgrade_passed=True)
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
    print('WORKFLOW_RESUME_PG_OWNER_RESULT '+str(root/'result.json'),flush=True)
    return 0 if record['passed'] and record.get('temporary_database_absent') else 1


def run():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review',type=Path,required=True);parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    if args.postgres:return asyncio.run(postgres(args))
    report,_=verify(args);return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
