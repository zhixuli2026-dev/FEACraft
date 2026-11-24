"""Real S3 upload chunks, fresh-cache resumption and completion recovery."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import uuid
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from testsupport import IsolatedServer, SERVER
from s3testsupport import ObjectStore,LEGACY_CASE_FIXTURE,create_legacy_case
from s3_typed_publication_smoke import RUNTIME
from s3_file_transactions_smoke import FIXTURE as JOURNAL, get
from file_transactions_smoke import interrupt, wait_ready

FIXTURE=JOURNAL+'''
from app.services import s3_uploads as uploads
save_meta=uploads._save_meta
def saving(prefix,meta,etag):
    if meta['offset'] and mode()=='chunk_before_meta':hold()
    result=save_meta(prefix,meta,etag)
    if meta['offset'] and mode()=='chunk_response_lost':
        raise TimeoutError('fixture remote chunk metadata response lost')
    return result
uploads._save_meta=saving
cleanup=uploads.cleanup_completed
def cleaning(*args):
    if mode()=='cleanup_crash':hold()
    if mode()=='cleanup_failure':raise OSError('fixture S3 cleanup unavailable')
    return cleanup(*args)
uploads.cleanup_completed=cleaning
'''
DATA=bytes(range(256))*8192
DIGEST=hashlib.sha256(DATA).hexdigest()
SPLIT=1024*1024
MODES=('resume','chunk_before_meta','chunk_response_lost','before_commit','after_commit',
       'after_one','cleanup_crash','cleanup_failure','corrupt_chunk','target_conflict','cancel',
       'complete_cancel','cleanup_foreign')


def boot(settings,label,database_url=None,legacy=False):
    api=IsolatedServer(label,app='remote_upload:app',database_url=database_url)
    (api.root/'remote_upload.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE+(LEGACY_CASE_FIXTURE if legacy else ''),encoding='utf8')
    return api


def send(api,endpoint,offset,data,expected=200):
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=45) as client:
        response=client.put(endpoint+'?offset='+str(offset),content=data)
        assert response.status_code==expected,(response.status_code,response.text[:1500])
        return response.json() if expected!=500 else response.content


def verify(store,database_url=None,modes=None):
    output=Path(tempfile.mkdtemp(prefix='s3-upload-transactions-evidence-',dir=SERVER.parents[1]/'tmp'))
    settings=store.settings()
    if not database_url:
        settings['VIBEFLOW_DATABASE_URL']='sqlite+aiosqlite:///'+(output/'test.sqlite').as_posix()
    checks=[]
    for mode in modes or MODES:
        with boot(settings,'s3-upload-'+mode,database_url) as api:
            case=api.create_case('Remote upload '+mode)
            api.put('Ref/upload.bin','old upload')
            keys=[item['Key'] for page in store.client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket) for item in page.get('Contents',[])]
            source=next(key for key in keys if key.endswith('/cases/'+str(case['id'])+'/Ref/upload.bin'))
            prefix=source.removesuffix('/Ref/upload.bin')
            session=api.call('POST',api.prefix+'/uploads',{'path':'Ref/upload.bin','size':len(DATA),'sha256':DIGEST,'overwrite':True})
            endpoint=api.prefix+'/uploads/'+session['id']
            directory=prefix+'/.vibeflow/uploads/'+session['id']
            if mode.startswith('chunk_'):
                (api.root/'mode').write_text(mode)
                if mode=='chunk_before_meta':interrupt(api,lambda:send(api,endpoint,0,DATA[:SPLIT]))
                else:send(api,endpoint,0,DATA[:SPLIT],500)
            else:
                send(api,endpoint,0,DATA[:SPLIT])
            overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET')}
            overrides.update(settings)
            fresh=boot(overrides,'s3-upload-fresh',database_url)
            assert not (fresh.root/'files').exists()
            with fresh:
                fresh.token=api.token
                status=fresh.call('GET',endpoint)
                if mode=='chunk_before_meta':
                    assert status['offset']==0
                    send(fresh,endpoint,0,b'different bytes',409)
                    send(fresh,endpoint,0,DATA[:SPLIT])
                else:assert status['offset']==SPLIT,status
                send(fresh,endpoint,0,DATA[:SPLIT],409)
                send(fresh,endpoint,SPLIT,DATA[SPLIT:])
                assert fresh.call('GET',endpoint)['offset']==len(DATA)
                if mode=='complete_cancel':
                    (fresh.root/'mode').write_text('cancel_write')
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        writing=pool.submit(fresh.call,'POST',endpoint+':complete',expect=500,raw=True)
                        try:
                            wait_ready(fresh)
                            competing=pool.submit(api.call,'PUT',api.prefix+'/file',{'path':'Ref/upload.bin','content':'NEWER'},expect=(200,409))
                            time.sleep(.25)
                            assert not writing.done()
                            if database_url:
                                # Owner quota admission refuses reverse-lock
                                # waiting in PostgreSQL; retry only after drain.
                                rejected=competing.result(timeout=5)
                                assert rejected['detail']['code']=='storage_quota_busy',rejected
                                assert get(store,store.bucket,source)==b'old upload'
                            else:assert not competing.done()
                            assert fresh.call('GET','/api/health',api_prefix=False)['ok']
                        finally:(fresh.root/'release').touch()
                        writing.result(timeout=30)
                        competing.result(timeout=30)
                    if database_url:api.call('PUT',api.prefix+'/file',{'path':'Ref/upload.bin','content':'NEWER'})
                    assert get(store,store.bucket,source)==b'NEWER'
                    assert api.call('GET',endpoint)['status']=='uploading'
                    api.call('POST',endpoint+':complete',expect=409)
                elif mode=='cancel':
                    fresh.call('DELETE',endpoint)
                    fresh.call('GET',endpoint,expect=404)
                    send(fresh,endpoint,len(DATA),b'x',409)
                    assert get(store,store.bucket,source)==b'old upload'
                    objects=store.client.list_objects_v2(Bucket=store.bucket,Prefix=directory+'/chunks/').get('Contents',[])
                    assert objects==[]
                elif mode=='corrupt_chunk':
                    store.client.put_object(Bucket=store.bucket,Key=directory+'/chunks/0000000000/data',Body=b'corrupt')
                    fresh.call('POST',endpoint+':complete',expect=409)
                    assert get(store,store.bucket,source)==b'old upload'
                elif mode=='target_conflict':
                    fresh.call('PUT',api.prefix+'/file',{'path':'Ref/upload.bin','content':'external edit'})
                    fresh.call('POST',endpoint+':complete',expect=409)
                    assert get(store,store.bucket,source)==b'external edit'
                else:
                    if mode in {'before_commit','after_commit','after_one','cleanup_crash','cleanup_foreign'}:
                        (fresh.root/'mode').write_text('cleanup_crash' if mode=='cleanup_foreign' else mode)
                        interrupt(fresh,lambda:fresh.call('POST',endpoint+':complete'))
                        if mode=='cleanup_foreign':
                            store.client.put_object(Bucket=store.bucket,Key=directory+'/chunks/0000000000/data',Body=b'FOREIGN CHUNK')
                    else:
                        (fresh.root/'mode').write_text(mode)
                        result=fresh.call('POST',endpoint+':complete')
                        assert result['sha256']==DIGEST
                        if mode=='cleanup_failure':assert result['cleanup_pending']
                    with boot(overrides,'s3-upload-recovered',database_url) as recovered:
                        recovered.token=api.token
                        committed=mode not in {'before_commit','after_one'}
                        status=recovered.call('GET',endpoint)
                        assert (status.get('status')=='completed')==committed,(mode,status)
                        assert recovered.call('GET',api.prefix+'/raw?path=Ref/upload.bin',raw=True)==(DATA if committed else b'old upload')
                        done=recovered.call('POST',endpoint+':complete')
                        assert done['sha256']==DIGEST,done
                        assert recovered.call('GET',api.prefix+'/raw?path=Ref/upload.bin',raw=True)==DATA
                        if mode=='cleanup_foreign':
                            assert done['cleanup_pending']
                            assert get(store,store.bucket,directory+'/chunks/0000000000/data')==b'FOREIGN CHUNK'
                        else:
                            assert not done.get('cleanup_pending'),done
                            assert get(store,store.bucket,directory+'/meta.json') is None
                            assert not store.client.list_objects_v2(Bucket=store.bucket,Prefix=directory+'/chunks/').get('Contents')
                        recovered.call('PUT',api.prefix+'/file',{'path':'Ref/upload.bin','content':'later edit'})
                        assert recovered.call('POST',endpoint+':complete')==done
                        assert get(store,store.bucket,source)==b'later edit'
                        recovered.call('DELETE',endpoint,expect=409)
                        send(recovered,endpoint,0,b'x',409)
                        assert recovered.call('GET',api.prefix+'/file-transactions')['status']=='idle'
            checks.append({'mode':mode,'api':str(api.root),'fresh':str(fresh.root)})
            (output/'checks.json').write_text(json.dumps({'count':len(checks),'checks':checks,'complete':False},indent=2),encoding='utf8')
            print('PASS '+mode,flush=True)
    result={'count':len(checks),'checks':checks,'complete':True,'database':'postgresql' if database_url else 'sqlite','store':str(store.root)}
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'status':'passed','count':len(checks),'evidence':str(output)}),flush=True)
    return output,result


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--legacy-only',action='store_true')
    parser.add_argument('--modes',nargs='+',choices=MODES)
    args=parser.parse_args()
    if args.legacy_only:
        with ObjectStore() as store:
            with boot(store.settings(),'s3-upload-legacy',legacy=True) as api:
                create_legacy_case(api,'Legacy remote upload capability')
                api.call('POST',api.prefix+'/uploads',{'path':'Ref/never.txt','size':5},expect=422)
                api.call('GET',api.prefix+'/raw?path=Ref/never.txt',expect=404)
                keys=[item['Key'] for page in store.client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket) for item in page.get('Contents',[])]
                assert not any('/.vibeflow/uploads/' in key for key in keys)
            result={'count':1,'passed':True,'api':str(api.root),'check':'incompatible_backend_rejected_before_upload_creation'}
            (store.root/'upload-legacy-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
            print(json.dumps({'status':'passed','count':1,'evidence':str(store.root)}),flush=True)
        return
    with ObjectStore(executable=RUNTIME,volume_max=24) as store:
        if not args.postgres:
            await asyncio.to_thread(verify,store,None,args.modes)
            return
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_remoteupload_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            logs=Path(tempfile.mkdtemp(prefix='remote-upload-migration-',dir=SERVER.parents[1]/'tmp'))
            await asyncio.to_thread(alembic,temporary,logs,'upgrade','head')
            output,result=await asyncio.to_thread(verify,store,temporary,args.modes)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        result['temporary_database_removed']=True
        (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
