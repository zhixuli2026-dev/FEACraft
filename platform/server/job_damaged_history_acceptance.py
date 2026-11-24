"""Actual historical JSON faults, manual audit and optional native/browser work.

Metadata fixtures never execute their historical command. The optional live
case runs actual CadQuery through normal APIs, with no inference substitute.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import time
import traceback
import uuid

import httpx
from testsupport import IsolatedServer, SERVER

ROOT = SERVER.parents[1]
APP = '''import json,os,uuid
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Job,JobAttempt,JobExecutionHead,ArtifactPublication,NativeSession,Workspace,Membership
from app.routers.workspaces import get_owned_case
from app.services import job_recovery_notice,job_write_checkpoint,process_identity_native,quota,sharing
from app.services.case_write import CaseWriteConflict
from app.services.workflow_state import ExecutionConflict

@app.get('/api/v1/test/damaged-birth')
async def birth():return process_identity_native.observe(os.getpid())

@app.post('/api/v1/test/damaged/{case_id}')
async def seed(case_id:int,user=Depends(get_current_user)):
    result=[]
    async with SessionLocal() as db:
        await get_owned_case(case_id,user,db,write=True)
        for index,(summary,manifest,output,checkpoint,status) in enumerate([
            ('{','{','{','{','cancelled'),('[]','[]','[]','[]','cancelled'),
            ('{}','{}','{}','{}','failed'),('{}','{}','{','{','succeeded')]):
            job=Job(case_id=case_id,type='shell',command='HISTORICAL_METADATA_ONLY_'+str(index),status=status,parsed_summary=summary)
            db.add(job);await db.flush()
            attempt=None
            if index!=2:
                attempt=JobAttempt(job_id=job.id,root_job_id=job.id,number=1,phase=status,input_manifest_json=manifest,
                    output_manifest_json=output,write_checkpoint_json=checkpoint)
                db.add(attempt);await db.flush()
                db.add(JobExecutionHead(job_id=job.id,next_number=2,active_attempt_id=attempt.id))
                await db.flush()
            if index==0:
                db.add(ArtifactPublication(case_id=case_id,kind='geometry',generation=1,attempt_id=attempt.id,
                    immutable_path='.vibeflow/publications/retained-missing-bundle',manifest_sha256='0'*64,status='prepared'))
            await job_recovery_notice.record(db,await job_recovery_notice.rows(db,job.id),'state_recovery_required')
            result.append({'job_id':job.id,'attempt_id':attempt.id if attempt else None,'kind':index})
        await db.commit()
    return result

@app.get('/api/v1/test/damaged-drain/{case_id}')
async def drain(case_id:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True)
        try:await job_write_checkpoint.require_drained_locked(db,case)
        except (CaseWriteConflict,ExecutionConflict,ValueError) as error:return {'allowed':False,'code':getattr(error,'code',type(error).__name__)}
        return {'allowed':True}

@app.post('/api/v1/test/damaged-share/{case_id}')
async def share(case_id:int,body:dict,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True);workspace=await db.get(Workspace,case.workspace_id)
        assert workspace.user_id==user.id
        org=await quota.ensure_user_org(db,user)
        db.add(Membership(user_id=body['user_id'],org_id=org.id,role='viewer'))
        await sharing.set_share(db,workspace,org.id,'member',True);await db.commit()
    return {'shared':True}

@app.get('/api/v1/test/damaged-terminals')
async def terminals(user=Depends(get_current_user)):
    from app.routers.terminal import _sessions
    from app.services.terminal_records import public
    async with SessionLocal() as db:
        return [dict(public(row),owner_available=row.id in _sessions) for row in await db.scalars(select(NativeSession).where(NativeSession.created_by==user.id))
            if json.loads(row.owner_identity_json)==process_identity_native.observe(os.getpid())]
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def history(path, case_id):
    with sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True) as db:
        return {name: db.execute(query, (case_id,)).fetchall() for name, query in [
            ('jobs', 'SELECT * FROM jobs WHERE case_id=? ORDER BY id'),
            ('attempts', 'SELECT * FROM job_attempts WHERE job_id IN (SELECT id FROM jobs WHERE case_id=?) ORDER BY id'),
            ('publications', 'SELECT * FROM artifact_publications WHERE case_id=? ORDER BY id')]}


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--browser',action='store_true')
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    api=IsolatedServer('job-damaged-history',app='damaged_fixture:app')
    (api.root/'damaged_fixture.py').write_text(APP,encoding='utf8')
    report={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'review_sha256':sha(args.review),'checks':[]}
    entered=False;handle=preview=preview_handle=preview_log=None
    try:
        api.__enter__();entered=True;birth=api.call('GET','/test/damaged-birth');handle=ProcessHandle.query(birth);report['API_birth']=birth
        api.create_case('Damaged historical Jobs');owner=api.token;user=api.call('GET','/auth/me')
        damaged_case=api.case_id;fixtures=api.call('POST','/test/damaged/'+str(damaged_case),{})
        original=history(api.root/'test.sqlite',damaged_case)
        rows=api.call('GET',api.prefix+'/jobs');assert len(rows)==4
        for row in fixtures:
            identifier=row['job_id'];current=api.call('GET','/jobs/'+str(identifier))
            if row['kind'] in {0,1,3}:assert current['record_issues']
            plan=api.call('GET','/jobs/'+str(identifier)+'/recovery-disposition')
            assert plan['available_actions']==['retain_for_review'],plan
            if row['kind'] in {0,1,3}:assert 'write_checkpoint_unverified' in plan['issues']
            payload={'attempt_id':plan['attempt_id'],'snapshot_sha256':plan['snapshot_sha256'],'action':'retain_for_review'}
            result=api.call('POST','/jobs/'+str(identifier)+':retain-for-review',payload)
            assert api.call('POST','/jobs/'+str(identifier)+':retain-for-review',payload)==result
            assert result['execution_authorized'] is result['content_drained'] is result['stop_evidence_created'] is False
            assert api.call('GET','/test/damaged-drain/'+str(damaged_case))['allowed'] is False
        assert history(api.root/'test.sqlite',damaged_case)==original
        diagnosis=api.call('GET','/jobs/'+str(fixtures[0]['job_id'])+'/publication-recovery')
        assert diagnosis['database_status']=='prepared' and diagnosis['state']=='legacy_unjournaled'
        report['checks'].append({'kind':'four_damaged_or_legacy_histories_read_and_retained','original_rows_unchanged':True,
            'registration_did_not_authorize_content':True,'publication_diagnosis':diagnosis})
        foreign=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123'})
        api.token=foreign['token'];assert api.call('GET','/jobs/'+str(fixtures[0]['job_id']),expect=404)
        api.token=owner;api.call('POST','/test/damaged-share/'+str(damaged_case),{'user_id':foreign['user']['id']})
        api.token=foreign['token'];assert len(api.call('GET',api.prefix+'/jobs'))==4
        assert api.call('GET','/jobs/'+str(fixtures[0]['job_id']))['id']==fixtures[0]['job_id']
        plan=api.call('GET','/jobs/'+str(fixtures[0]['job_id'])+'/recovery-disposition')
        assert api.call('POST','/jobs/'+str(fixtures[0]['job_id'])+':retain-for-review',
            {'attempt_id':plan['attempt_id'],'snapshot_sha256':plan['snapshot_sha256'],'action':'retain_for_review'},expect=404)
        api.token=owner;report['checks'].append({'kind':'viewer_reads_history_but_cannot_register_foreign_hidden'})
        if args.browser:
            workspace=api.call('GET','/workspaces')[0]
            live_case=api.call('POST','/workspaces/'+str(workspace['id'])+'/cases',{'name':'Actual native polling Jobs'})
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            web=ROOT/'platform/web';preview_log=(api.root/'web-preview.log').open('w',encoding='utf8')
            preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),'preview',
                '--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=web,env=dict(os.environ,VITE_API_TARGET=api.base),
                stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            preview_birth=identity.observe(preview.pid);preview_handle=ProcessHandle.query(preview_birth);report['preview_birth']=preview_birth
            web_url='http://127.0.0.1:'+str(port)
            with httpx.Client(timeout=1,trust_env=False) as client:
                until=time.monotonic()+30
                while True:
                    assert preview.poll() is None and time.monotonic()<until
                    try:
                        if client.get(web_url).status_code==200:break
                    except httpx.HTTPError:pass
                    time.sleep(.1)
            setup={'root':str(api.root),'web':web_url,'email':user['email'],'damaged_case':damaged_case,'fixtures':fixtures,'live_case':live_case['id']}
            (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2),encoding='utf8')
            print('JOB_DAMAGED_BROWSER_READY '+str(api.root),flush=True)
            done=api.root/'browser-finished.json';until=time.monotonic()+1200
            while not done.exists():
                assert preview.poll() is None and api.proc.poll() is None
                if time.monotonic()>until:raise TimeoutError('Actual browser acceptance not finished')
                time.sleep(.2)
            report['browser']=json.loads(done.read_text(encoding='utf8'));assert report['browser']['passed']
            job=api.wait_job(report['browser']['actual_native_job_id'],timeout=180)
            assert job['case_id']==live_case['id'] and job['attempt']['process_stop']['verified'] is True
            assert not job['record_issues']
            assert history(api.root/'test.sqlite',damaged_case)==original
            report['checks'].append({'kind':'actual_native_poll_and_readonly_damaged_browser','job_id':job['id'],'process_stop_verified':True})
        report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1800],
            'frames':[{'file':Path(row.filename).name,'line':row.lineno} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered and args.browser:
            try:
                for row in api.call('GET','/test/damaged-terminals'):
                    if not row['cleanup_verified'] and row['owner_available']:api.call('DELETE','/terminal/sessions/'+row['id'])
                sessions=api.call('GET','/test/damaged-terminals');assert all(row['cleanup_verified'] for row in sessions)
                report['actual_terminal_stops']=sessions
            except BaseException as error:report['cleanup_failure']=type(error).__name__
        if preview:preview.terminate();preview.wait(timeout=15)
        if preview_handle:preview_handle.wait_exit(15);preview_handle.close();report['preview_exit_verified']=True
        if preview_log:preview_log.close()
        if entered:api.__exit__(None if report.get('functional_passed') else RuntimeError,None,None)
        if handle:handle.wait_exit(15);handle.close();report['API_exit_verified']=True
        report['passed']=bool(report.get('functional_passed') and report.get('API_exit_verified') and 'cleanup_failure' not in report)
        (api.root/'damaged-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print('JOB_DAMAGED_RESULT '+str(api.root/'damaged-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
