"""Real S3 logical versions, legacy migration and HTTP requests after owner death."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import uuid

import httpx
from botocore.exceptions import ClientError
from app.services.s3_live_store import Client, HEAD, DIRECTORY, PENDING
from s3_delay_proxy_test import DelayedRequest
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME
from staging_sync_smoke import boot
from staging_transfer_smoke import EXTRA, kill_fixture
from s3_file_transactions_smoke import get
from testsupport import SERVER

EXTRA += '''
@app.get('/fixture/live-prefix/{case_id}')
async def live_prefix(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
 case=await get_owned_case(case_id,user,db)
 return {'prefix':case.s3_prefix}
'''


def rejected(call):
    try:call()
    except ClientError as error:
        assert error.response['ResponseMetadata']['HTTPStatusCode']==412, error
    else:raise AssertionError('Stale conditional operation was accepted')


def adapter_checks(store):
    prefix='workspaces/91/cases/1';args={'Bucket':store.bucket}
    raw=store.raw_client;client=store.client;checks=[]
    raw.put_object(**args,Key=prefix+'/old.txt',Body=b'old',ContentType='text/plain',Metadata={'tag':'kept'})
    raw.put_object(**args,Key=prefix+'/gone.txt',Body=b'gone')
    client.fence(store.bucket,prefix)
    # Raw old-format requests can arrive after migration, but no migrated read
    # or inventory falls back to these physical keys.
    raw.put_object(**args,Key=prefix+'/old.txt',Body=b'late old writer')
    raw.put_object(**args,Key=prefix+'/late.txt',Body=b'late new file')
    assert get(store,store.bucket,prefix+'/old.txt')==b'old'
    assert get(store,store.bucket,prefix+'/late.txt') is None
    head=client.head_object(**args,Key=prefix+'/old.txt');old=head['ETag']
    assert head['Metadata']=={'tag':'kept'} and head['ContentType']=='text/plain'
    response=client.get_object(**args,Key=prefix+'/old.txt',Range='bytes=1-2',IfMatch=old)
    try:assert response['Body'].read()==b'ld' and response['ContentRange']=='bytes 1-2/3'
    finally:response['Body'].close()
    checks.append('legacy_complete_import_metadata_range_and_late_raw_writes_invisible')
    client.put_object(**args,Key=prefix+'/old.txt',Body=b'old',IfMatch=old)
    second=client.head_object(**args,Key=prefix+'/old.txt')['ETag'];assert second!=old
    rejected(lambda:client.put_object(**args,Key=prefix+'/old.txt',Body=b'bad',IfMatch=old))
    rejected(lambda:client.get_object(**args,Key=prefix+'/old.txt',IfMatch=old))
    rejected(lambda:client.delete_object(**args,Key=prefix+'/old.txt',IfMatch=old))
    rejected(lambda:client.put_object(**args,Key=prefix+'/old.txt',Body=b'bad',IfNoneMatch='*'))
    checks.append('same_bytes_receive_new_version_and_reject_stale_read_write_delete')
    private=prefix+'/.vibeflow/publications/test/immutable.txt'
    client.put_object(**args,Key=private,Body=b'private',IfNoneMatch='*')
    client.delete_object(**args,Key=prefix+'/gone.txt')
    pages=list(client.get_paginator('list_objects_v2').paginate(**args,Prefix=prefix+'/',PaginationConfig={'PageSize':1}))
    paths=[row['Key'] for page in pages for row in page.get('Contents',[])]
    assert paths==[private,prefix+'/old.txt'],paths
    assert not client.list_objects_v2(**args,Prefix=prefix+'/gone').get('Contents')
    assert [row['Key'] for row in client.list_objects_v2(**args,Prefix=prefix+'/old').get('Contents',[])]==[prefix+'/old.txt']
    checks.append('logical_listing_pagination_and_subprefix_hide_private_store_and_deleted_legacy_keys')
    url=client.generate_presigned_url('get_object',Params={**args,'Key':prefix+'/old.txt'},ExpiresIn=60)
    client.put_object(**args,Key=prefix+'/old.txt',Body=b'changed',IfMatch=second)
    with httpx.Client(trust_env=False) as http:
        response=http.get(url);assert response.status_code==200 and response.content==b'old'
    checks.append('presigned_download_pins_immutable_version_after_new_edit')
    original,etag=client._load(store.bucket,prefix)
    client.fence(store.bucket,prefix)
    rejected(lambda:client._save(store.bucket,prefix,original,etag))
    checks.append('unchanged_content_recovery_fence_rejects_prior_head')
    class IgnoredConditions:
        def __getattr__(self, name):return getattr(raw, name)
        def put_object(self, **kwargs):
            kwargs.pop('IfMatch', None);kwargs.pop('IfNoneMatch', None)
            return raw.put_object(**kwargs)
    unsupported=Client(IgnoredConditions());foreign='workspaces/91/cases/2'
    try:unsupported.put_object(**args,Key=foreign+'/rejected.txt',Body=b'never visible')
    except ValueError as error:assert 'CAS conditions' in str(error),error
    else:raise AssertionError('Ignored backend CAS accepted')
    assert client._load(store.bucket,foreign)==(None,None)
    checks.append('backend_ignoring_preconditions_cannot_publish_a_live_head')
    for same_file in (False, True):
        barrier=threading.Barrier(2);local=threading.local();save=client._save;conflicts=[]
        def racing_save(*positional,**kwargs):
            if not getattr(local,'entered',False):
                local.entered=True;barrier.wait(timeout=10)
            try:return save(*positional,**kwargs)
            except ClientError as error:
                conflicts.append(error.response['ResponseMetadata']['HTTPStatusCode'])
                raise
        client._save=racing_save
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                targets=[prefix+'/same.txt']*2 if same_file else [prefix+'/one.txt',prefix+'/two.txt']
                futures=[pool.submit(client.put_object,**args,Key=path,Body=str(i).encode(),IfNoneMatch='*')
                         for i,path in enumerate(targets)]
                successes=0
                for future in futures:
                    try:future.result(timeout=20);successes+=1
                    except ClientError as error:assert error.response['ResponseMetadata']['HTTPStatusCode']==412,error
                assert successes==(1 if same_file else 2),successes
                assert conflicts and set(conflicts)=={412},conflicts
        finally:client._save=save
        if not same_file:
            assert get(store,store.bucket,prefix+'/one.txt')==b'0'
            assert get(store,store.bucket,prefix+'/two.txt')==b'1'
        checks.append('concurrent_same_file_conflict_preserved' if same_file else 'concurrent_distinct_files_merge_without_lost_update')
    entered=threading.Event();release=threading.Event();save=client._save;main_thread=threading.current_thread()
    def delayed_create(*positional,**kwargs):
        if threading.current_thread() is not main_thread and not entered.is_set():
            entered.set();assert release.wait(10)
        return save(*positional,**kwargs)
    client._save=delayed_create
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(client.put_object,**args,Key=prefix+'/aba-create.txt',Body=b'stale',IfNoneMatch='*')
            try:
                assert entered.wait(10)
                client.put_object(**args,Key=prefix+'/aba-create.txt',Body=b'newer',IfNoneMatch='*')
                client.delete_object(**args,Key=prefix+'/aba-create.txt')
            finally:release.set()
            rejected(lambda:future.result(timeout=10))
    finally:client._save=save
    assert get(store,store.bucket,prefix+'/aba-create.txt') is None
    checks.append('create_delete_absence_cannot_rebase_an_older_create')
    return checks


def run(store, database_url=None, modes=None):
    report=Path(tempfile.mkdtemp(prefix='s3-live-store-evidence-',dir=SERVER.parents[1]/'tmp'))
    checks=[]
    for selected in modes or ('blob','pending','update','delete','aba','committed_clear','update_unedited','pending_unedited'):
        with DelayedRequest(store.endpoint) as proxy:
            settings=store.settings();settings['VIBEFLOW_S3_ENDPOINT']=proxy.endpoint
            if not database_url:settings['VIBEFLOW_DATABASE_URL']='sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix()
            api=boot(settings,database_url)
            fixture=api.root/'staging_fixture.py';fixture.write_text(fixture.read_text(encoding='utf8')+EXTRA,encoding='utf8')
            with api:
                case=api.create_case('HTTP late '+selected);api.put('fault.txt','old');api.put('remove.txt','remove')
                prefix=api.call('GET',f'/fixture/live-prefix/{case["id"]}',api_prefix=False)['prefix']
                value,_=store.client._load(store.bucket,prefix);original=value['files']['fault.txt']['etag']
                def select(method,path,headers,body):
                    if method!='PUT':return False
                    if selected=='blob':return '/'+DIRECTORY+'/objects/' in path and path.endswith('/fault.txt') and body==b'new'
                    if not path.endswith('/'+HEAD):return False
                    files={path:entry for path,entry in json.loads(body)['files'].items() if not entry.get('deleted')}
                    changed=files.get('fault.txt',{}).get('etag')!=original
                    if selected in {'pending','pending_unedited'}:return PENDING in files
                    if selected in {'update','update_unedited'}:return changed and PENDING in files
                    if selected=='aba':return changed and 'new.txt' in files
                    if selected=='delete':return changed and 'remove.txt' not in files
                    return changed and PENDING not in files
                shared={**settings,'VIBEFLOW_JWT_SECRET':api.env['VIBEFLOW_JWT_SECRET']}
                with boot(shared,database_url) as peer,ThreadPoolExecutor(max_workers=1) as pool:
                    peer.token=api.token;proxy.select=select
                    future=pool.submit(api.call,'POST',f'/fixture/transfer/{case["id"]}',api_prefix=False)
                    assert proxy.ready.wait(35),'No selected real HTTP request received: '+selected
                    process=api.call('GET',f'/fixture/storage-owner/{case["id"]}',api_prefix=False)['process']
                    chosen=json.loads((api.root/'run.json').read_text())
                    assert chosen['prefix']==prefix
                    kill_fixture(process,future)
                    peer.call('POST',f'/fixture/recover/{case["id"]}',api_prefix=False)
                    committed=selected=='committed_clear'
                    assert get(store,store.bucket,prefix+'/fault.txt')==(b'new' if committed else b'old')
                    assert get(store,store.bucket,prefix+'/new.txt')==(b'created' if committed else None)
                    assert get(store,store.bucket,prefix+'/remove.txt')==(None if committed else b'remove')
                    assert get(store,store.bucket,prefix+'/'+PENDING) is None
                    # Also test without a newer edit: otherwise that edit alone
                    # could invalidate the old CAS and hide a missing recovery fence.
                    unedited=selected.endswith('_unedited')
                    if not unedited:
                        peer.call('PUT',f'/cases/{case["id"]}/file',{'path':'fault.txt','content':'newer'})
                    proxy.release.set();assert proxy.forwarded.wait(20)
                    assert proxy.status==(200 if selected=='blob' else 412),proxy.status
                    expected=b'old' if unedited else b'newer'
                    assert get(store,store.bucket,prefix+'/fault.txt')==expected
                    assert peer.call('GET',f'/cases/{case["id"]}/raw?path=fault.txt',raw=True)==expected
                    assert get(store,store.bucket,prefix+'/'+PENDING) is None
                    checks.append({'mode':selected,'late_http_status':proxy.status,'api':str(api.root),'peer':str(peer.root),
                                   'native_owner_killed':process,'newer_edit_preserved':not unedited,
                                   'no_edit_between_recovery_and_late_request':unedited,'pending_absent':True})
        print('PASS '+selected,flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'real_s3':True,'real_http_delayed':True,
            'database':'postgresql' if database_url else 'sqlite'}
    (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'evidence':str(report)}),flush=True)
    return report,result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+')
    parser.add_argument('--postgres-only',action='store_true')
    parser.add_argument('--adapter-only',action='store_true')
    args=parser.parse_args()
    with ObjectStore(executable=RUNTIME,volume_max=12) as store:
        checks=adapter_checks(store)
        (store.root/'adapter-checks.json').write_text(json.dumps({'passed':True,'count':len(checks),'checks':checks}),encoding='utf8')
        print('ADAPTER_OK '+str(store.root/'adapter-checks.json'),flush=True)
        if args.adapter_only:return
        if not args.postgres_only:await asyncio.to_thread(run,store,None,args.modes)
        if not (args.postgres or args.postgres_only):return
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_live_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            logs=Path(tempfile.mkdtemp(prefix='live-migration-',dir=SERVER.parents[1]/'tmp'))
            await asyncio.to_thread(alembic,temporary,logs,'upgrade','head')
            report,result=await asyncio.to_thread(run,store,temporary,args.modes)
        finally:
            if created:await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        result['temporary_database_removed']=True
        (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
