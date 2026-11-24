"""Actual legacy native failure and registered reprepare through the compiled UI.

No model calls or invented inference. Browser response loss follows an actual
server commit; cleanup requires retained API/preview creation handles and SQL
stop proofs for every terminal created by this API, including page reloads.
"""
import argparse,hashlib,json,os,shutil,socket,sqlite3,subprocess,sys,time,traceback,uuid
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from job_input_recovery_ui_acceptance import APP as BASE,read_rows

ROOT=SERVER.parents[1]
APP=BASE+'''
@app.post('/api/v1/test/actual-legacy/{case_id}')
async def actual_legacy(case_id:int,user=Depends(get_current_user)):
    import sys,uuid
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True)
        async with committing(db,case,content_write=False):
            spec={'command':'Historical fixed argv','case_prefix':case.s3_prefix,'runtime':'windows',
                'argv':[sys.executable,'Ref/legacy.py'],'evidence_run_dir':None,'timeout_seconds':120,'max_log_bytes':16777216}
            job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=uuid.uuid4().hex,
                digest=job_execution.request_hash({'actual_legacy':uuid.uuid4().hex}),job_type='convert',
                command=spec['command'],spec=spec,input_paths=['Ref/legacy.py','E-report/report.md'])
            await job_delivery.request(db,job,user_id=user.id,mode='local');identifier=job.id
        await job_delivery.deliver(identifier)
        from app.services import jobrunner
        process=jobrunner.get_proc(identifier)
        assert process is not None
        await process.wait()
        return {'id':identifier}
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--resume-original',action='store_true',help='Copy the actual first failed run; never execute its original again')
    args=parser.parse_args()
    review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    source=ROOT/('tmp/legacy-reprepare-browser-9vd4snhp' if args.resume_original else 'tmp/input-UI-readback-p1sletpj')
    if args.resume_original:
        prior=json.loads((source/'UI-result.json').read_text(encoding='utf8'))
        assert not prior['passed'] and prior['actual_API_exited'] and not prior.get('cleanup_failures')
        assert not list((source/'files').glob('workspaces/*/cases/*/.vibeflow/file-transactions/pending')),'Pending storage-bound journals cannot be copied to a different filesystem root'
    else:assert json.loads((source/'readback-result.json').read_text(encoding='utf8'))['passed']
    source_hash=sha(source/'test.sqlite')
    api=IsolatedServer('legacy-reprepare-browser',app='legacy_ui_fixture:app')
    (api.root/'legacy_ui_fixture.py').write_text(APP,encoding='utf8')
    with sqlite3.connect('file:'+(source/'test.sqlite').as_posix()+'?mode=ro',uri=True) as old:
        with sqlite3.connect(api.root/'test.sqlite') as new:old.backup(new)
    shutil.copytree(source/'files',api.root/'files')
    tables=['native_sessions','native_cli_sessions','native_viz_sessions','native_session_links','job_input_recoveries']
    before={table:read_rows(api.root/'test.sqlite',table) for table in tables}
    report={'passed':False,'new_real_model_calls':0,'new_mock_llm_calls':0,'developer_database_modified':False,
        'source_review_sha256':sha(args.review)}
    entered=False;handle=preview=preview_handle=preview_log=None;case_id=original_id=None
    print('LEGACY_REPREPARE_BROWSER '+str(api.root),flush=True)
    def save():(api.root/'UI-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    try:
        api.__enter__();entered=True;birth=api.call('GET','/test/input-birth');handle=ProcessHandle.query(birth);report['API_birth']=birth
        if args.resume_original:
            with sqlite3.connect('file:'+(api.root/'test.sqlite').as_posix()+'?mode=ro',uri=True) as db:
                original_id,case_id,owner,email=db.execute("SELECT j.id,j.case_id,w.user_id,u.email FROM jobs j JOIN cases c ON c.id=j.case_id JOIN workspaces w ON w.id=c.workspace_id JOIN users u ON u.id=w.user_id WHERE j.command='Historical fixed argv' ORDER BY j.id DESC LIMIT 1").fetchone()
            api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
            api.case_id=case_id;api.prefix='/cases/'+str(case_id);executions=source/'actual-legacy-executions.txt'
            report['original_reused_from_actual_failed_run']=str(source)
        else:
            api.create_case('Legacy registered reprepare browser');case_id=api.case_id
            executions=api.root/'actual-legacy-executions.txt'
            code='from pathlib import Path\nwith Path('+repr(str(executions))+').open("a") as f:f.write("once\\n")\nraise SystemExit(23)\n'
            api.put('Ref/legacy.py',code);api.put('E-report/report.md','# Old sealed report input\n\nOLD_SEALED_REPORT\n')
            original_id=api.call('POST','/test/actual-legacy/'+str(case_id))['id']
        user=api.call('GET','/auth/me')
        original=api.wait_job(original_id,expected='failed')
        assert original['exit_code']==23 and original['attempt']['process_stop']['verified'] is True
        assert original['attempt'].get('handler') is None and executions.read_text(encoding='utf8')=='once\n'
        original_rows={'job':read_rows(api.root/'test.sqlite','jobs')[str(original_id)],
            'attempt':read_rows(api.root/'test.sqlite','job_attempts')[original['attempt']['id']]}
        nonce='ACTUAL_REPREPARE_UI_'+uuid.uuid4().hex
        api.put('E-report/report.md','# Current independent report input\n\n'+nonce+'\n')
        options=api.call('GET','/jobs/'+str(original_id)+'/operations')
        assert options['reprepare']['available'] and {'key':'report.build','version':'1'} in options['reprepare']['handler_choices']
        assert not any(options[action]['available'] for action in ['retry','rerun','resume'])
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        web=ROOT/'platform/web';preview_log=(api.root/'web-preview.log').open('w',encoding='utf8')
        preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),'preview','--host','127.0.0.1','--port',str(port),'--strictPort'],
            cwd=web,env=dict(os.environ,VITE_API_TARGET=api.base),stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        preview_birth=identity.observe(preview.pid);preview_handle=ProcessHandle.query(preview_birth);report['preview_birth']=preview_birth
        web_url='http://127.0.0.1:'+str(port);deadline=time.monotonic()+30
        with httpx.Client(timeout=1,trust_env=False) as client:
            while True:
                assert preview.poll() is None
                try:
                    if client.get(web_url).status_code==200:break
                except httpx.HTTPError:pass
                assert time.monotonic()<deadline;time.sleep(.1)
        setup={'root':str(api.root),'web':web_url,'email':user['email'],'user_id':user['id'],'case_id':case_id,
            'case_name':'Legacy registered reprepare browser','original_id':original_id,'nonce':nonce,
            'parameters':{'run_dir':'C-run/case-1','mode':'markdown','allow_standalone':True}}
        (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2)+'\n',encoding='utf8');save()
        print('LEGACY_REPREPARE_BROWSER_READY '+web_url,flush=True)
        done=api.root/'browser-finished.json';deadline=time.monotonic()+1200
        while not done.exists():
            assert preview.poll() is None and api.proc.poll() is None
            if time.monotonic()>=deadline:raise TimeoutError('Browser acceptance did not finish')
            time.sleep(.2)
        browser=json.loads(done.read_text(encoding='utf8'));assert browser['passed'];report['browser']=browser
        response=browser['responseLoss'];replay=browser['reloadReplay'];target=response['body']['id']
        assert response['status']==202 and replay['status']==202 and replay['body']['id']==target and replay['body']['created'] is False
        assert response['key']==replay['key'] and response['requestBody']==replay['requestBody']
        assert response['requestBody']['reprepare_handler']=={'key':'report.build','version':'1'}
        final=api.wait_job(target,timeout=180)
        assert final['attempt']['process_stop']['verified'] is True and final['attempt']['handler']['key']=='report.build'
        run_path=final['attempt']['workspace']['run_path']
        assert nonce.encode() in api.call('GET',api.prefix+'/raw?path='+run_path+'/E-report/build/report.html',raw=True)
        assert executions.read_text(encoding='utf8')=='once\n'
        assert read_rows(api.root/'test.sqlite','jobs')[str(original_id)]==original_rows['job']
        assert read_rows(api.root/'test.sqlite','job_attempts')[original['attempt']['id']]==original_rows['attempt']
        jobs=[row['id'] for row in read_rows(api.root/'test.sqlite','jobs').values() if row['case_id']==case_id]
        assert sorted(jobs)==sorted([original_id,target])
        requests=api.call('GET','/test/input-native-requests/'+str(case_id))
        assert requests and all(row['cleanup_verified'] is True for row in requests)
        after={table:read_rows(api.root/'test.sqlite',table) for table in tables}
        assert all(all(after[table][key]==value for key,value in rows.items()) for table,rows in before.items())
        assert sha(source/'test.sqlite')==source_hash
        report.update(functional_passed=True,original_job=original,actual_new_job=final,original_native_executed_once_only=True,
            original_job_and_attempt_byte_fields_unchanged=True,actual_case_job_ids=jobs,actual_HTML_contains_current_nonce=True,
            actual_native_preparation_stops=requests,prior_native_and_input_audits_unchanged=True,source_database_unchanged=True)
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1600],
            'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                for row in api.call('GET','/test/input-owned-terminals'):
                    if row['cleanup_verified'] is not True and row['owner_available']:api.call('DELETE','/terminal/sessions/'+row['id'])
                terminals=api.call('GET','/test/input-owned-terminals');assert all(row['cleanup_verified'] is True for row in terminals)
                report['actual_owned_terminal_stops']=terminals
                if case_id is not None:
                    ids=[row['id'] for row in read_rows(api.root/'test.sqlite','jobs').values() if row['case_id']==case_id]
                    for identifier in ids:
                        state=api.call('GET','/jobs/'+str(identifier))
                        if state['status'] in {'queued','running'}:api.call('POST','/jobs/'+str(identifier)+':cancel')
                        deadline=time.monotonic()+30
                        while True:
                            state=api.call('GET','/jobs/'+str(identifier))
                            if state['attempt']['process_stop'].get('verified') is True:break
                            assert time.monotonic()<deadline;time.sleep(.1)
            except BaseException as error:report.setdefault('cleanup_failures',[]).append(type(error).__name__)
            try:
                try:api.call('POST','/test/input-exit')
                except (OSError,httpx.TransportError):pass
                handle.wait_exit(10);report['actual_API_exited']=True
            except BaseException as error:report.setdefault('cleanup_failures',[]).append(type(error).__name__)
            api.__exit__(RuntimeError if 'failure' in report else None,None,None)
        if handle is not None:handle.close()
        if preview is not None:
            if preview.poll() is None:preview.terminate()
            preview.wait(timeout=10)
        if preview_handle is not None:preview_handle.wait_exit(10);report['actual_preview_exited']=True;preview_handle.close()
        if preview_log is not None:preview_log.close()
        report['passed']=bool(report.get('functional_passed') and report.get('actual_API_exited') and report.get('actual_preview_exited') and not report.get('cleanup_failures'));save()
    print('LEGACY_REPREPARE_BROWSER_RESULT '+str(api.root/'UI-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
