"""Final real-browser inspection on a copy of accepted actual Job history.

No new business Job or inference is permitted; original data stays read-only.
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

import httpx
from testsupport import IsolatedServer, SERVER
from job_damaged_history_acceptance import APP

ROOT=SERVER.parents[1]


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def business(path):
    with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as db:
        return {name:hashlib.sha256(repr(db.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall()).encode()).hexdigest()
            for name in ['jobs','job_attempts','job_execution_heads','artifact_publications','artifact_publication_heads','job_input_recoveries']}


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--from-history',type=Path,required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    original=args.from_history.resolve();assert original.parent==(ROOT/'tmp').resolve() and original.name.startswith('job-damaged-history-')
    prior=json.loads((original/'damaged-result.json').read_text(encoding='utf8'))
    assert prior['passed'] and prior['API_exit_verified'] and prior['preview_exit_verified']
    source=original/'test.sqlite';source_hash=sha(source);before=business(source)
    api=IsolatedServer('job-recovery-final-readback',app='readback_fixture:app')
    assert not any(p.is_symlink() for p in (original/'files').rglob('*'))
    with sqlite3.connect('file:'+source.as_posix()+'?mode=ro',uri=True) as a,sqlite3.connect(api.root/'test.sqlite') as b:a.backup(b)
    shutil.copytree(original/'files',api.root/'files');(api.root/'readback_fixture.py').write_text(APP,encoding='utf8')
    report={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'new_native_business_jobs':0,
        'original_history':str(original),'original_database_sha256':source_hash,'business_hashes_before':before,'review_sha256':sha(args.review)}
    entered=False;handle=preview=preview_handle=preview_log=None
    try:
        api.__enter__();entered=True;birth=api.call('GET','/test/damaged-birth');handle=ProcessHandle.query(birth);report['API_birth']=birth
        with sqlite3.connect(api.root/'test.sqlite') as db:email=db.execute('SELECT email FROM users WHERE id=1').fetchone()[0]
        api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        web=ROOT/'platform/web';preview_log=(api.root/'web-preview.log').open('w',encoding='utf8')
        preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),'preview',
            '--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=web,env=dict(os.environ,VITE_API_TARGET=api.base),
            stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        birth_preview=identity.observe(preview.pid);preview_handle=ProcessHandle.query(birth_preview);report['preview_birth']=birth_preview
        web_url='http://127.0.0.1:'+str(port)
        with httpx.Client(timeout=1,trust_env=False) as client:
            until=time.monotonic()+30
            while True:
                assert preview.poll() is None and time.monotonic()<until
                try:
                    if client.get(web_url).status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(.1)
        (api.root/'browser-setup.json').write_text(json.dumps({'root':str(api.root),'web':web_url,'email':email},indent=2),encoding='utf8')
        print('JOB_FINAL_READBACK_READY '+str(api.root),flush=True)
        done=api.root/'browser-finished.json';until=time.monotonic()+900
        while not done.exists():
            assert preview.poll() is None and api.proc.poll() is None
            if time.monotonic()>until:raise TimeoutError('Final browser inspection not finished')
            time.sleep(.2)
        report['browser']=json.loads(done.read_text(encoding='utf8'));assert report['browser']['passed']
        job=api.call('GET','/jobs/9');assert job['status']=='succeeded' and job['attempt']['process_stop']['verified'] is True
        assert business(api.root/'test.sqlite')==before
        report.update(functional_passed=True,business_rows_unchanged=True,original_native_job_9_stop_readback=True)
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1600],
            'frames':[{'file':Path(row.filename).name,'line':row.lineno} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
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
        report['original_database_unchanged']=sha(source)==source_hash
        report['passed']=bool(report.get('functional_passed') and report.get('API_exit_verified') and report.get('preview_exit_verified')
            and report['original_database_unchanged'] and 'cleanup_failure' not in report)
        (api.root/'readback-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print('JOB_FINAL_READBACK_RESULT '+str(api.root/'readback-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
