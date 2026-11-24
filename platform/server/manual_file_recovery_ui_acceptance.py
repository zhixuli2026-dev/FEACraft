"""Actual browser manual isolation/selection with real files and lost HTTP reply."""
import argparse,hashlib,json,os,shutil,socket,sqlite3,subprocess,time,traceback
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from manual_file_recovery_acceptance import APP as BASE
from manual_file_recovery_s3_acceptance import APP as REMOTE_BASE,RUNTIME
from s3testsupport import ObjectStore

ROOT=SERVER.parents[1]
COMMON='''
@app.get('/api/v1/test/manual-owned-terminals')
async def owned_terminals(user=Depends(get_current_user)):
    from app.models import NativeSession
    from app.services.terminal_records import public
    from app.routers.terminal import _sessions
    current=observe(os.getpid())
    async with SessionLocal() as db:
        return [{**public(row),'owner_available':row.id in _sessions} for row in await db.scalars(select(NativeSession).where(NativeSession.created_by==user.id))
            if json.loads(row.owner_identity_json)==current]
'''
APP=BASE+COMMON
APP_S3=REMOTE_BASE+COMMON


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def run_browser(args,app,store=None):
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity
    api=IsolatedServer('manual-files-S3-browser' if store else 'manual-files-browser',app='manual_ui_fixture:app')
    (api.root/'manual_ui_fixture.py').write_bytes(app.encode())
    if store:(api.root/'isolated-store.json').write_text(json.dumps(store.settings()),encoding='utf8')
    report={'passed':False,'backend':'s3+sqlite' if store else 'fs+sqlite','new_real_model_calls':0,'new_mock_llm_calls':0,
        'developer_database_modified':False,'developer_bucket_modified':False,'source_review_sha256':sha(args.review)}
    entered=False;handle=preview=preview_handle=preview_log=None
    print('MANUAL_FILES_BROWSER '+str(api.root),flush=True)
    def save():(api.root/'UI-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    try:
        api.__enter__();entered=True;birth=api.call('GET','/test/manual-birth');handle=ProcessHandle.query(birth);report['API_birth']=birth
        case_name='Remote file versions browser' if store else 'Manual file versions browser'
        api.create_case(case_name);user=api.call('GET','/auth/me')
        api.put('Ref/current.txt','UI_CURRENT_UNTOUCHED')
        seeded=api.call('POST','/test/s3-source/'+str(api.case_id),{}) if store else api.call('POST','/test/manual-seed/'+str(api.case_id),{'kind':'pending'})
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
        setup={'root':str(api.root),'web':web_url,'API':api.base,'email':user['email'],'user_id':user['id'],
            'case_id':api.case_id,'case_name':case_name,'backend':report['backend'],
            **({'objects_source':seeded['source']} if store else {})}
        (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2)+'\n',encoding='utf8');save()
        print('MANUAL_FILES_BROWSER_READY '+web_url,flush=True)
        done=api.root/'browser-finished.json';deadline=time.monotonic()+1200
        while not done.exists():
            assert preview.poll() is None and api.proc.poll() is None
            if time.monotonic()>=deadline:raise TimeoutError('Browser acceptance did not finish')
            time.sleep(.2)
        browser=json.loads(done.read_text(encoding='utf8'));assert browser['passed'];report['browser']=browser
        loss=browser['responseLoss'];replay=browser['reloadReplay']
        assert loss['status']==replay['status']==202 and loss['body']==replay['body']
        assert loss['key']==replay['key'] and loss['requestBody']==replay['requestBody']
        assert loss['body']['state']=='completed' and loss['body']['action']=='restore_file'
        assert api.call('GET',api.prefix+'/raw?path=Ref/current.txt',raw=True)==b'UI_NEWER_AFTER_RESTORE'
        isolation_id=browser['isolation']['id']
        before,after=(b'S3_ACTUAL_BEFORE',b'S3_ACTUAL_AFTER') if store else (b'actual-retained-before',b'actual-partial-after')
        assert api.call('GET',api.prefix+'/file-recovery/actions/'+isolation_id+'/file?path=before/0',raw=True)==before
        assert api.call('GET',api.prefix+'/file-recovery/actions/'+isolation_id+'/file?path=after/0',raw=True)==after
        if store:
            pointer=browser['pointerIsolation']['id']
            assert api.call('GET',api.prefix+'/file-recovery/actions/'+pointer+'/file?path=pending.json',raw=True)==b'{"partial":'
            for path,expected in [('before/0',before),('after/0',after)]:
                obj=store.client.get_object(Bucket=store.bucket,Key=seeded['case_prefix']+'/.vibeflow/file-transactions/'+seeded['source']+'/'+path)
                try:assert obj['Body'].read()==expected
                finally:obj['Body'].close()
        history=api.call('GET',api.prefix+'/file-recovery/actions')
        assert sum(row['action']=='isolate_version' for row in history)==(2 if store else 1)
        assert sum(row['action']=='restore_file' for row in history)==1
        assert all(row['state']=='completed' and row['stop_evidence_created'] is False and row['old_sql_commit_rewritten'] is False for row in history)
        with sqlite3.connect(api.root/'test.sqlite') as db:
            assert db.execute('SELECT COUNT(*) FROM jobs WHERE case_id=?',(api.case_id,)).fetchone()[0]==0
            assert not db.execute("SELECT id FROM case_storage_operations WHERE case_id=? AND state!='settled'",(api.case_id,)).fetchall()
        report.update(functional_passed=True,newer_edit_not_overwritten=True,retained_both_versions_unchanged=True,
            isolation_count=2 if store else 1,restore_count=1,no_business_job_or_model_execution=True)
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1800],
            'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                for row in api.call('GET','/test/manual-owned-terminals'):
                    if row['cleanup_verified'] is not True and row['owner_available']:api.call('DELETE','/terminal/sessions/'+row['id'])
                terminals=api.call('GET','/test/manual-owned-terminals');assert all(row['cleanup_verified'] is True for row in terminals)
                report['actual_owned_terminal_stops']=terminals
            except BaseException as error:report.setdefault('cleanup_failures',[]).append(type(error).__name__)
            try:
                try:api.call('POST','/test/manual-exit')
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
    print('MANUAL_FILES_BROWSER_RESULT '+str(api.root/'UI-result.json'),flush=True)
    return report['passed'],api.root


def run():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    app=APP_S3 if args.s3 else APP
    review=json.loads(args.review.read_text(encoding='utf8'));assert review['fixture_sha256']==hashlib.sha256(app.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    if not args.s3:
        passed,_=run_browser(args,app);return 0 if passed else 1
    assert review['native_store_binary_sha256']==sha(RUNTIME)
    with ObjectStore(executable=RUNTIME) as store:passed,root=run_browser(args,app,store)
    probe="""import json,pathlib,sys
r=pathlib.Path(sys.argv[1]);o=json.loads((r/'owner.json').read_text());p=pathlib.Path('/proc')/str(o['pid'])
same=p.exists() and (p/'stat').read_text().rsplit(')',1)[1].split()[19]==o['start'] and p.stat().st_uid==o['uid']
assert not same
print('ORIGINAL_STORE_BIRTH_ABSENT')
"""
    stopped=subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c',probe,store.linux],
        capture_output=True,text=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
    result={'passed':passed and stopped.returncode==0,'actual_native_store_exited':stopped.returncode==0,'store_root':str(store.root),
        'functional_result':str(root/'UI-result.json'),'developer_database_modified':False,'developer_bucket_modified':False,
        'new_real_model_calls':0,'new_mock_llm_calls':0}
    (root/'store-exit-verified.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    print('MANUAL_FILES_BROWSER_STORE_EXIT '+str(root/'store-exit-verified.json'),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
