"""Actual workflow-to-CasePanel confirmation with real CadQuery and response-loss recovery.

Copies accepted terminal history; never modifies the source database or starts
an inference/model provider. The browser drops only an actual stage HTTP reply.
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

from testsupport import SERVER,IsolatedServer
from case_panel_baseline_inspection import APP

ROOT=SERVER.parents[1]
TABLES=['workflow_runs','workflow_steps','jobs','job_attempts']


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def history(path,limits=None):
    with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as db:
        if limits is None:limits={name:db.execute('SELECT coalesce(max(rowid),0) FROM '+name).fetchone()[0] for name in TABLES}
        hashes={name:hashlib.sha256(repr(db.execute('SELECT * FROM '+name+' WHERE rowid<=? ORDER BY rowid',(limits[name],)).fetchall()).encode()).hexdigest() for name in TABLES}
        return limits,hashes


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services.process_identity_native import observe
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    source=ROOT/'tmp/workflow-jobs-UI-uz2d3e0p';prior=json.loads((source/'UI-result.json').read_text(encoding='utf8'))
    assert prior['passed'] and prior['API_exit_verified'] and prior['preview_exit_verified']
    original=source/'test.sqlite';original_sha=sha(original);limits,before=history(original)
    api=IsolatedServer('case-stage-confirmation',app='case_stage_fixture:app')
    with sqlite3.connect('file:'+original.as_posix()+'?mode=ro',uri=True) as a,sqlite3.connect(api.root/'test.sqlite') as b:a.backup(b)
    assert not any(p.is_symlink() for p in (source/'files').rglob('*'))
    shutil.copytree(source/'files',api.root/'files');(api.root/'case_stage_fixture.py').write_text(APP,encoding='utf8')
    report={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'actual_native_jobs':[],
        'review_sha256':sha(args.review),'original_database_sha256':original_sha,'original_history':before}
    entered=False;api_handle=preview=preview_handle=preview_log=None
    def stopped(identifier):
        until=time.monotonic()+180
        while time.monotonic()<until:
            job=api.call('GET','/jobs/'+str(identifier));attempt=job.get('attempt') or {}
            if job['status'] in {'succeeded','failed','cancelled','interrupted'} and attempt.get('phase')==job['status'] and (attempt.get('process_stop') or {}).get('verified') is True:
                report.setdefault('native_stops',{})[str(identifier)]={'status':job['status'],'stop':attempt['process_stop']};return job
            if job.get('completion_error'):raise RuntimeError('Actual native finalization error: '+job['completion_error'])
            time.sleep(.15)
        raise TimeoutError('Native stop not verified')
    try:
        api.__enter__();entered=True;report['API_birth']=api.call('GET','/test/case-panel-birth');api_handle=ProcessHandle.query(report['API_birth'])
        with sqlite3.connect(api.root/'test.sqlite') as db:email=db.execute('SELECT email FROM users WHERE id=1').fetchone()[0]
        api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token'];user=api.call('GET','/auth/me')
        info=api.call('GET','/cases/1/caseDict');assert info['workflow_state']=='A'
        run=api.call('POST','/cases/1/workflow-runs',{'target':'geometry','expected_revision':info['dict_revision'],
            'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':180}},extra_headers={'Idempotency-Key':str(uuid.uuid4())})
        report['actual_native_jobs']=[a['job_id'] for step in run['steps'] for a in step['attempts']];assert len(report['actual_native_jobs'])==1
        assert stopped(report['actual_native_jobs'][0])['status']=='succeeded'
        until=time.monotonic()+30
        while True:
            run=api.call('GET','/workflow-runs/'+run['id'])
            if run['status']=='awaiting_confirmation':break
            assert time.monotonic()<until;time.sleep(.1)
        step=next(s for s in run['steps'] if s['confirmation_state']=='B')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        web=ROOT/'platform/web';preview_log=(api.root/'web-preview.log').open('w',encoding='utf8')
        preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),'preview','--host','127.0.0.1','--port',str(port),'--strictPort'],
            cwd=web,env=dict(os.environ,VITE_API_TARGET=api.base),stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        report['preview_birth']=observe(preview.pid);preview_handle=ProcessHandle.query(report['preview_birth'])
        setup={'root':str(api.root),'web':'http://127.0.0.1:'+str(port),'email':email,'user_id':user['id'],'case_id':1,
            'run_id':run['id'],'step_id':step['id'],'step_version':step['version'],'revision':info['dict_revision'],'job_id':report['actual_native_jobs'][0]}
        (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2),encoding='utf8');print('CASE_STAGE_READY '+str(api.root),flush=True)
        until=time.monotonic()+1200
        while not (api.root/'browser-finished.json').exists():
            assert time.monotonic()<until and api.proc.poll() is None and preview.poll() is None;time.sleep(.15)
        report['browser']=json.loads((api.root/'browser-finished.json').read_text(encoding='utf8'));assert report['browser']['passed']
        after=api.call('GET','/workflow-runs/'+run['id']);confirmed=next(s for s in after['steps'] if s['id']==step['id'])
        assert after['status']=='succeeded' and confirmed['status']=='succeeded' and confirmed['version']==step['version']+1
        receipt=confirmed['confirmation'];assert receipt=={'user_id':user['id'],'state':'B','from_revision':info['dict_revision'],'to_revision':info['dict_revision']+1}
        assert api.call('GET','/cases/1/caseDict')['workflow_state']=='B'
        assert history(api.root/'test.sqlite',limits)[1]==before;report['copied_old_workflow_job_attempt_rows_unchanged']=True
        report['confirmation_receipt']=receipt;report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1400],'frames':[{'file':Path(row.filename).name,'line':row.lineno} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                for row in api.call('GET','/cases/1/workflow-runs'):
                    if row['status'] not in {'succeeded','failed','cancelled','interrupted'}:api.call('POST','/workflow-runs/'+row['id']+':cancel',{'expected_version':row['version']})
                for identifier in report['actual_native_jobs']:
                    job=api.call('GET','/jobs/'+str(identifier))
                    if job['status'] in {'queued','running'}:api.call('POST','/jobs/'+str(identifier)+':cancel',{})
                    stopped(identifier)
                for row in api.call('GET','/terminal/sessions?include_closed=true')['sessions']:
                    if row.get('cleanup_verified') is not True:api.call('DELETE','/terminal/sessions/'+row['id'])
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
        report['original_database_unchanged']=sha(original)==original_sha
        report['passed']=bool(report.get('functional_passed') and report.get('API_exit_verified') and report.get('preview_exit_verified') and report['original_database_unchanged'] and 'cleanup_failure' not in report)
        (api.root/'stage-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8');print('CASE_STAGE_RESULT '+str(api.root/'stage-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
