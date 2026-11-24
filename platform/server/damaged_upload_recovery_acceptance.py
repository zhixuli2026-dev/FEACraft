"""Real damaged upload bytes, exact isolation and actual API exit; no inference."""
import argparse,hashlib,json,shutil,subprocess,traceback,uuid
from pathlib import Path
import httpx
from botocore.exceptions import ClientError
from testsupport import IsolatedServer,SERVER
from s3testsupport import ObjectStore
from upload_recovery_acceptance import APP as BASE,RUNTIME

ROOT=SERVER.parents[1]
APP=BASE+'''
import hashlib
from app import storage
from app.services import manual_file_recovery as manual,manual_upload_recovery as raw,uploads
from app.services import owner_storage_budget
from app.services.case_write import coordinate
@app.post('/api/v1/test/upload-damage/{case_id}/{identifier}')
async def damage(case_id:int,identifier:str,body:dict,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True)
        async with owner_storage_budget.hold_case(db,case):
            async with coordinate(db,case,content_write=False):
                root=storage._fs_path(case.s3_prefix,'.')
                location=raw.path('uploads/'+identifier)
                if body['kind']=='root_link':
                    target=Path.cwd()/'outside-upload-root';target.mkdir(exist_ok=True)
                    (target/'sentinel').write_bytes(b'OUTSIDE_SENTINEL')
                    directory=uploads._session_dir(case.s3_prefix,identifier)
                    directory.parent.mkdir(parents=True,exist_ok=True)
                    os.symlink(target,directory,target_is_directory=True)
                elif storage._use_s3():
                    from app.services.s3_file_transactions import _read,_conditional
                    chosen=location+('/chunks/0000000000/data' if body['kind']=='change' else '/meta.json')
                    before,etag,_=_read(case.s3_prefix,chosen,65536)
                    data=None if body['kind']=='missing' else b'CHANGED' if body['kind']=='change' else b'{"partial":'
                    _conditional(case.s3_prefix,chosen,data,etag,{} if data is not None else None)
                else:
                    directory=uploads._session_dir(case.s3_prefix,identifier)
                    if body['kind']=='missing':(directory/'meta.json').unlink()
                    elif body['kind']=='change':(directory/'data.part').write_bytes(b'CHANGED')
                    else:
                        (directory/'meta.json').write_bytes(b'{"partial":')
                        if body.get('hardlink'):
                            target=Path.cwd()/'outside-upload-link';target.write_bytes(b'OUTSIDE_SENTINEL')
                            os.link(target,directory/'link-probe')
            await db.commit()
        return {'source':'uploads/'+identifier,'case_prefix':case.s3_prefix,'file_path':'chunks/0000000000/data' if storage._use_s3() else 'data.part'}
@app.post('/api/v1/test/raw-isolate-arm')
async def arm_raw(user=Depends(get_current_user)):
    (Path.cwd()/'raw-isolate-exit').touch();return {'armed':True}
actual_local=manual.isolate_local
actual_remote=raw.isolate_remote
def stopped(fn,*args):
    value=fn(*args);armed=Path.cwd()/'raw-isolate-exit'
    if armed.exists():armed.unlink();os._exit(0)
    return value
manual.isolate_local=lambda *args:stopped(actual_local,*args)
raw.isolate_remote=lambda *args:stopped(actual_remote,*args)
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(args,store=None):
    from app.services.windows_process_scope import ProcessHandle
    api=IsolatedServer('damaged-upload-S3' if store else 'damaged-upload-FS',app='damaged_upload_fixture:app')
    (api.root/'damaged_upload_fixture.py').write_bytes(APP.encode())
    if store:(api.root/'isolated-store.json').write_text(json.dumps(store.settings()),encoding='utf8')
    report={'passed':False,'backend':'s3+sqlite' if store else 'fs+sqlite','new_real_model_calls':0,'new_mock_llm_calls':0,
        'developer_database_modified':False,'developer_bucket_modified':False,'source_review_sha256':sha(args.review),'checks':[],'API_exit_witnesses':[]}
    entered=False;handles=[];email=None
    print('DAMAGED_UPLOAD '+str(api.root),flush=True)
    def save():(api.root/'damaged-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    def call(method,path,body=None,key=None,expect=200,data=None):
        with httpx.Client(timeout=120,trust_env=False) as client:
            headers={'Authorization':'Bearer '+api.token} if api.token else {}
            if key:headers['Idempotency-Key']=key
            r=client.request(method,api.base+'/api/v1'+path,json=body,content=data,headers=headers)
            assert r.status_code==expect,(method,path,r.status_code,r.text[:1500]);return r.json()
    def part(identifier,data=b'PARTIAL',expect=200):return call('PUT',api.prefix+'/uploads/'+identifier+'?offset=0',data=data,expect=expect)
    def create():
        row=call('POST',api.prefix+'/uploads',{'path':'Ref/original.bin','size':19,'expected_sha256':None})
        part(row['id']);return row['id']
    def damage(identifier,kind,**extra):return call('POST','/test/upload-damage/'+str(api.case_id)+'/'+identifier,{'kind':kind,**extra})
    def plan(source):return call('GET',api.prefix+'/file-recovery?source='+source)
    def body(p,action,**extra):return {'action':action,'source':p.get('source','pending'),'snapshot_sha256':p['snapshot_sha256'],
        'expected_revision':p['expected_revision'],'expected_event_sequence':p['expected_event_sequence'],'note':'Actual damaged upload '+action,**extra}
    def action(b,key=None,expect=202):return call('POST',api.prefix+'/file-recovery/actions',b,key or uuid.uuid4().hex,expect)
    def scope(name):
        workspace=api.call('GET','/workspaces')[0];case=api.call('POST','/workspaces/'+str(workspace['id'])+'/cases',{'name':name})
        api.case_id=case['id'];api.prefix='/cases/'+str(case['id'])
    def start():
        nonlocal entered
        api.__enter__();entered=True;birth=api.call('GET','/test/upload-birth');handles.append((ProcessHandle.query(birth),birth))
        if email:api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
    def witness():
        handle,birth=handles[-1];handle.wait_exit(10);report['API_exit_witnesses'].append({'birth':birth,'state':'exited','proof':'retained_creation_handle'})
    try:
        start();api.create_case('Broken upload exact isolation');email=api.call('GET','/auth/me')['email']
        api.put('Ref/current.txt','CURRENT_UNTOUCHED');identifier=create();seed=damage(identifier,'broken')
        listing=call('GET',api.prefix+'/upload-recovery');assert next(v for v in listing['entries'] if v['id']==identifier)['manual_source']==seed['source']
        p=plan(seed['source']);assert any(v['path']==seed['file_path'] and v['sha256']==hashlib.sha256(b'PARTIAL').hexdigest() for v in p['files'])
        kept=action(body(p,'retain_for_review'));assert not kept['result']['write_limits_relaxed']
        if store:
            old=store.raw_client.get_object(Bucket=store.bucket,Key=seed['case_prefix'].rstrip('/')+'/.vibeflow/live-store/head.json')
            try:old_head=old['Body'].read();old_etag=old['ETag']
            finally:old['Body'].close()
        b=body(plan(seed['source']),'isolate_version');key=uuid.uuid4().hex;api.call('POST','/test/raw-isolate-arm')
        try:action(b,key)
        except (OSError,httpx.TransportError):pass
        else:raise AssertionError('Expected actual API exit after physical isolation')
        witness();api.__exit__(None,None,None);entered=False;shutil.copyfile(api.root/'api.log',api.root/'api-before-exit.log');start()
        isolated=action(b,key);assert isolated['state']=='completed' and action(b,key)==isolated
        assert api.call('GET',api.prefix+'/raw?path=Ref/current.txt',raw=True)==b'CURRENT_UNTOUCHED'
        assert api.call('GET',api.prefix+'/file-recovery/actions/'+isolated['id']+'/file?path='+seed['file_path'],raw=True)==b'PARTIAL'
        call('GET',api.prefix+'/uploads/'+identifier,expect=409);part(identifier,b'LATE',expect=409)
        call('POST',api.prefix+'/uploads/'+identifier+':complete',expect=409)
        assert not any(v['id']==identifier for v in call('GET',api.prefix+'/upload-recovery')['entries'])
        if store:
            try:store.raw_client.put_object(Bucket=store.bucket,Key=seed['case_prefix'].rstrip('/')+'/.vibeflow/live-store/head.json',Body=old_head,IfMatch=old_etag)
            except ClientError as error:assert error.response['ResponseMetadata']['HTTPStatusCode']==412
            else:raise AssertionError('Stale catalog write resurrected isolated upload')
        restored=call('GET',api.prefix+'/file-recovery/actions/'+isolated['id']+'/restore-plan?path='+seed['file_path'])
        result=action(body(restored,'restore_file',isolation_id=isolated['id'],file_path=seed['file_path'],target_path='Ref/salvaged-partial.bin',expected_target_sha256=None))
        assert result['state']=='completed' and api.call('GET',api.prefix+'/raw?path=Ref/salvaged-partial.bin',raw=True)==b'PARTIAL'
        report['checks'].append({'kind':'raw_broken_metadata_retained_actual_isolation_API_exit_same_confirm_new_edit_original_upload_permanently_closed','ticket':isolated['id']})
        scope('Missing upload metadata');identifier=create();seed=damage(identifier,'missing');p=plan(seed['source'])
        assert not any(v['path']=='meta.json' for v in p['files'])
        isolated=action(body(p,'isolate_version'));assert isolated['state']=='completed'
        assert api.call('GET',api.prefix+'/file-recovery/actions/'+isolated['id']+'/file?path='+seed['file_path'],raw=True)==b'PARTIAL'
        part(identifier,b'LATE',expect=409)
        report['checks'].append({'kind':'actual_missing_metadata_isolated_original_bytes_preserved'})
        scope('Raw source changes and special entries');identifier=create();seed=damage(identifier,'broken',hardlink=not bool(store));p=plan(seed['source'])
        damage(identifier,'change');action(body(p,'isolate_version'),expect=409)
        p=plan(seed['source'])
        if not store:assert any(v['path']=='link-probe' and v['kind']=='hardlink' for v in p['special_entries'])
        isolated=action(body(p,'isolate_version'));assert isolated['state']=='completed'
        if not store:
            assert (api.root/'outside-upload-link').read_bytes()==b'OUTSIDE_SENTINEL'
            call('GET',api.prefix+'/file-recovery/actions/'+isolated['id']+'/file?path=link-probe',expect=422)
        report['checks'].append({'kind':'actual_source_change_rejected_then_new_confirmation_and_no_followed_special_entry'})
        if not store:
            scope('Root link observed only');identifier=uuid.uuid4().hex;seed=damage(identifier,'root_link');p=plan(seed['source'])
            assert p['files']==[] and p['available_actions']==['retain_for_review'] and p['blocked']['code']=='upload_path_unverified'
            kept=action(body(p,'retain_for_review'));assert kept['state']=='completed'
            action(body(plan(seed['source']),'isolate_version'),expect=409)
            assert (api.root/'outside-upload-root/sentinel').read_bytes()==b'OUTSIDE_SENTINEL'
            report['checks'].append({'kind':'actual_directory_symlink_only_own_link_observed_retention_no_read_or_move'})
        report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1800],
            'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                try:api.call('POST','/test/upload-exit')
                except (OSError,httpx.TransportError):pass
                witness()
            except BaseException as error:report['cleanup_failure']=type(error).__name__
            api.__exit__(RuntimeError if 'failure' in report else None,None,None)
        for handle,_ in handles:handle.close()
        report['passed']=bool(report.get('functional_passed') and len(report['API_exit_witnesses'])==2 and 'cleanup_failure' not in report);save()
    print('DAMAGED_UPLOAD_RESULT '+str(api.root/'damaged-result.json'),flush=True)
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
