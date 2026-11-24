"""Actual lost-browser-upload UI, real bytes, same confirmation after HTTP loss."""
import argparse,hashlib,json,os,shutil,socket,sqlite3,subprocess,time,traceback,uuid
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from s3testsupport import ObjectStore
from upload_recovery_acceptance import APP as BASE,RUNTIME

ROOT=SERVER.parents[1]
APP=BASE+'''
from sqlalchemy import select
@app.get('/api/v1/test/upload-owned-terminals')
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
    api=IsolatedServer('upload-recovery-S3-browser' if store else 'upload-recovery-FS-browser',app='upload_ui_fixture:app')
    (api.root/'upload_ui_fixture.py').write_bytes(APP.encode())
    if store:(api.root/'isolated-store.json').write_text(json.dumps(store.settings()),encoding='utf8')
    report={'passed':False,'backend':'s3+sqlite' if store else 'fs+sqlite','new_real_model_calls':0,'new_mock_llm_calls':0,
        'developer_database_modified':False,'developer_bucket_modified':False,'source_review_sha256':sha(args.review)}
    entered=False;handle=preview=preview_handle=preview_log=None
    print('UPLOAD_RECOVERY_BROWSER '+str(api.root),flush=True)
    def save():(api.root/'UI-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    def create(path,data):
        upload=api.call('POST',api.prefix+'/uploads',{'path':path,'size':len(data),'expected_sha256':None})
        with httpx.Client(timeout=60,trust_env=False) as client:
            r=client.put(api.base+'/api/v1'+api.prefix+'/uploads/'+upload['id']+'?offset=0',
                content=data[:7],headers={'Authorization':'Bearer '+api.token})
            assert r.status_code==200,r.text
        return upload['id']
    try:
        api.__enter__();entered=True;birth=api.call('GET','/test/upload-birth');handle=ProcessHandle.query(birth);report['API_birth']=birth
        name='Server upload recovery browser';api.create_case(name);user=api.call('GET','/auth/me')
        size=4*1024*1024+17;data=(uuid.uuid4().hex.encode()*(size//32+1))[:size]
        original=api.root/'original.bin';original.write_bytes(data);wrong=api.root/'wrong.bin';wrong.write_bytes(b'X'+data[1:])
        resume_id=create('Ref/recover.bin',data);cancel_id=create('Ref/cancel.bin',b'cancel-source-bytes-19')
        retain_id=create('Ref/retain.bin',b'retain-source-bytes-19')
        api.call('POST','/test/upload-legacy/'+str(api.case_id)+'/'+resume_id)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        web=ROOT/'platform/web';preview_log=(api.root/'web-preview.log').open('w',encoding='utf8')
        preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),'preview',
            '--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=web,env=dict(os.environ,VITE_API_TARGET=api.base),
            stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
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
            'case_id':api.case_id,'case_name':name,'resume_id':resume_id,'cancel_id':cancel_id,'retain_id':retain_id,
            'original_file':str(original),'wrong_file':str(wrong),'source_sha256':hashlib.sha256(data).hexdigest()}
        (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2)+'\n',encoding='utf8');save()
        print('UPLOAD_RECOVERY_BROWSER_READY '+web_url,flush=True)
        done=api.root/'browser-finished.json';deadline=time.monotonic()+1200
        while not done.exists():
            assert preview.poll() is None and api.proc.poll() is None
            if time.monotonic()>=deadline:raise TimeoutError('Browser acceptance did not finish')
            time.sleep(.2)
        browser=json.loads(done.read_text(encoding='utf8'));assert browser['passed'];report['browser']=browser
        assert browser['adoptionLoss']==browser['adoptionReplay'] and browser['adoptionLoss']['status']==202
        assert browser['adoptionLoss']['body']['state']=='completed' and browser['adoptionLoss']['body']['action']=='resume_upload'
        assert browser['wrong_file_rejected_before_submit'] and browser['actual_file_reselected_after_reload']
        assert api.call('GET',api.prefix+'/raw?path=Ref/recover.bin',raw=True)==b'UI_NEWER_AFTER_UPLOAD'
        assert api.call('GET',api.prefix+'/uploads/'+retain_id)['offset']==7
        api.call('GET',api.prefix+'/uploads/'+cancel_id,expect=404)
        history=api.call('GET',api.prefix+'/upload-recovery/actions')['entries']
        assert len(history)==3 and all(v['state']=='completed' for v in history)
        assert {v['action'] for v in history}=={'retain_for_review','resume_upload','cancel_upload'}
        with sqlite3.connect(api.root/'test.sqlite') as db:
            assert db.execute('SELECT COUNT(*) FROM jobs WHERE case_id=?',(api.case_id,)).fetchone()[0]==0
            rows=dict(db.execute('SELECT id,state FROM upload_storage_reservations WHERE case_id=?',(api.case_id,)))
            assert rows[resume_id]=='completed' and rows[cancel_id]=='cancelled' and rows[retain_id]=='active'
        report.update(functional_passed=True,exactly_three_audited_actions=True,newer_edit_not_overwritten=True,
            retained_upload_unchanged=True,cancelled_upload_closed=True,no_business_job_or_model_execution=True)
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1800],
            'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                for row in api.call('GET','/test/upload-owned-terminals'):
                    if row['cleanup_verified'] is not True and row['owner_available']:api.call('DELETE','/terminal/sessions/'+row['id'])
                terminals=api.call('GET','/test/upload-owned-terminals');assert all(row['cleanup_verified'] is True for row in terminals)
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
    print('UPLOAD_RECOVERY_BROWSER_RESULT '+str(api.root/'UI-result.json'),flush=True)
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
