"""Inspect incumbent CasePanel on a copy of actual accepted history; no new jobs/inference."""
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

from testsupport import SERVER, IsolatedServer

ROOT=SERVER.parents[1]
APP='''import os
from app.main import app
from app.services.process_identity_native import observe
@app.get('/api/v1/test/case-panel-birth')
async def birth():return observe(os.getpid())
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def business(path):
    with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as db:
        return {name:hashlib.sha256(repr(db.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall()).encode()).hexdigest()
            for name in ['workflow_runs','workflow_steps','jobs','job_attempts','artifact_publications','artifact_publication_heads']}


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services.process_identity_native import observe
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    source=ROOT/'tmp/workflow-jobs-UI-uz2d3e0p';prior=json.loads((source/'UI-result.json').read_text(encoding='utf8'))
    assert prior['passed'] and prior['API_exit_verified'] and prior['preview_exit_verified']
    original=source/'test.sqlite';original_sha=sha(original);before=business(original)
    api=IsolatedServer('case-panel-baseline',app='case_baseline_fixture:app')
    with sqlite3.connect('file:'+original.as_posix()+'?mode=ro',uri=True) as a,sqlite3.connect(api.root/'test.sqlite') as b:a.backup(b)
    assert not any(path.is_symlink() for path in (source/'files').rglob('*'))
    shutil.copytree(source/'files',api.root/'files');(api.root/'case_baseline_fixture.py').write_text(APP,encoding='utf8')
    report={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'new_native_business_jobs':0,
        'review_sha256':sha(args.review),'original_database_sha256':original_sha,'original_business_hashes':before}
    entered=False;api_handle=preview=preview_handle=preview_log=None
    try:
        api.__enter__();entered=True;report['API_birth']=api.call('GET','/test/case-panel-birth');api_handle=ProcessHandle.query(report['API_birth'])
        with sqlite3.connect(api.root/'test.sqlite') as db:email=db.execute('SELECT email FROM users WHERE id=1').fetchone()[0]
        api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        web=ROOT/'platform/web';preview_log=(api.root/'web-preview.log').open('w',encoding='utf8')
        preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),'preview',
            '--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=web,env=dict(os.environ,VITE_API_TARGET=api.base),
            stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        report['preview_birth']=observe(preview.pid);preview_handle=ProcessHandle.query(report['preview_birth'])
        setup={'web':'http://127.0.0.1:'+str(port),'email':email,'root':str(api.root)}
        (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2),encoding='utf8')
        print('CASE_PANEL_BASELINE_READY '+str(api.root),flush=True)
        until=time.monotonic()+600
        while not (api.root/'browser-finished.json').exists():
            assert time.monotonic()<until and api.proc.poll() is None and preview.poll() is None;time.sleep(.15)
        report['browser']=json.loads((api.root/'browser-finished.json').read_text(encoding='utf8'));assert report['browser']['passed']
        assert business(api.root/'test.sqlite')==before;report['business_rows_unchanged']=True;report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1200],
            'frames':[{'file':Path(row.filename).name,'line':row.lineno} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                sessions=api.call('GET','/terminal/sessions?include_closed=true')['sessions']
                for row in sessions:
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
        (api.root/'baseline-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print('CASE_PANEL_BASELINE_RESULT '+str(api.root/'baseline-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
