"""Actual browser confirmation/replay of independent input recovery requests."""
import argparse,hashlib,json,os,shutil,socket,sqlite3,subprocess,sys,time,traceback
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from job_input_recovery_acceptance import APP as BASE

ROOT=SERVER.parents[1]
APP=BASE+'''
@app.get('/api/v1/test/input-owned-terminals')
async def owned_terminals(user=Depends(get_current_user)):
    from app.models import NativeSession
    from app.routers.terminal import _sessions
    from app.services.terminal_records import public
    current=observe(os.getpid())
    async with SessionLocal() as db:
        return [{**public(row),'owner_available':row.id in _sessions} for row in await db.scalars(select(NativeSession).where(NativeSession.created_by==user.id))
            if json.loads(row.owner_identity_json)==current]
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read_rows(path,table):
    with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        return {str(row['id']):dict(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY id')}


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True);args=parser.parse_args()
    review=json.loads(args.review.read_text(encoding='utf8'));assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for path,digest in review['source_hashes'].items():assert sha(ROOT/path)==digest
    source=ROOT/'tmp/job-input-recovery-tpa1zazi';assert json.loads((source/'input-recovery-result.json').read_text(encoding='utf8'))['passed']
    source_sha=sha(source/'test.sqlite')
    api=IsolatedServer('input-recovery-browser',app='input_ui_fixture:app');(api.root/'input_ui_fixture.py').write_text(APP,encoding='utf8')
    with sqlite3.connect('file:'+(source/'test.sqlite').as_posix()+'?mode=ro',uri=True) as old:
        with sqlite3.connect(api.root/'test.sqlite') as new:old.backup(new)
    shutil.copytree(source/'files',api.root/'files')
    tables=['native_sessions','native_cli_sessions','native_viz_sessions','native_session_links','job_input_recoveries']
    before={table:read_rows(api.root/'test.sqlite',table) for table in tables}
    report={'passed':False,'new_real_model_calls':0,'new_mock_llm_calls':0,'developer_database_modified':False,'source_review_sha256':sha(args.review)}
    entered=False;handle=preview=preview_handle=preview_log=None;new_jobs=[]
    print('INPUT_RECOVERY_BROWSER '+str(api.root),flush=True)
    def save():(api.root/'UI-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    def setup_case(name,kind):
        workspace=api.call('GET','/workspaces')[0]
        case=api.call('POST','/workspaces/'+str(workspace['id'])+'/cases',{'name':name})
        api.case_id=case['id'];api.prefix='/cases/'+str(case['id'])
        api.put('Ref/input.txt','ORIGINAL_UI_INPUT')
        if kind=='convert':api.put('E-report/report.md','# Actual browser input recovery\n\nREGISTERED_UI_CURRENT_INPUT\n')
        row=api.call('POST','/test/unsealed/'+str(case['id']),{'command':'echo OLD_UI_REQUEST_MUST_NOT_EXECUTE','job_type':kind})
        assert api.call('POST','/test/refuse-unsealed/'+str(row['id']))=={'result':'input_recovery_required'}
        return {**row,'case_id':case['id'],'case_name':name}
    try:
        api.__enter__();entered=True;birth=api.call('GET','/test/input-birth');handle=ProcessHandle.query(birth);report['API_birth']=birth
        api.create_case('Browser recovery account seed');user=api.call('GET','/auth/me')
        ordinary=setup_case('Input recovery browser acceptance','shell')
        professional=setup_case('Registered input browser acceptance','convert')
        originals={str(row['id']):{'job':read_rows(api.root/'test.sqlite','jobs')[str(row['id'])],
            'attempt':read_rows(api.root/'test.sqlite','job_attempts')[row['attempt_id']]} for row in [ordinary,professional]}
        code="from pathlib import Path; print(Path('Ref/input.txt').read_text()); Path('ui-input-proof.txt').write_text(Path('Ref/input.txt').read_text()); Path('ui-input-executions.txt').open('a').write('once\\n')"
        command='"'+sys.executable+'" -I -S -c "'+code+'"'
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        web=ROOT/'platform/web';preview_log=(api.root/'web-preview.log').open('w',encoding='utf8')
        preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),'preview','--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=web,
            env=dict(os.environ,VITE_API_TARGET=api.base),stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        preview_birth=identity.observe(preview.pid);preview_handle=ProcessHandle.query(preview_birth);report['preview_birth']=preview_birth
        web_url='http://127.0.0.1:'+str(port);until=time.monotonic()+30
        with httpx.Client(timeout=1,trust_env=False) as client:
            while True:
                assert preview.poll() is None
                try:
                    if client.get(web_url).status_code==200:break
                except httpx.HTTPError:pass
                assert time.monotonic()<until;time.sleep(.1)
        setup={'root':str(api.root),'web':web_url,'email':user['email'],'ordinary':ordinary,'professional':professional,'command':command}
        (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2)+'\n',encoding='utf8');save()
        print('INPUT_RECOVERY_BROWSER_READY '+web_url,flush=True)
        done=api.root/'browser-finished.json';until=time.monotonic()+1200
        while not done.exists():
            assert preview.poll() is None and api.proc.poll() is None
            if time.monotonic()>=until:raise TimeoutError('Browser acceptance did not finish')
            time.sleep(.2)
        browser=json.loads(done.read_text(encoding='utf8'));assert browser['passed'];report['browser']=browser
        for row in [ordinary,professional]:
            plan=api.call('GET','/jobs/'+str(row['id'])+'/input-recovery')
            relation=next(link for link in plan['links'] if link['source_job_id']==row['id']);target=relation['target_job_id'];new_jobs.append(target)
            result=api.wait_job(target,timeout=180);assert result['attempt']['process_stop']['verified'] is True
            old=api.call('GET','/jobs/'+str(row['id']));assert old['status']=='cancelled' and old['attempt']['process_identity'] is None
            old_attempt=read_rows(api.root/'test.sqlite','job_attempts')[row['attempt_id']]
            assert old_attempt['input_manifest_json']==originals[str(row['id'])]['attempt']['input_manifest_json'] and old_attempt['claim_token'] is None
            path=result['attempt']['workspace']['run_path']
            if row is ordinary:
                assert api.call('GET','/cases/'+str(row['case_id'])+'/raw?path='+path+'/ui-input-proof.txt',raw=True)==b'CURRENT_UI_INPUT_V2'
                executions=api.call('GET','/cases/'+str(row['case_id'])+'/raw?path='+path+'/ui-input-executions.txt',raw=True)
                assert executions.replace(b'\r\n',b'\n')==b'once\n'
            else:
                assert result['attempt']['handler']['key']=='report.build'
                assert b'REGISTERED_UI_CURRENT_INPUT' in api.call('GET','/cases/'+str(row['case_id'])+'/raw?path='+path+'/E-report/build/report.html',raw=True)
                native=api.call('GET','/test/input-native-requests/'+str(row['case_id']));assert native and all(v['cleanup_verified'] is True for v in native)
            report.setdefault('actual_replacements',[]).append({'receipt':relation,'job':result})
        after={table:read_rows(api.root/'test.sqlite',table) for table in tables}
        assert all(all(after[table][key]==value for key,value in rows.items()) for table,rows in before.items())
        assert len(after['job_input_recoveries'])==len(before['job_input_recoveries'])+2 and sha(source/'test.sqlite')==source_sha
        report.update(functional_passed=True,source_database_unchanged=True,all_previous_native_rows_and_links_preserved=True,two_new_replacements_only=True)
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                for row in api.call('GET','/test/input-owned-terminals'):
                    if row['cleanup_verified'] is not True and row['owner_available']:api.call('DELETE','/terminal/sessions/'+row['id'])
                terminals=api.call('GET','/test/input-owned-terminals');assert all(row['cleanup_verified'] is True for row in terminals)
                report['actual_owned_terminal_stops']=terminals
                for identifier in new_jobs:
                    final=api.call('GET','/jobs/'+str(identifier));assert final['attempt']['process_stop']['verified'] is True
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
    print('INPUT_RECOVERY_BROWSER_RESULT '+str(api.root/'UI-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
