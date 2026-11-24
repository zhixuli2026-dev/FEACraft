"""Real HTTP sealing rejects metadata overflow before allocating FS/S3 inputs."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import uuid
from testsupport import IsolatedServer,SERVER
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME

FIXTURE='''
import json,os,time
from pathlib import Path
from fastapi import Depends
from app.main import app
from app.db import get_db
from app.config import get_settings
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app import storage
from app.services import quota,storage_usage,attempt_seal_journal as seals,private_cache,attempt_objects as objects,jobrunner
from app.services.case_write import coordinate
root=Path(__file__).parent
current_user=None
def mode():return (root/'mode').read_text()
def record(kind,**values):
 if mode()!='idle':
  with (root/'trace.jsonl').open('a',encoding='utf8') as out:out.write(json.dumps({'kind':kind,**values})+'\\n')
def sample(kind):
 if mode()=='monitor' and current_user is not None:
  value=storage_usage._measure(get_settings(),current_user,time.monotonic()+30,None)
  record('sample',phase=kind,bytes=value.bytes)
async def quotas(db,user):return {**quota.DEFAULT_QUOTAS,'storage_mb':int((root/'limit').read_text())}
quota.quotas_for=quotas
check=quota.check_storage_quota
async def checked(db,user,incoming):
 global current_user
 current_user=user.id
 before=await storage_usage.measure_user(user.id)
 record('admission',before=before.bytes,incoming=incoming)
 await check(db,user,incoming)
 if mode()=='change_source':
  prefix=(root/'prefix').read_text()
  storage.put_text(prefix,'Ref/input.txt','changed during admission')
quota.check_storage_quota=checked
async def no_launch(*args,**kwargs):return None
jobrunner.start_job=no_launch  # This suite checks sealing, not execution.
witness=seals.witness
async def witnessed(db,case,attempt,plan):
 result=await witness(db,case,attempt,plan)
 sample('seal_witness')
 (root/'sealed.json').write_text(json.dumps(plan['manifest']),encoding='utf8')
 return result
seals.witness=witnessed
link=os.link
def linked(src,dst,*args,**kwargs):
 result=link(src,dst,*args,**kwargs)
 if '/attempt-provisions/' in str(dst).replace('\\\\','/'):
  sample('history_install' if '/history/' in str(dst).replace('\\\\','/') else 'plan_install')
 return result
os.link=linked
put=objects.put_new
def putting(prefix,path,data):
 result=put(prefix,path,data)
 if path.startswith('.vibeflow/attempt-provisions/') or path.startswith('.vibeflow/attempts/'):
  sample('remote_put')
 return result
objects.put_new=putting
init=private_cache.Cache.__init__
def creating(self,kind):
 if kind=='.attempt-sources':record('cache_created')
 return init(self,kind)
private_cache.Cache.__init__=creating
read=objects.read
def reading(prefix,path,**kwargs):
 if path=='Ref/input.txt' and kwargs.get('etag'):record('source_download')
 return read(prefix,path,**kwargs)
objects.read=reading
@app.get('/api/v1/fixture/usage')
async def usage(user=Depends(get_current_user)):return (await storage_usage.measure_user(user.id)).bytes
@app.get('/api/v1/fixture/prefix/{case_id}')
async def prefix(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
 return (await get_owned_case(case_id,user,db)).s3_prefix
@app.post('/api/v1/fixture/recover/{case_id}')
async def recover(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
 case=await get_owned_case(case_id,user,db)
 async with coordinate(db,case,content_write=False):result=await seals.recover_locked(db,case)
 await db.commit()
 return result
'''


def trace(api):
    path=api.root/'trace.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()] if path.exists() else []


def verify(url=None,store=None):
    api=IsolatedServer('seal-storage-budget',app='seal_storage_fixture:app',database_url=url)
    settings=store.settings() if store else {}
    (api.root/'seal_storage_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')
    (api.root/'limit').write_text('8');(api.root/'mode').write_text('idle')
    checks=[]
    with api:
        case=api.create_case('Seal storage budget');api.put('Ref/input.txt','x'*1024)
        prefix=api.call('GET',f"/fixture/prefix/{case['id']}");(api.root/'prefix').write_text(prefix)
        used=api.call('GET','/fixture/usage')
        size=1024*1024-used-2048;assert size>0
        uid=prefix.split('/')[1]
        if store:
            ballast=f'workspaces/{uid}/ballast.bin'
            store.raw_client.put_object(Bucket=store.bucket,Key=ballast,Body=b'b'*size)
        else:
            ballast=api.root/f'files/workspaces/{uid}/ballast.bin';ballast.write_bytes(b'b'*size)
        assert api.call('GET','/fixture/usage')==1024*1024-2048
        (api.root/'limit').write_text('1');(api.root/'mode').write_text('reject')
        body={'command':'echo input-sealing-only','input_paths':['Ref/input.txt']}
        api.call('POST',api.prefix+'/jobs',body,expect=429)
        assert api.call('GET',api.prefix+'/jobs')==[]
        rows=trace(api);assert rows and rows[0]['incoming']>2048
        assert not any(row['kind'] in {'cache_created','source_download','sample'} for row in rows),rows
        assert not (api.root/'sealed.json').exists()
        checks.append('metadata_and_recovery_peak_rejected_before_any_input_cache_or_seal')
        if store:store.raw_client.delete_object(Bucket=store.bucket,Key=ballast)
        else:ballast.unlink()
        (api.root/'limit').write_text('8');(api.root/'mode').write_text('monitor');(api.root/'trace.jsonl').unlink()
        job=api.call('POST',api.prefix+'/jobs',body)
        assert job['status']=='queued'
        manifest=json.loads((api.root/'sealed.json').read_text(encoding='utf8'))
        import hashlib
        assert manifest['files']['Ref/input.txt']['sha256']==hashlib.sha256(b'x'*1024).hexdigest()
        recovered=api.call('POST',f"/fixture/recover/{case['id']}")
        rows=trace(api);admitted=next(row for row in rows if row['kind']=='admission')
        samples=[row for row in rows if row['kind']=='sample'];assert samples
        for row in samples:assert row['bytes']<=admitted['before']+admitted['incoming'],(admitted,row)
        if not store:assert {'plan_install','seal_witness','history_install'}<={row['phase'] for row in samples}
        history=api.call('POST',f"/fixture/recover/{case['id']}")
        assert history==[]
        checks.append('observed_seal_and_recovery_installation_peaks_stay_inside_admitted_bytes')
        checks.append('real_digest_is_committed_and_recovery_is_idempotent')
        if store:
            assert sum(row['kind']=='cache_created' for row in rows)==1
            assert sum(row['kind']=='source_download' for row in rows)==1
            assert next(i for i,row in enumerate(rows) if row['kind']=='admission')<next(i for i,row in enumerate(rows) if row['kind']=='cache_created')
            (api.root/'mode').write_text('change_source');(api.root/'trace.jsonl').unlink()
            api.call('POST',api.prefix+'/jobs',body,expect=409)
            assert len(api.call('GET',api.prefix+'/jobs'))==1
            assert not any(row['kind'] in {'cache_created','source_download'} for row in trace(api))
            checks.append('S3_version_change_after_admission_is_refused_before_cache_allocation')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (api.root/'seal-storage-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('PASS '+json.dumps(result),flush=True);return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    from contextlib import nullcontext
    report=Path(tempfile.mkdtemp(prefix='seal-storage-evidence-',dir=SERVER.parents[1]/'tmp'))
    with ObjectStore(executable=RUNTIME) if args.s3 else nullcontext() as store:
        results={'sqlite':await asyncio.to_thread(verify,None,store)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_sealstorage_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_sealstorage_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            with ObjectStore(executable=RUNTIME) if args.s3 else nullcontext() as store:
                results['postgresql']=await asyncio.to_thread(verify,target,store)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('SEAL_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
