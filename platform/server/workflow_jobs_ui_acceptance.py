"""Real workflow UI: actual CadQuery outcomes, finalization barrier and cancellation.

The barrier only delays the real post-native finalizer. Legacy pagination and
interrupted rows are explicitly metadata/protocol fixtures and never dispatch.
No model response, native execution result or inference is substituted.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import traceback
import uuid

import httpx
from testsupport import IsolatedServer, SERVER

ROOT = SERVER.parents[1]
APP = '''import asyncio,json,os,uuid
from datetime import datetime,timezone
from pathlib import Path
from fastapi import Depends
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import workflow_scheduler as scheduler,finalizing_stage
from app.services.process_identity_native import observe

original_begin=finalizing_stage.begin
async def held_begin(proc):
    stage=await original_begin(proc)
    if Path('hold-finalizing').exists():
        Path('actual-finalizing.json').write_text(json.dumps({'job_id':proc.job_id,'native_status':proc.status,'native_exit_code':proc.exit_code}))
        deadline=asyncio.get_running_loop().time()+240
        while not Path('release-finalizing').exists():
            if asyncio.get_running_loop().time()>deadline:raise TimeoutError('Explicit UI finalization barrier expired')
            await asyncio.sleep(.1)
    return stage
finalizing_stage.begin=held_begin

@app.get('/api/v1/test/workflow-ui-birth')
async def birth():return observe(os.getpid())

@app.post('/api/v1/test/workflow-ui-rows/{case_id}')
async def rows(case_id:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True);result=[]
        for index in range(23):
            run,_=await scheduler.create_run(db,case_id=case_id,user=user,target='geometry',run_dir='C-run/case-1',
                expected_revision=case.dict_revision,parameters=None,idempotency_key=str(uuid.uuid4()))
            run.created_at=datetime(2020,1,1,tzinfo=timezone.utc)
            if index==0:
                run.status='interrupted';run.error_code='UI_PROTOCOL_FIXTURE'
                run.detail='Metadata-only interruption fixture; no native interruption is claimed.'
            result.append({'id':run.id,'status':run.status})
        await db.commit();return result
'''


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services.process_identity_native import observe
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args(); review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items(): assert sha(ROOT/name)==digest,name
    api=IsolatedServer('workflow-jobs-UI',app='workflow_ui_fixture:app')
    (api.root/'workflow_ui_fixture.py').write_text(APP,encoding='utf8')
    report={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'actual_native_jobs':[],
        'review_sha256':sha(args.review),'metadata_protocol_fixture_rows':[], 'native_stops':{}}
    entered=False; api_handle=preview=preview_handle=preview_log=None
    case_ids=[]

    def save(): (api.root/'UI-progress.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    def stopped(identifier,expected=None):
        until=time.monotonic()+180
        while time.monotonic()<until:
            job=api.call('GET','/jobs/'+str(identifier));attempt=job.get('attempt') or {}
            if job['status'] in {'succeeded','failed','cancelled','interrupted'} and attempt.get('phase')==job['status'] and (attempt.get('process_stop') or {}).get('verified') is True:
                if expected is not None: assert job['status']==expected,(identifier,job['status'],job.get('completion_error'))
                report['native_stops'][str(identifier)]={'status':job['status'],'attempt_id':attempt['id'],'stop':attempt['process_stop']}
                return job
            if job.get('completion_error'): raise RuntimeError('Actual native finalization error: '+job['completion_error'])
            time.sleep(.15)
        raise TimeoutError('Actual native stop not verified: '+str(identifier))
    def discover():
        for case_id in case_ids:
            for job in api.call('GET','/cases/'+str(case_id)+'/jobs'):
                if job['id'] not in report['actual_native_jobs']: report['actual_native_jobs'].append(job['id'])
    def wait_file(name,seconds=600):
        until=time.monotonic()+seconds
        while not (api.root/name).exists():
            assert time.monotonic()<until and api.proc.poll() is None and (preview is None or preview.poll() is None),name
            time.sleep(.15)
        return json.loads((api.root/name).read_text(encoding='utf-8-sig'))
    def workflow(case_id):
        revision=api.call('GET','/cases/'+str(case_id)+'/caseDict')['dict_revision']
        value=api.call('POST','/cases/'+str(case_id)+'/workflow-runs',{'target':'geometry','expected_revision':revision,
            'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':180}},extra_headers={'Idempotency-Key':str(uuid.uuid4())})
        discover(); return value

    try:
        api.__enter__();entered=True
        report['API_birth']=api.call('GET','/test/workflow-ui-birth');api_handle=ProcessHandle.query(report['API_birth'])
        case=api.create_case('Workflow finalization and pagination');case_ids.append(case['id'])
        user=api.call('GET','/auth/me'); workspace=api.call('GET','/workspaces')[0]
        api.put('A-geo/geometry.py','import cadquery as cq\nimport time\ntime.sleep(8)\nresult = cq.Workplane("XY").box(12, 8, 4)\n')
        discover()
        for identifier in report['actual_native_jobs']: stopped(identifier,'succeeded')
        fixtures=api.call('POST','/test/workflow-ui-rows/'+str(case['id']),{});report['metadata_protocol_fixture_rows']=fixtures
        (api.root/'hold-finalizing').write_text('Actual native output only',encoding='ascii')
        actual=workflow(case['id']); boundary=wait_file('actual-finalizing.json',120)
        assert boundary['native_status']=='succeeded' and boundary['native_exit_code']==0
        detail=api.call('GET','/workflow-runs/'+actual['id'])
        geometry=next(step for step in detail['steps'] if step['key']=='geometry')
        assert geometry['active_attempt_id'] and any(a['id']==geometry['active_attempt_id'] and a['phase']=='finalizing' for a in geometry['attempts'])
        report['actual_finalizing_boundary']=boundary
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        web=ROOT/'platform/web';preview_log=(api.root/'web-preview.log').open('w',encoding='utf8')
        preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),'preview',
            '--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=web,env=dict(os.environ,VITE_API_TARGET=api.base),
            stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        preview_handle=ProcessHandle.query(observe(preview.pid)); report['preview_birth']=observe(preview.pid)
        setup={'root':str(api.root),'web':'http://127.0.0.1:'+str(port),'email':user['email'],
            'case_id':case['id'],'run_id':actual['id'],'job_id':boundary['job_id'],'fixtures':fixtures}
        (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2),encoding='utf8');save()
        print('WORKFLOW_JOBS_UI_READY '+str(api.root),flush=True)
        first=wait_file('browser-phase-one.json');assert first['passed'];report['browser_phase_one']=first
        (api.root/'release-finalizing').write_text('release',encoding='ascii')
        (api.root/'hold-finalizing').unlink()
        stopped(boundary['job_id'],'cancelled')
        until=time.monotonic()+30
        while api.call('GET','/workflow-runs/'+actual['id'])['status']!='cancelled':
            assert time.monotonic()<until;time.sleep(.1)
        succeeded=workflow(case['id'])
        report['success_run_id']=succeeded['id']
        (api.root/'phase-one-settled.json').write_text(json.dumps({'run_id':succeeded['id'],
            'job_id':next(a['job_id'] for step in succeeded['steps'] for a in step['attempts'])}),encoding='utf8')
        second=wait_file('browser-phase-two.json');assert second['passed'];report['browser_phase_two']=second
        cancelled=api.call('GET','/workflow-runs/'+succeeded['id']);assert cancelled['status']=='cancelled'
        failed_case=api.call('POST','/workspaces/'+str(workspace['id'])+'/cases',{'name':'Workflow actual native failure'});case_ids.append(failed_case['id'])
        api.prefix='/cases/'+str(failed_case['id']);api.case_id=failed_case['id']
        api.put('A-geo/geometry.py','import cadquery as cq\nraise RuntimeError("ACTUAL_CADQUERY_INPUT_FAILURE")\n')
        discover()
        for job in api.call('GET',api.prefix+'/jobs'):stopped(job['id'],'failed')
        failed=workflow(failed_case['id'])
        for step in failed['steps']:
            for attempt in step['attempts']:stopped(attempt['job_id'],'failed')
        until=time.monotonic()+30
        while api.call('GET','/workflow-runs/'+failed['id'])['status']!='failed':
            assert time.monotonic()<until;time.sleep(.1)
        setup.update(failed_case_id=failed_case['id'],failed_run_id=failed['id'])
        (api.root/'browser-phase-three-setup.json').write_text(json.dumps(setup,indent=2),encoding='utf8')
        third=wait_file('browser-phase-three.json');assert third['passed'];report['browser_phase_three']=third
        report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1200],
            'frames':[{'file':Path(row.filename).name,'line':row.lineno} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        (api.root/'release-finalizing').write_text('cleanup release',encoding='ascii')
        if entered:
            try:
                discover()
                for case_id in case_ids:
                    for value in api.call('GET','/cases/'+str(case_id)+'/workflow-runs'):
                        if value['status'] not in {'succeeded','failed','cancelled','interrupted'}:
                            api.call('POST','/workflow-runs/'+value['id']+':cancel',{'expected_version':value['version']})
                for identifier in report['actual_native_jobs']:
                    job=api.call('GET','/jobs/'+str(identifier))
                    if job['status'] in {'queued','running'}:api.call('POST','/jobs/'+str(identifier)+':cancel',{})
                    stopped(identifier)
                # The full workbench opens actual PTY sessions independently of jobs.
                sessions=api.call('GET','/terminal/sessions?include_closed=true')['sessions']
                for session in sessions:
                    if session.get('cleanup_verified') is not True:api.call('DELETE','/terminal/sessions/'+session['id'])
                report['terminal_cleanup']=api.call('GET','/terminal/sessions?include_closed=true')['sessions']
                assert all(row.get('cleanup_verified') is True for row in report['terminal_cleanup'])
            except BaseException as error:report['cleanup_failure']=type(error).__name__+': '+str(error)[:500]
        if preview:
            if preview.poll() is None:preview.terminate()
            preview.wait(timeout=15)
        if preview_handle:preview_handle.wait_exit(15);preview_handle.close();report['preview_exit_verified']=True
        if preview_log:preview_log.close()
        if entered:api.__exit__(None if report.get('functional_passed') else RuntimeError,None,None)
        if api_handle:api_handle.wait_exit(15);api_handle.close();report['API_exit_verified']=True
        report['passed']=bool(report.get('functional_passed') and report.get('API_exit_verified') and report.get('preview_exit_verified') and 'cleanup_failure' not in report)
        (api.root/'UI-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print('WORKFLOW_JOBS_UI_RESULT '+str(api.root/'UI-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
