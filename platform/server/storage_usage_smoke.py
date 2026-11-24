"""Real FS/S3 retained-byte quotas, pagination, native versions and I/O liveness."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

from testsupport import IsolatedServer,SERVER
from s3testsupport import ObjectStore

RUNTIME=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'

FIXTURE='''
import asyncio,threading,time
from pathlib import Path
from fastapi import Depends
from app.main import app
from app.db import get_db
from app.deps import get_current_user
from app.services import quota,storage_usage
from app.config import get_settings
root=Path(__file__).parent
started=threading.Event();active=0;maximum=0;completed=0
async def quotas(db,user):return {**quota.DEFAULT_QUOTAS,'storage_mb':1}
quota.quotas_for=quotas
measure=storage_usage._measure
def measuring(*args,**kwargs):
 global active,maximum,completed
 active+=1;maximum=max(maximum,active);started.set()
 try:
  if (root/'mode').read_text()=='slow':time.sleep(2)
  return measure(*args,**kwargs)
 finally:active-=1;completed+=1
storage_usage._measure=measuring
raw=storage_usage._raw_client
def raw_client(*args):
 if (root/'mode').read_text()=='backend_error':raise OSError('injected unavailable inventory')
 return raw(*args)
storage_usage._raw_client=raw_client
@app.get('/api/v1/fixture/usage')
async def usage(db=Depends(get_db),user=Depends(get_current_user)):
 return await quota.usage(db,user)
@app.post('/api/v1/fixture/storage-check')
async def check(body:dict,db=Depends(get_db),user=Depends(get_current_user)):
 await quota.check_storage_quota(db,user,body['bytes'])
 return {'allowed':True}
@app.post('/api/v1/fixture/limits')
async def limits(body:dict,user=Depends(get_current_user)):
 get_settings().storage_measurement_max_entries=body.get('entries',1000000)
 get_settings().storage_measurement_timeout_seconds=body.get('seconds',30)
 return {'ok':True}
@app.get('/api/v1/fixture/ping')
async def ping():return {'active':active,'maximum':maximum}
@app.post('/api/v1/fixture/cancel')
async def cancel(user=Depends(get_current_user)):
 global maximum,completed
 assert active==0;maximum=0;completed=0;started.clear()
 operation=asyncio.create_task(storage_usage.measure_user(user.id))
 while not started.is_set():await asyncio.sleep(.01)
 operation.cancel()
 replacement=asyncio.create_task(storage_usage.measure_user(user.id))
 try:await operation
 except asyncio.CancelledError:cancelled=True
 else:cancelled=False
 await replacement
 return {'cancelled':cancelled,'maximum':maximum,'completed':completed,'active':active}
'''


def register(api):
    row=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
        'password':'isolated-test-123','name':'Storage accounting'})
    api.token=row['token'];return row['user']['id']


def environment(api,settings):
    settings={**settings,'VIBEFLOW_STORAGE_MEASUREMENT_CONCURRENCY':'1','VIBEFLOW_STORAGE_MEASUREMENT_PAGE_SIZE':'2'}
    (api.root/'usage_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')
    (api.root/'mode').write_text('normal')


def common(api,expected,checks):
    measured=api.call('GET','/fixture/usage');assert measured['storage_bytes_used']==expected,measured
    api.call('POST','/fixture/storage-check',{'bytes':0},expect=429)
    api.call('POST','/fixture/storage-check',{'bytes':-1},expect=422)
    api.call('POST','/fixture/storage-check',{'bytes':True},expect=422)
    checks.append('retained_bytes_and_integer_admission_are_enforced')
    api.call('POST','/fixture/limits',{'entries':1})
    api.call('GET','/fixture/usage',expect=503)
    api.call('POST','/fixture/limits',{})
    checks.append('incomplete_inventory_does_not_become_zero_usage')
    (api.root/'mode').write_text('slow')
    with ThreadPoolExecutor(max_workers=1) as pool:
        operation=pool.submit(api.call,'GET','/fixture/usage')
        deadline=time.monotonic()+5
        while api.call('GET','/fixture/ping')['active']==0:
            assert time.monotonic()<deadline;time.sleep(.02)
        start=time.monotonic();api.call('GET','/fixture/ping');elapsed=time.monotonic()-start
        assert elapsed<.5 and not operation.done(),elapsed
        assert operation.result(timeout=10)['storage_bytes_used']==expected
    checks.append('slow_inventory_keeps_event_loop_responsive')
    start=time.monotonic();cancelled=api.call('POST','/fixture/cancel',{})
    assert cancelled=={'cancelled':True,'maximum':1,'completed':2,'active':0},cancelled
    assert time.monotonic()-start>=3.8
    checks.append('cancelled_scan_drains_before_releasing_its_slot')
    api.call('POST','/fixture/limits',{'seconds':.1})
    api.call('GET','/fixture/usage',expect=503)
    api.call('POST','/fixture/limits',{})
    (api.root/'mode').write_text('normal')
    checks.append('deadline_failure_is_explicit_and_capacity_recovers')


def verify_fs(url=None):
    api=IsolatedServer('storage-usage-fs',app='usage_fixture:app',database_url=url)
    environment(api,{})
    checks=[]
    with api:
        uid=register(api);base=api.root/f'files/workspaces/{uid}'
        paths={'cases/1/.vibeflow/attempts/retained/run/output.bin':2*1024*1024,
            'cases/1/.vibeflow/publications/old/files/report.html':1024,'cases/2/.deleted/kept.bin':2048}
        for name,size in paths.items():
            target=base/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'x'*size)
        neighbor=api.root/f'files/workspaces/{uid}0/foreign.bin';neighbor.parent.mkdir(parents=True);neighbor.write_bytes(b'f'*8192)
        expected=sum(paths.values());common(api,expected,checks)
        checks.append('local_history_working_copies_and_retained_files_stay_charged_without_neighbor_leak')
        import subprocess
        link=base/'foreign-junction'
        result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(neighbor.parent)],capture_output=True)
        assert result.returncode==0
        try:api.call('GET','/fixture/usage',expect=503)
        finally:link.rmdir()  # Remove this junction only, never its target tree.
        assert neighbor.read_bytes()==b'f'*8192
        checks.append('junction_refused_without_following_or_removing_foreign_data')
        for name in paths:(base/name).unlink()
        assert api.call('GET','/fixture/usage')['storage_bytes_used']==0
        api.call('POST','/fixture/storage-check',{'bytes':1024*1024})
        api.call('POST','/fixture/storage-check',{'bytes':1024*1024+1},expect=429)
        checks.append('storage_limit_accepts_exact_bytes_and_rejects_one_more')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (api.root/'storage-usage-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


def verify_s3(store,url=None):
    api=IsolatedServer('storage-usage-s3',app='usage_fixture:app',database_url=url)
    environment(api,store.settings());checks=[]
    with api:
        uid=register(api);prefix=f'workspaces/{uid}/';raw=store.raw_client;args={'Bucket':store.bucket}
        key=prefix+'cases/1/.vibeflow/attempts/retained/run/output.bin'
        raw.put_object(**args,Key=key,Body=b'x'*(2*1024*1024))
        raw.put_object(**args,Key=f'workspaces/{uid}0/foreign.bin',Body=b'f'*8192)
        for number in range(5):raw.put_object(**args,Key=prefix+f'cases/1/.vibeflow/publications/old{number}/report.html',Body=b'h'*17)
        logical=prefix+'cases/1/live.txt'
        store.client.put_object(**args,Key=logical,Body=b'first version')
        store.client.put_object(**args,Key=logical,Body=b'new')
        physical=[row for page in raw.get_paginator('list_objects_v2').paginate(**args,Prefix=prefix) for row in page.get('Contents',[])]
        expected=sum(row['Size'] for row in physical)
        logical_size=sum(row['Size'] for page in store.client.get_paginator('list_objects_v2').paginate(**args,Prefix=prefix) for row in page.get('Contents',[]))
        assert expected>logical_size
        common(api,expected,checks)
        checks.append('physical_paginated_objects_include_hidden_live_history_and_exclude_neighbor')
        (api.root/'mode').write_text('backend_error')
        api.call('GET','/fixture/usage',expect=503)
        api.call('POST','/fixture/storage-check',{'bytes':0},expect=503)
        (api.root/'mode').write_text('normal')
        checks.append('backend_failure_never_falls_back_to_local_cache_or_zero')
        raw.put_bucket_versioning(**args,VersioningConfiguration={'Status':'Enabled'})
        versioned=prefix+'native-history.bin'
        first=raw.put_object(**args,Key=versioned,Body=b'a'*7)
        second=raw.put_object(**args,Key=versioned,Body=b'b'*11)
        assert first['VersionId']!=second['VersionId']
        raw.delete_object(**args,Key=versioned)
        assert api.call('GET','/fixture/usage')['storage_bytes_used']==expected+18
        checks.append('native_bucket_versions_remain_charged_after_delete_marker')
        raw.put_bucket_versioning(**args,VersioningConfiguration={'Status':'Suspended'})
        raw.put_object(**args,Key=versioned,Body=b'c'*13)
        assert api.call('GET','/fixture/usage')['storage_bytes_used']==expected+31
        checks.append('suspended_versioning_counts_previous_and_null_versions')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (api.root/'storage-usage-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='storage-usage-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite_fs':await asyncio.to_thread(verify_fs)}
    with ObjectStore(executable=RUNTIME) as store:results['sqlite_s3']=await asyncio.to_thread(verify_s3,store)
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_storageusage_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_storageusage_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql_fs']=await asyncio.to_thread(verify_fs,target)
            with ObjectStore(executable=RUNTIME) as store:results['postgresql_s3']=await asyncio.to_thread(verify_s3,store,target)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('STORAGE_USAGE_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items() if isinstance(v,dict)}}))


if __name__=='__main__':asyncio.run(main())
