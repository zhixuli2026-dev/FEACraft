"""Owned CLI staging against a file-backed S3 facade, not a remote S3 service."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import socket
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic
from codex_session_smoke import CONTROLLED as BASE, WORKER, wait_for

STAGING_WORKER = WORKER.replace("elif method=='thread/start':", "elif method=='thread/start':\n        target=Path.cwd()/'Ref';target.mkdir(exist_ok=True);(target/'cli-data.txt').write_text('CLI',encoding='utf8')")

CONTROLLED = BASE + '''
import hashlib
from app import storage
from app.services import trash
from app.routers import terminal
enabled=False;blocked=set();retried=set();starts={}
original_use=storage._use_s3;unavailable=trash.case_unavailable
storage._use_s3=lambda:enabled or original_use()
for suffix in ('put','get','size','get_range','delete','exists','list'):
    setattr(storage,'_s3_'+suffix,getattr(storage,'_fs_'+suffix))
def listing(prefix,subdir=''):
    items=storage._fs_list(prefix,subdir)
    for item in items:item['etag']=hashlib.sha256(storage._fs_get(prefix,item['path'])).hexdigest()
    return items
storage._s3_list=listing
trash.case_unavailable=lambda case_id,workspace_id:case_id in blocked or unavailable(case_id,workspace_id)
removing=terminal.shutil.rmtree
def remove(path,*args,**kwargs):
    if mode=='cleanup_failure':raise PermissionError('isolated CLI staging lock')
    if mode=='cleanup_retry' and str(path) not in retried:
        retried.add(str(path));raise PermissionError('isolated transient CLI staging lock')
    return removing(path,*args,**kwargs)
terminal.shutil.rmtree=remove
prepare=storage.prepare_job_staging
def preparing(*args):
    stage=prepare(*args)
    if mode in {'cancel_clone','selection_commit_failure'}:
        temporary=root/'cloned.writing';temporary.write_text(json.dumps({'stage':stage}));temporary.replace(root/'cloned.json')
    if mode=='cancel_clone':
        while not (root/'release-clone').exists():time.sleep(.02)
    return stage
storage.prepare_job_staging=preparing
from staging_test_facade import install
install()
from app.services import staging_sync
sync=staging_sync.collect
def syncing(*args):
    if mode=='sync_hold':
        (root/'sync-started').touch()
        while not (root/'release-sync').exists():time.sleep(.02)
    return sync(*args)
staging_sync.collect=syncing
allocate=records.create
async def allocating(*args,**kwargs):
    owner=await allocate(*args,**kwargs)
    if mode=='selection_commit_failure':kwargs['db'].info['cli_fail_commit']=True
    return owner
records.create=allocating
starting=sessions.Session.start
async def start(self):
    starts[self.case_id]=asyncio.current_task()
    return await starting(self)
sessions.Session.start=start
environment=sessions.private_environment
def private(*args):
    result=environment(*args)
    (Path(result[0]['CODEX_HOME'])/'auth.json').write_text('{"access_token":"synthetic-private-only"}')
    return result
sessions.private_environment=private
@app.post('/api/v1/fixture/staging-enable')
async def enable(user=Depends(get_current_user)):
    global enabled
    enabled=True;return {'ok':True}
@app.post('/api/v1/fixture/staging-change/{identifier}/{kind}')
async def change(identifier:str,kind:str,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(NativeCliSession,identifier);selected=json.loads(row.profile_json)
        if kind=='conflict':storage._fs_put(selected['source_prefix'],'Ref/cli-data.txt',b'REMOTE')
        elif kind=='case_unavailable':blocked.add(row.case_id)
        elif kind=='source_rebound':
            case=await db.get(Case,row.case_id);case.s3_prefix+='-rebound';await db.commit()
        return {'ok':True}
@app.get('/api/v1/fixture/staging/{identifier}')
async def details(identifier:str,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(NativeCliSession,identifier);selected=json.loads(row.profile_json);stage=selected['staging_prefix']
        return {'state':records.public(row),'stage':stage,'directory':storage._fs_path(stage,'.').exists(),
                'manifest':storage._staging_manifest_path(stage).exists(),'cleanup_marker':storage._terminal_cleanup_marker(stage).exists(),
                'remote':storage._fs_get(selected['source_prefix'],'Ref/cli-data.txt').decode(),
                'remote_paths':[item['path'] for item in storage._fs_list(selected['source_prefix'])]}
@app.post('/api/v1/fixture/resync/{identifier}')
async def resync(identifier:str,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(NativeCliSession,identifier);selected=json.loads(row.profile_json)
        try:storage.sync_job_staging(selected['staging_prefix'],selected['source_prefix'])
        except storage.StagingConflict:return {'refused':True}
        return {'refused':False}
@app.get('/api/v1/fixture/starting/{case_id}')
async def task_state(case_id:int,user=Depends(get_current_user)):
    task=starts.get(case_id)
    return {'exists':task is not None,'cancelled':task.cancelling() if task is not None else 0,'done':task.done() if task is not None else False}
'''


def run_database(url, folder, only_sync=False, boundary=None):
    from staging_liveness_test import instrument,verify_liveness
    folder.mkdir(parents=True)
    if url is not None:
        alembic(url, folder, 'upgrade', 'head')
    api = IsolatedServer('codex-cli-staging', app='cli_staging:app', database_url=url)
    (api.root / 'cli.py').write_text(STAGING_WORKER, encoding='utf8')
    (api.root / 'cli_staging.py').write_text(instrument(CONTROLLED,boundary), encoding='utf8')
    checks=[]
    with api:
        api.create_case('CLI staging')
        workspace=api.call('GET','/workspaces')[0]['id']
        api.call('POST','/fixture/staging-enable')
        def body(mode):
            api.call('POST','/fixture/mode/'+mode)
            case=api.call('POST',f'/workspaces/{workspace}/cases',{'name':'CLI '+mode+' '+uuid.uuid4().hex[:6]})
            api.call('PUT',f"/cases/{case['id']}/file",{'path':'Ref/cli-data.txt','content':'BEFORE'})
            conv=api.call('POST','/conversations',{'case_id':case['id'],'permission_mode':'full_access'})
            return {'case_id':case['id'],'conversation_id':conv['id']}
        for mode in (('sync_hold',) if only_sync else ('normal','conflict','case_unavailable','source_rebound','sync_hold','cleanup_retry','cleanup_failure')):
            request=body(mode)
            value=api.call('POST','/codex/sessions',request)
            sid=value['id'];path='/codex/sessions/'+sid
            assert api.call('POST',path+'/thread')['thread_id']
            before=api.call('GET','/fixture/staging/'+sid)
            assert before['remote']=='BEFORE' and before['directory'] and before['manifest']
            if mode in {'conflict','case_unavailable','source_rebound'}:
                api.call('POST','/fixture/staging-change/'+sid+'/'+mode)
            expected=502 if mode in {'conflict','case_unavailable','source_rebound','cleanup_failure'} else 200
            if mode=='sync_hold':
                with ThreadPoolExecutor(1) as pool:
                    closing=pool.submit(api.call,'DELETE',path)
                    wait_for(lambda:(api.root/'sync-started').exists(),bool)
                    state=api.call('GET',path)
                    assert state['phase']=='active' and state['cleanup_verified'] is None
                    assert api.call('GET','/fixture/internal/'+sid)['stop']['verified']
                    assert not closing.done()
                    asyncio.run(verify_liveness(api,request['case_id'],'cli',boundary))
                    (api.root/'release-sync').touch()
                    assert closing.result()['cleanup_verified']
            else:
                api.call('DELETE',path,expect=expected)
            details=api.call('GET','/fixture/staging/'+sid)
            state=details['state']
            assert state['cleanup_verified'],(mode,details)
            if mode in {'normal','sync_hold','cleanup_retry'}:
                assert state['phase']=='closed' and details['remote']=='CLI'
                assert not details['directory'] and not details['manifest'] and not details['cleanup_marker']
                assert not any('auth.json' in path or '.private-cli' in path or 'owner.json' in path for path in details['remote_paths'])
                api.call('DELETE',f"/conversations/{request['conversation_id']}")
            else:
                assert state['phase']=='failed' and details['directory'] and details['manifest']
                assert details['remote']==('REMOTE' if mode=='conflict' else 'CLI' if mode=='cleanup_failure' else 'BEFORE')
                if mode=='cleanup_failure':
                    assert details['cleanup_marker']
                    assert api.call('POST','/fixture/resync/'+sid)['refused']
                    api.call('DELETE',f"/conversations/{request['conversation_id']}",expect=409)
            checks.append(mode)
        if only_sync:
            result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
            (folder/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
            return result

        before_count=len(api.call('GET','/codex/sessions'))
        request=body('cancel_clone')
        data=json.dumps(request).encode('utf8')
        connection=socket.create_connection(('127.0.0.1',api.port),timeout=10)
        headers=(f'POST /api/v1/codex/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {api.token}\r\n'
                 f'Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n').encode('ascii')
        connection.sendall(headers+data)
        wait_for(lambda:(api.root/'cloned.json').exists(),bool)
        stage=json.loads((api.root/'cloned.json').read_text())['stage']
        connection.shutdown(socket.SHUT_RDWR);connection.close()
        state=wait_for(lambda:api.call('GET',f"/fixture/starting/{request['case_id']}"),lambda value:value['cancelled']>0)
        assert not state['done']
        (api.root/'release-clone').touch()
        wait_for(lambda:api.call('GET',f"/fixture/starting/{request['case_id']}"),lambda value:value['done'])
        assert len(api.call('GET','/codex/sessions'))==before_count
        assert not (api.root/'files'/stage).exists()
        assert not (api.root/'files'/'.staging'/(Path(stage).name+'.manifest.json')).exists()
        assert not (api.root/'files'/'.staging'/(Path(stage).name+'.terminal-cleanup.json')).exists()
        checks.append('cancel_clone_joins_worker_and_cleans_without_process')

        (api.root/'cloned.json').unlink()
        request=body('selection_commit_failure')
        api.call('POST','/codex/sessions',request,expect=502)
        stage=json.loads((api.root/'cloned.json').read_text())['stage']
        assert not (api.root/'files'/stage).exists()
        assert not (api.root/'files'/'.staging'/(Path(stage).name+'.manifest.json')).exists()
        assert not (api.root/'files'/'.staging'/(Path(stage).name+'.terminal-cleanup.json')).exists()
        assert len(api.call('GET','/codex/sessions'))==before_count
        assert not any(json.loads(path.read_text())['mode'] in {'cancel_clone','selection_commit_failure'} for path in api.root.glob('cli-*.json'))
        checks.append('selection_commit_failure_cleans_unentered_stage')
        result={'passed':True,'count':len(checks),'checks':checks,'product_api':True,'real_native_file_write':True,
                'controlled_s3_facade':True,'actual_remote_s3_service_used':False,'actual_codex_inference':False,'evidence':str(api.root)}
    assert len(checks)==9
    (folder/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='codex-staging-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(run_database,None,root/'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_clistage_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_clistage_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            results['postgresql']=await asyncio.to_thread(run_database,url.set(database=name).render_as_string(hide_password=False),root/'postgresql')
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (root/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'databases':list(results),'evidence':str(root)}))


if __name__=='__main__':asyncio.run(main())
