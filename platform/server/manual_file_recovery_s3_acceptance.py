"""Real disposable S3 manual isolation/new edits, actual API death and ACLs."""
import argparse,asyncio,hashlib,json,re,shutil,subprocess,tempfile,time,traceback,uuid
from pathlib import Path
import httpx
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from botocore.exceptions import ClientError
from testsupport import IsolatedServer,SERVER
from s3testsupport import ObjectStore

ROOT=SERVER.parents[1]
RUNTIME=ROOT/'third_party/seaweed-4.52-runtime/weed'
PREFIX="import os,json\nfrom pathlib import Path\nos.environ.update(json.loads((Path(__file__).parent/'isolated-store.json').read_text(encoding='utf8')))\n"
EXTRA='''
import os,json,uuid
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app import storage
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Case,Workspace,Membership
from app.routers.workspaces import get_owned_case
from app.services import manual_file_recovery as manual,manual_file_recovery_s3 as remote
from app.services.durable_events import lock_case_execution
from app.services.process_identity_native import observe
from app.routers import manual_files as routes
public_error=routes.public_error
def error_trace(error):
    import traceback
    chain=[];item=error
    while item is not None and len(chain)<8:
        chain.append({'type':type(item).__name__,'message':str(item)[:500] if isinstance(item,(ValueError,manual.CaseWriteConflict)) else '',
            'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(item.__traceback__)]})
        item=item.__cause__
    (Path.cwd()/'error-trace.json').write_text(json.dumps(chain,indent=2),encoding='utf8')
    return public_error(error)
routes.public_error=error_trace
@app.get('/api/v1/test/manual-birth')
async def birth():return observe(os.getpid())
@app.post('/api/v1/test/manual-exit')
async def exit_api():os._exit(0)
@app.post('/api/v1/test/s3-source/{case_id}')
async def source(case_id:int,body:dict,user=Depends(get_current_user)):
    from app.services import attempt_objects as objects,s3_file_transactions as journal
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True);await lock_case_execution(db,case_id)
        identifier=str(uuid.uuid4());directory=journal._directory(identifier)
        for path,data in [('before/0',b'S3_ACTUAL_BEFORE'),('after/0',b'S3_ACTUAL_AFTER'),('manifest.json',b'{"partial":')]:
            objects.put_new(case.s3_prefix,directory+'/'+path,data)
        encoded=b'{"partial":'
        if body.get('known_live_writer'):
            encoded=json.dumps({'plan':{'id':identifier,'scope':'staging','writer':{'schema':'vibeflow.staging-writer.v1','process':observe(os.getpid())}},
                'plan_sha256':'0'*64,'event_id':str(uuid.uuid4())}).encode()
        if body.get('pending',True):storage.s3().put_object(**objects.arguments(case.s3_prefix,journal.PENDING),Body=encoded,IfNoneMatch='*')
        if body.get('fault'):(Path.cwd()/('fault-'+body['fault'])).touch()
        await db.commit()
        return {'source':'objects/'+identifier,'case_prefix':case.s3_prefix}
@app.post('/api/v1/test/s3-fault')
async def arm(body:dict,user=Depends(get_current_user)):
    assert body['fault'] in {'isolate','restore_before_commit','restore_after_commit'}
    (Path.cwd()/('fault-'+body['fault'])).touch();return {'armed':body['fault']}
@app.post('/api/v1/test/manual-access')
async def access(body:dict,user=Depends(get_current_user)):
    from app.services import sharing
    async with SessionLocal() as db:
        case=await db.get(Case,body['case_id']);workspace=await db.get(Workspace,case.workspace_id)
        if body['mode']=='restore':workspace.user_id=user.id
        else:
            workspace.user_id=body['other_user_id']
            owner=await db.scalar(select(Membership).where(Membership.user_id==body['other_user_id'],Membership.role=='admin').limit(1))
            member=await db.scalar(select(Membership).where(Membership.user_id==user.id,Membership.org_id==owner.org_id))
            if member is None:member=Membership(user_id=user.id,org_id=owner.org_id,role='viewer');db.add(member)
            member.role='member' if body['mode']=='member' else 'viewer'
            await sharing.set_share(db,workspace,owner.org_id,'member' if body['mode']=='member' else 'viewer',body['mode']!='revoked')
        await db.commit()
    return {'mode':body['mode']}
actual_isolate=remote.isolate
def isolate(*args,**kwargs):
    value=actual_isolate(*args,**kwargs);armed=Path.cwd()/'fault-isolate'
    if armed.exists():armed.unlink();os._exit(0)
    return value
remote.isolate=isolate
actual_apply=remote.remote._apply
def apply(*args,**kwargs):
    value=actual_apply(*args,**kwargs);armed=Path.cwd()/'fault-restore_before_commit'
    if armed.exists() and remote._restore.get() is not None and args[4]=='after':armed.unlink();os._exit(0)
    return value
remote.remote._apply=apply
actual_commit=AsyncSession.commit
async def commit(self):
    value=await actual_commit(self);armed=Path.cwd()/'fault-restore_after_commit'
    if armed.exists() and self.info.get('local_file_transactions'):armed.unlink();os._exit(0)
    return value
AsyncSession.commit=commit
'''
APP=PREFIX+EXTRA


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(args,store,database_url=None):
    from app.services.windows_process_scope import ProcessHandle
    api=IsolatedServer('manual-file-S3',app='manual_s3_fixture:app',database_url=database_url)
    (api.root/'manual_s3_fixture.py').write_bytes(APP.encode());(api.root/'isolated-store.json').write_text(json.dumps(store.settings()),encoding='utf8')
    report={'passed':False,'backend':'s3+postgresql' if database_url else 's3+sqlite','new_real_model_calls':0,'new_mock_llm_calls':0,
        'developer_database_modified':False,'developer_bucket_modified':False,'source_review_sha256':sha(args.review),'checks':[],'API_exit_witnesses':[]}
    entered=False;handles=[];email=None;handle=birth=None
    print('MANUAL_FILE_S3 '+str(api.root),flush=True)
    def save():(api.root/'s3-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    def call(method,path,body=None,expect=200,key=None,token=None,server=None):
        server=server or api
        with httpx.Client(timeout=120,trust_env=False) as client:
            headers={'Authorization':'Bearer '+(token or server.token)} if token or server.token else {}
            if key:headers['Idempotency-Key']=key
            r=client.request(method,server.base+'/api/v1'+path,json=body,headers=headers)
            assert r.status_code==expect,(method,path,r.status_code,r.text[:1000])
            return r.json()
    def plan(source='pending'):return call('GET',api.prefix+'/file-recovery?source='+source)
    def body(p,action,**extra):return {'action':action,'source':p.get('source','pending'),'snapshot_sha256':p['snapshot_sha256'],
        'expected_revision':p['expected_revision'],'expected_event_sequence':p['expected_event_sequence'],'note':'Actual remote version '+action,**extra}
    def action(b,key=None,expect=202,server=None):
        return call('POST',api.prefix+'/file-recovery/actions',b,expect,key or uuid.uuid4().hex,server=server)
    def read(path):
        r=store.client.get_object(Bucket=store.bucket,Key=case_prefix+'/'+path)
        try:return r['Body'].read()
        finally:r['Body'].close()
    def prefix_for_api():
        return call('POST','/test/s3-source/'+str(api.case_id),{'pending':False})
    def start():
        nonlocal entered,handle,birth
        api.__enter__();entered=True;birth=api.call('GET','/test/manual-birth');handle=ProcessHandle.query(birth);handles.append(handle)
        if email:api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
    def exit_witness():
        handle.wait_exit(10);report['API_exit_witnesses'].append({'birth':birth,'state':'exited','proof':'retained_creation_handle'})
    def restart():
        nonlocal entered
        exit_witness();api.__exit__(None,None,None);entered=False
        shutil.copyfile(api.root/'api.log',api.root/('api-exit-'+str(len(report['API_exit_witnesses']))+'.log'));start()
    def interrupt(b,key):
        try:action(b,key)
        except (OSError,httpx.TransportError):pass
        else:raise AssertionError('Expected real API exit')
        restart()

    @contextmanager
    def concurrent_peer():
        if database_url is None:
            yield api
            return
        peer=IsolatedServer('manual-file-S3-peer',app=api.app,database_url=database_url)
        settings={**store.settings(),'VIBEFLOW_JWT_SECRET':api.env['VIBEFLOW_JWT_SECRET']}
        (peer.root/'manual_s3_fixture.py').write_bytes(APP.encode())
        (peer.root/'isolated-store.json').write_text(json.dumps(settings),encoding='utf8')
        with peer:
            peer.token=api.token
            peer_birth=peer.call('GET','/test/manual-birth')
            assert peer_birth!=birth and peer.base!=api.base
            peer_handle=ProcessHandle.query(peer_birth)
            try:
                yield peer
            finally:
                try:
                    try:peer.call('POST','/test/manual-exit')
                    except OSError:pass
                    peer_handle.wait_exit(10)
                    report['peer_API_exit_witness']={'birth':peer_birth,'state':'exited','proof':'retained_creation_handle'}
                finally:peer_handle.close()
    try:
        start();api.create_case('Remote manual versions actual');email=api.call('GET','/auth/me')['email']
        api.put('Ref/live.txt','S3_EXTERNAL_CURRENT')
        seeded=call('POST','/test/s3-source/'+str(api.case_id),{});case_prefix=seeded['case_prefix'];objects_source=seeded['source']
        p=plan();kept=action(body(p,'retain_for_review'));assert kept['result']['write_limits_relaxed'] is False
        raw=store.raw_client.get_object(Bucket=store.bucket,Key=case_prefix+'/.vibeflow/live-store/head.json')
        try:old_head=raw['Body'].read();old_etag=raw['ETag']
        finally:raw['Body'].close()
        isolated=action(body(plan(),'isolate_version'));assert isolated['state']=='completed' and isolated['result']['pending_pointer_detached']
        assert read('Ref/live.txt')==b'S3_EXTERNAL_CURRENT'
        try:store.raw_client.put_object(Bucket=store.bucket,Key=case_prefix+'/.vibeflow/live-store/head.json',Body=old_head,IfMatch=old_etag)
        except ClientError as error:assert error.response['ResponseMetadata']['HTTPStatusCode']==412
        else:raise AssertionError('Old catalog identity resurrected the pending pointer')
        report['checks'].append({'kind':'corrupt_pending_preserved_detached_no_live_overwrite_and_actual_stale_CAS_412','ticket':isolated['id'],
            'scope':'real stale conditional HTTP PUT; in-flight proxy timing not claimed'})
        p=plan(objects_source);key=uuid.uuid4().hex;b=body(p,'isolate_version');isolation=action(b,key)
        assert isolation['state']=='completed' and action(b,key)==isolation
        assert read(isolation['result']['retained_path']+'/before/0')==b'S3_ACTUAL_BEFORE'
        assert read('.vibeflow/file-transactions/'+objects_source+'/before/0')==b'S3_ACTUAL_BEFORE'
        restore=call('GET',api.prefix+'/file-recovery/actions/'+isolation['id']+'/restore-plan?path=before/0')
        version=call('GET',api.prefix+'/file:version?path=Ref/live.txt')
        b=body(restore,'restore_file',isolation_id=isolation['id'],file_path='before/0',target_path='Ref/live.txt',expected_target_sha256=version['content_sha256'])
        key=uuid.uuid4().hex;restored=action(b,key);assert restored['state']=='completed' and read('Ref/live.txt')==b'S3_ACTUAL_BEFORE'
        api.put('Ref/live.txt','S3_NEWER_EDIT');assert action(b,key)==restored and read('Ref/live.txt')==b'S3_NEWER_EDIT'
        report['checks'].append({'kind':'remote_objects_preserved_independent_candidate_new_edit_replay_never_overwrites_newer','ticket':restored['id']})
        concurrent=call('POST','/test/s3-source/'+str(api.case_id),{'pending':False})
        concurrent_body=body(plan(concurrent['source']),'isolate_version');concurrent_key=uuid.uuid4().hex
        with concurrent_peer() as peer, ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(action,concurrent_body,concurrent_key)
            second=pool.submit(action,concurrent_body,concurrent_key,server=peer)
            one,two=first.result(),second.result()
        assert one['id']==two['id'];states=[one['state'],two['state']];deadline=time.monotonic()+30
        while one['state']!='completed':
            assert time.monotonic()<deadline;time.sleep(.1);one=action(concurrent_body,concurrent_key)
        assert action(concurrent_body,concurrent_key)==one
        report['checks'].append({'kind':'actual_same_key_concurrent_remote_isolation_one_receipt','ticket':one['id'],
            'initial_states':states,'independent_API_processes':peer is not api})
        outsider=call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Isolated ACL other'})
        other=call('GET','/auth/me',token=outsider['token']);access={'case_id':api.case_id,'other_user_id':other['id']}
        call('GET',api.prefix+'/file-recovery/actions',expect=404,token=outsider['token'])
        try:
            call('POST','/test/manual-access',{**access,'mode':'viewer'})
            readonly=plan(objects_source);assert readonly['available_actions']==[] and readonly['blocked']['code']=='read_only'
            readonly_restore=call('GET',api.prefix+'/file-recovery/actions/'+isolation['id']+'/restore-plan?path=before/0')
            assert not readonly_restore['available'] and readonly_restore['blocked']['code']=='read_only'
            action(b,key,expect=404)
            call('POST','/test/manual-access',{**access,'mode':'member'});assert action(b,key)==restored
            call('POST','/test/manual-access',{**access,'mode':'revoked'})
            call('GET',api.prefix+'/file-recovery/actions',expect=404);action(b,key,expect=404)
        finally:call('POST','/test/manual-access',{**access,'mode':'restore'})
        report['checks'].append({'kind':'foreign_viewer_member_revoked_before_committed_receipt'})
        seeded=call('POST','/test/s3-source/'+str(api.case_id),{'fault':'isolate'})
        p=plan();b=body(p,'isolate_version');key=uuid.uuid4().hex;interrupt(b,key)
        final=action(b,key);assert final['state']=='completed' and action(b,key)==final
        report['checks'].append({'kind':'actual_API_exit_after_remote_detachment_same_confirm_finishes','ticket':final['id']})
        for fault,chosen in [('restore_before_commit','before/0'),('restore_after_commit','after/0')]:
            p=call('GET',api.prefix+'/file-recovery/actions/'+isolation['id']+'/restore-plan?path='+chosen)
            version=call('GET',api.prefix+'/file:version?path=Ref/live.txt')
            b=body(p,'restore_file',isolation_id=isolation['id'],file_path=chosen,target_path='Ref/live.txt',expected_target_sha256=version['content_sha256'])
            call('POST','/test/s3-fault',{'fault':fault});key=uuid.uuid4().hex;interrupt(b,key)
            done=action(b,key);assert done['state']=='completed' and action(b,key)==done
            assert read('Ref/live.txt')==(b'S3_ACTUAL_BEFORE' if chosen=='before/0' else b'S3_ACTUAL_AFTER')
            report['checks'].append({'kind':'actual_API_exit_'+fault+'_original_confirm_reconciles','ticket':done['id']})
        report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1800],
            'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                try:api.call('POST','/test/manual-exit')
                except (OSError,httpx.TransportError):pass
                exit_witness()
            except BaseException as error:report['cleanup_failure']=type(error).__name__
            api.__exit__(RuntimeError if 'failure' in report else None,None,None)
        for value in handles:value.close()
        report['passed']=bool(report.get('functional_passed') and len(report['API_exit_witnesses'])==4
            and (database_url is None or report.get('peer_API_exit_witness')) and 'cleanup_failure' not in report)
        save()
    print('MANUAL_FILE_S3_RESULT '+str(api.root/'s3-result.json'),flush=True)
    return report['passed'],api.root


def run_store(args,database_url):
    with ObjectStore(executable=RUNTIME) as store:passed,root=verify(args,store,database_url)
    probe="""import json,pathlib,sys
r=pathlib.Path(sys.argv[1]);o=json.loads((r/'owner.json').read_text());p=pathlib.Path('/proc')/str(o['pid'])
same=p.exists() and (p/'stat').read_text().rsplit(')',1)[1].split()[19]==o['start'] and p.stat().st_uid==o['uid']
assert not same
print('ORIGINAL_STORE_BIRTH_ABSENT')
"""
    verified=subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c',probe,store.linux],capture_output=True,text=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
    result={'passed':passed and verified.returncode==0,'functional_result':str(root/'s3-result.json'),'actual_native_store_exited':verified.returncode==0,
        'store_root':str(store.root),'developer_database_modified':False,'developer_bucket_modified':False,'new_real_model_calls':0,'new_mock_llm_calls':0}
    (root/'store-exit-verified.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    return 0 if result['passed'] else 1


async def main():
    from live_llm_terminal_agent_acceptance import disposable_database
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    sources=review['source_hashes']
    assert 'platform/server/manual_file_recovery_s3_acceptance.py' in sources
    for name,digest in sources.items():assert sha(ROOT/name)==digest,name
    async with disposable_database(args.postgres) as database_url:
        return await asyncio.to_thread(run_store,args,database_url)


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
