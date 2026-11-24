"""Actual upload discovery/adoption/source enforcement and native cancel exit."""
import argparse,hashlib,json,shutil,subprocess,time,traceback,uuid
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from s3testsupport import ObjectStore

ROOT=SERVER.parents[1]
RUNTIME=ROOT/'third_party/seaweed-4.52-runtime/weed'
PREFIX="import os,json\nfrom pathlib import Path\np=Path(__file__).parent/'isolated-store.json'\nif p.exists():os.environ.update(json.loads(p.read_text(encoding='utf8')))\n"
APP=PREFIX+'''
import os
from pathlib import Path
from fastapi import Depends
from sqlalchemy import delete
from app.main import app
from app.db import SessionLocal
from app.models import UploadStorageReservation,UploadChunkCredit
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services.process_identity_native import observe
from app.services import uploads
@app.get('/api/v1/test/upload-birth')
async def birth():return observe(os.getpid())
@app.post('/api/v1/test/upload-exit')
async def exit_api():os._exit(0)
@app.post('/api/v1/test/upload-legacy/{case_id}/{identifier}')
async def legacy(case_id:int,identifier:str,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        await get_owned_case(case_id,user,db,write=True)
        await db.execute(delete(UploadChunkCredit).where(UploadChunkCredit.upload_id==identifier))
        await db.execute(delete(UploadStorageReservation).where(UploadStorageReservation.id==identifier,UploadStorageReservation.case_id==case_id))
        await db.commit()
    return {'legacy_without_SQL_reservation':True}
@app.post('/api/v1/test/upload-arm-cancel-exit')
async def arm(user=Depends(get_current_user)):
    (Path.cwd()/'cancel-exit').touch();return {'armed':True}
actual_cancel=uploads.cancel_session
def cancel(*args,**kwargs):
    result=actual_cancel(*args,**kwargs)
    armed=Path.cwd()/'cancel-exit'
    if armed.exists():armed.unlink();os._exit(0)
    return result
uploads.cancel_session=cancel
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(args,store=None):
    from app.services.windows_process_scope import ProcessHandle
    api=IsolatedServer('upload-recovery-S3' if store else 'upload-recovery-FS',app='upload_fixture:app')
    (api.root/'upload_fixture.py').write_bytes(APP.encode())
    if store:(api.root/'isolated-store.json').write_text(json.dumps(store.settings()),encoding='utf8')
    report={'passed':False,'backend':'s3+sqlite' if store else 'fs+sqlite','new_real_model_calls':0,'new_mock_llm_calls':0,
        'developer_database_modified':False,'developer_bucket_modified':False,'source_review_sha256':sha(args.review),
        'checks':[],'API_exit_witnesses':[]}
    entered=False;handles=[];email=None
    print('UPLOAD_RECOVERY '+str(api.root),flush=True)
    def save():(api.root/'upload-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    def call(method,path,body=None,key=None,expect=200,data=None):
        with httpx.Client(timeout=120,trust_env=False) as client:
            headers={'Authorization':'Bearer '+api.token} if api.token else {}
            if key:headers['Idempotency-Key']=key
            r=client.request(method,api.base+'/api/v1'+path,json=body,content=data,headers=headers)
            assert r.status_code==expect,(method,path,r.status_code,r.text[:1500])
            return r.json()
    def create(path,size):return call('POST',api.prefix+'/uploads',{'path':path,'size':size,'expected_sha256':None})
    def part(identifier,offset,data,expect=200):return call('PUT',api.prefix+'/uploads/'+identifier+'?offset='+str(offset),data=data,expect=expect)
    def plan(identifier):return call('GET',api.prefix+'/upload-recovery/'+identifier)
    def body(p,action,**kw):return {'action':action,'upload_id':p['id'],'snapshot_sha256':p['snapshot_sha256'],
        'note':'Actual upload '+action,'source_blocks':None,'prefix_blocks':None,**kw}
    def action(b,key=None,expect=202):return call('POST',api.prefix+'/upload-recovery/actions',b,key or uuid.uuid4().hex,expect)
    def start():
        nonlocal entered
        api.__enter__();entered=True;birth=api.call('GET','/test/upload-birth');handle=ProcessHandle.query(birth);handles.append((handle,birth))
        if email:api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
    def witness():
        handle,birth=handles[-1];handle.wait_exit(10);report['API_exit_witnesses'].append({'birth':birth,'state':'exited','proof':'retained_creation_handle'})
    try:
        start();api.create_case('Actual lost browser upload recovery');email=api.call('GET','/auth/me')['email']
        size=4*1024*1024+17;data=(uuid.uuid4().hex.encode()*(size//32+1))[:size]
        identifier=create('Ref/adopted.bin',size)['id'];part(identifier,0,data[:7])
        listing=call('GET',api.prefix+'/upload-recovery');assert any(v['id']==identifier and v['offset']==7 for v in listing['entries'])
        p=plan(identifier);assert p['prefix_blocks']==[{'size':7,'sha256':hashlib.sha256(data[:7]).hexdigest()}]
        b=body(p,'retain_for_review');key=uuid.uuid4().hex;kept=action(b,key);assert action(b,key)==kept and not kept['result']['write_limits_relaxed']
        report['checks'].append({'kind':'actual_partial_bytes_discovered_and_retained_same_receipt'})
        api.call('POST','/test/upload-legacy/'+str(api.case_id)+'/'+identifier)
        listing=call('GET',api.prefix+'/upload-recovery');assert next(v for v in listing['entries'] if v['id']==identifier)['legacy_without_reservation']
        p=plan(identifier);blocks=[hashlib.sha256(data[i:i+4*1024*1024]).hexdigest() for i in range(0,size,4*1024*1024)]
        bad=body(p,'resume_upload',source_blocks=blocks,prefix_blocks=[{'size':7,'sha256':'0'*64}]);action(bad,expect=409)
        b=body(p,'resume_upload',source_blocks=blocks,prefix_blocks=p['prefix_blocks']);key=uuid.uuid4().hex;resumed=action(b,key)
        assert resumed['state']=='completed' and resumed['result']['upload']['id']==identifier and action(b,key)==resumed
        wrong=bytearray(data[7:4*1024*1024]);wrong[-1]^=1;part(identifier,7,bytes(wrong),expect=409)
        assert call('GET',api.prefix+'/uploads/'+identifier)['offset']==7
        part(identifier,7,data[7:4*1024*1024]);part(identifier,4*1024*1024,data[4*1024*1024:])
        completed=call('POST',api.prefix+'/uploads/'+identifier+':complete');assert completed['sha256']==hashlib.sha256(data).hexdigest()
        assert api.call('GET',api.prefix+'/raw?path=Ref/adopted.bin',raw=True)==data
        api.put('Ref/adopted.bin','NEWER_AFTER_UPLOAD')
        assert action(b,key)==resumed
        assert call('POST',api.prefix+'/uploads/'+identifier+':complete')['sha256']==completed['sha256']
        assert api.call('GET',api.prefix+'/raw?path=Ref/adopted.bin',raw=True)==b'NEWER_AFTER_UPLOAD'
        report['checks'].append({'kind':'actual_legacy_adoption_arbitrary_offset_source_guard_wrong_chunk_refused_complete_and_replay_newer_kept'})
        other=create('Ref/cancelled.bin',19)['id'];part(other,0,b'partial')
        api.put('Ref/cancelled.bin','EXTERNAL_TARGET')
        p=plan(other);assert not p['target_matches'] and 'resume_upload' not in p['available_actions'] and 'cancel_upload' in p['available_actions']
        b=body(p,'cancel_upload');key=uuid.uuid4().hex;api.call('POST','/test/upload-arm-cancel-exit')
        try:action(b,key)
        except (OSError,httpx.TransportError):pass
        else:raise AssertionError('Expected real API exit after physical cancellation')
        witness();api.__exit__(None,None,None);entered=False;shutil.copyfile(api.root/'api.log',api.root/'api-before-exit.log');start()
        cancelled=action(b,key);assert cancelled['state']=='completed' and cancelled['result']['cancelled'] and action(b,key)==cancelled
        assert api.call('GET',api.prefix+'/raw?path=Ref/cancelled.bin',raw=True)==b'EXTERNAL_TARGET'
        part(other,7,b'late',expect=409)
        assert not any(v['id']==other for v in call('GET',api.prefix+'/upload-recovery')['entries'])
        report['checks'].append({'kind':'actual_target_conflict_cancel_cleanup_API_exit_same_confirm_no_target_change_late_write_denied'})
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
        for h,_ in handles:h.close()
        report['passed']=bool(report.get('functional_passed') and len(report['API_exit_witnesses'])==2 and 'cleanup_failure' not in report);save()
    print('UPLOAD_RECOVERY_RESULT '+str(api.root/'upload-result.json'),flush=True)
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
