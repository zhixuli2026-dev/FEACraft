"""Actual damaged-upload navigation, isolation reply loss/reload and salvage."""
import argparse,hashlib,json,os,shutil,socket,sqlite3,subprocess,time,traceback
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from s3testsupport import ObjectStore
from damaged_upload_recovery_acceptance import APP as BASE,RUNTIME

ROOT=SERVER.parents[1]
APP=BASE+'''
from sqlalchemy import select
@app.get('/api/v1/test/raw-upload-owned-terminals')
async def owned_terminals(user=Depends(get_current_user)):
    from app.models import NativeSession
    from app.services.terminal_records import public
    from app.routers.terminal import _sessions
    current=observe(os.getpid())
    async with SessionLocal() as db:
        return [{**public(row),'owner_available':row.id in _sessions} for row in await db.scalars(select(NativeSession).where(NativeSession.created_by==user.id))
            if json.loads(row.owner_identity_json)==current]
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(args,store=None):
    from app.services.windows_process_scope import ProcessHandle
    from app.services.process_identity_native import observe
    api=IsolatedServer('damaged-upload-S3-browser' if store else 'damaged-upload-FS-browser',app='raw_upload_ui_fixture:app')
    (api.root/'raw_upload_ui_fixture.py').write_bytes(APP.encode())
    if store:(api.root/'isolated-store.json').write_text(json.dumps(store.settings()),encoding='utf8')
    report={'passed':False,'backend':'s3+sqlite' if store else 'fs+sqlite','new_real_model_calls':0,'new_mock_llm_calls':0,
        'developer_database_modified':False,'developer_bucket_modified':False,'source_review_sha256':sha(args.review)}
    entered=False;handle=preview=preview_handle=preview_log=None
    print('DAMAGED_UPLOAD_BROWSER '+str(api.root),flush=True)
    def save():(api.root/'UI-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    try:
        api.__enter__();entered=True;birth=api.call('GET','/test/upload-birth');handle=ProcessHandle.query(birth);report['API_birth']=birth
        name='Damaged upload manual review browser';api.create_case(name);user=api.call('GET','/auth/me')
        api.put('Ref/current.txt','CURRENT_UNTOUCHED')
        identifier=api.call('POST',api.prefix+'/uploads',{'path':'Ref/original.bin','size':19,'expected_sha256':None})['id']
        with httpx.Client(timeout=30,trust_env=False) as client:
            response=client.put(api.base+'/api/v1'+api.prefix+'/uploads/'+identifier+'?offset=0',content=b'PARTIAL',headers={'Authorization':'Bearer '+api.token})
            assert response.status_code==200,response.text
        seeded=api.call('POST','/test/upload-damage/'+str(api.case_id)+'/'+identifier,{'kind':'broken'})
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        web=ROOT/'platform/web';preview_log=(api.root/'web-preview.log').open('w',encoding='utf8')
        preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),'preview','--host','127.0.0.1','--port',str(port),'--strictPort'],
            cwd=web,env=dict(os.environ,VITE_API_TARGET=api.base),stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        preview_birth=observe(preview.pid);preview_handle=ProcessHandle.query(preview_birth);report['preview_birth']=preview_birth
        web_url='http://127.0.0.1:'+str(port);deadline=time.monotonic()+30
        with httpx.Client(timeout=1,trust_env=False) as client:
            while True:
                assert preview.poll() is None
                try:
                    if client.get(web_url).status_code==200:break
                except httpx.HTTPError:pass
                assert time.monotonic()<deadline;time.sleep(.1)
        setup={'root':str(api.root),'web':web_url,'API':api.base,'email':user['email'],'user_id':user['id'],
            'case_id':api.case_id,'case_name':name,'upload_id':identifier,'source':seeded['source'],'file_path':seeded['file_path']}
        (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2)+'\n',encoding='utf8');save()
        print('DAMAGED_UPLOAD_BROWSER_READY '+web_url,flush=True)
        done=api.root/'browser-finished.json';deadline=time.monotonic()+1200
        while not done.exists():
            assert preview.poll() is None and api.proc.poll() is None
            if time.monotonic()>=deadline:raise TimeoutError('Browser acceptance did not finish')
            time.sleep(.2)
        browser=json.loads(done.read_text(encoding='utf8'));assert browser['passed'];report['browser']=browser
        assert browser['isolationLoss']==browser['isolationReplay'] and browser['isolationLoss']['status']==202
        isolation=browser['isolationLoss']['body'];assert isolation['state']=='completed' and isolation['action']=='isolate_version'
        assert api.call('GET',api.prefix+'/raw?path=Ref/current.txt',raw=True)==b'CURRENT_UNTOUCHED'
        assert api.call('GET',api.prefix+'/raw?path=Ref/salvaged.bin',raw=True)==b'PARTIAL'
        assert api.call('GET',api.prefix+'/file-recovery/actions/'+isolation['id']+'/file?path='+seeded['file_path'],raw=True)==b'PARTIAL'
        assert api.call('GET',api.prefix+'/file-recovery/actions/'+isolation['id']+'/file?path=meta.json',raw=True)==b'{"partial":'
        api.call('GET',api.prefix+'/uploads/'+identifier,expect=409)
        assert not any(v['id']==identifier for v in api.call('GET',api.prefix+'/upload-recovery')['entries'])
        history=api.call('GET',api.prefix+'/file-recovery/actions')
        assert len(history)==3 and all(v['state']=='completed' and not v['stop_evidence_created'] and not v['old_sql_commit_rewritten'] for v in history)
        assert {v['action'] for v in history}=={'retain_for_review','isolate_version','restore_file'}
        with sqlite3.connect(api.root/'test.sqlite') as db:
            assert db.execute('SELECT COUNT(*) FROM jobs WHERE case_id=?',(api.case_id,)).fetchone()[0]==0
            assert db.execute('SELECT state,remaining_bytes FROM upload_storage_reservations WHERE id=?',(identifier,)).fetchone()==('cancelled',0)
            assert db.execute("SELECT COUNT(*) FROM durable_events WHERE case_id=? AND type='files.upload_isolated'",(api.case_id,)).fetchone()[0]==1
        report.update(functional_passed=True,raw_navigation_and_explicit_new_edit=True,exactly_one_original_upload_closed=True,
            retained_partial_and_broken_metadata_unchanged=True,current_target_unchanged=True,no_business_job_or_model_execution=True)
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1800],
            'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                for row in api.call('GET','/test/raw-upload-owned-terminals'):
                    if row['cleanup_verified'] is not True and row['owner_available']:api.call('DELETE','/terminal/sessions/'+row['id'])
                terminals=api.call('GET','/test/raw-upload-owned-terminals');assert all(v['cleanup_verified'] is True for v in terminals)
                report['actual_owned_terminal_stops']=terminals
            except BaseException as error:report.setdefault('cleanup_failures',[]).append(type(error).__name__)
            try:
                try:api.call('POST','/test/upload-exit')
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
    print('DAMAGED_UPLOAD_BROWSER_RESULT '+str(api.root/'UI-result.json'),flush=True)
    return report['passed'],api.root


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True);parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    review=json.loads(args.review.read_text(encoding='utf8'));assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    if not args.s3:
        passed,_=verify(args);return 0 if passed else 1
    assert sha(RUNTIME)==review['native_store_binary_sha256']
    with ObjectStore(executable=RUNTIME) as store:passed,root=verify(args,store)
    probe="""import json,pathlib,sys
r=pathlib.Path(sys.argv[1]);o=json.loads((r/'owner.json').read_text());p=pathlib.Path('/proc')/str(o['pid'])
same=p.exists() and (p/'stat').read_text().rsplit(')',1)[1].split()[19]==o['start'] and p.stat().st_uid==o['uid']
assert not same
"""
    stopped=subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c',probe,store.linux],capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
    result={'passed':passed and stopped.returncode==0,'actual_native_store_exited':stopped.returncode==0,'store_root':str(store.root)}
    (root/'store-exit-verified.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
