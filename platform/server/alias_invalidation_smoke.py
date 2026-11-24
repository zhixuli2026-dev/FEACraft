"""Real S3 published-alias edits and SQL snapshot races on disposable APIs."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import uuid

from alias_invalidation_test import FIXTURE
from staging_sync_smoke import boot
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME
from s3_file_transactions_smoke import get
from job_release_smoke import wait_file
from testsupport import SERVER

EXTRA = FIXTURE + '''
from app.services.durable_events import lock_case_execution
from app.db import SessionLocal
@app.post('/api/v1/fixture/alias-sync/{case_id}')
async def alias_sync(case_id:int,body:dict,user=Depends(get_current_user)):
 async def authorize(db):return await get_owned_case(case_id,user,db,write=True)
 return await staging_sync.sync_authorized(body['stage'],body['prefix'],case_id,authorize)
@app.post('/api/v1/fixture/alias-change/{case_id}')
async def alias_change(case_id:int,body:dict,db=Depends(get_db),user=Depends(get_current_user)):
 await get_owned_case(case_id,user,db);await lock_case_execution(db,case_id)
 head=await db.get(ArtifactPublicationHead,(case_id,'pdf-number:alias-fixture'))
 row=await db.get(ArtifactPublication,head.active_publication_id)
 if body['kind']=='head_version':head.version+=1
 elif body['kind']=='row_binding':row.manifest_sha256='0'*64
 elif body['kind']=='attempt_binding':
  attempt=await db.get(JobAttempt,row.attempt_id)
  attempt.publication_json=json_text({'descriptor':{'output':'changed.txt'}})
 elif body['kind']=='new_head':db.add(ArtifactPublicationHead(case_id=case_id,kind='geometry'))
 else:raise ValueError('Unsupported fixture mutation')
 await db.commit();return {'ok':True}
read_alias_manifest=artifact_publication.read_manifest
def reading_alias_manifest(*args):
 if mode() in {'head_version','row_binding','attempt_binding','new_head'}:
  (root/'ready').touch()
  deadline=time.monotonic()+30
  while not (root/'release').exists() and time.monotonic()<deadline:time.sleep(.02)
  if not (root/'release').exists():raise TimeoutError('Alias fixture release')
 return read_alias_manifest(*args)
artifact_publication.read_manifest=reading_alias_manifest
'''


def run(store, database_url=None):
    report = Path(tempfile.mkdtemp(prefix='alias-invalidation-evidence-', dir=SERVER.parents[1]/'tmp'))
    settings = store.settings()
    if not database_url:
        settings['VIBEFLOW_DATABASE_URL'] = 'sqlite+aiosqlite:///' + (report/'test.sqlite').as_posix()
    checks = []
    for selected in ('normal','generated','corrupt','unrelated','head_version','row_binding','attempt_binding','new_head'):
        api = boot(settings, database_url)
        fixture = api.root/'staging_fixture.py'
        fixture.write_text(fixture.read_text(encoding='utf8')+EXTRA, encoding='utf8')
        with api:
            case = api.create_case('Published alias '+selected)
            publication = api.call('POST', f'/fixture/alias/{case["id"]}')
            path = 'generated.txt' if selected=='generated' else 'unrelated.txt' if selected=='unrelated' else 'alias.txt'
            chosen = api.call('POST', f'/fixture/stage/{case["id"]}', {path:'new'}, api_prefix=False)
            if selected=='corrupt':
                store.client.delete_object(Bucket=store.bucket,
                    Key=chosen['prefix']+'/.vibeflow/publications/'+publication['publication_id']+'/manifest.json')
            (api.root/'mode').write_text(selected)
            race = selected in {'head_version','row_binding','attempt_binding','new_head'}
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(api.call, 'POST', f'/fixture/alias-sync/{case["id"]}', chosen,
                                     expect=409 if race else 200)
                if race:
                    wait_file(api.root/'ready')
                    start = time.monotonic()
                    api.call('POST', f'/fixture/alias-change/{case["id"]}', {'kind':selected})
                    elapsed = time.monotonic()-start
                    assert elapsed < 2.5, elapsed
                    (api.root/'release').touch()
                result = future.result(timeout=40)
            state = api.call('GET', f'/fixture/alias/{case["id"]}')
            invalidated = not race and selected!='unrelated'
            assert state['publication_id']==(None if invalidated else publication['publication_id']), state
            read = lambda relative: get(store,store.bucket,chosen['prefix']+'/'+relative)
            assert read('alias.txt')==(b'new' if selected in {'normal','corrupt'} else b'old')
            if selected in {'generated','unrelated'}:assert read(path)==b'new'
            assert read('.vibeflow/file-transactions/pending.json') is None
            if race:
                assert result['detail']['code']=='input_stale', result
                assert (api.root/'files'/chosen['stage']/'alias.txt').read_bytes()==b'new'
            checks.append({'mode':selected,'api':str(api.root),'head':state,'result':result})
        print('PASS '+selected,flush=True)
    result = {'passed':True,'count':len(checks),'checks':checks,
              'database':'postgresql' if database_url else 'sqlite','real_s3':True}
    (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'evidence':str(report)}),flush=True)
    return report,result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    with ObjectStore(executable=RUNTIME,volume_max=12) as store:
        await asyncio.to_thread(run,store)
        if not args.postgres:return
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_alias_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,
                                   password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            logs=Path(tempfile.mkdtemp(prefix='alias-migration-',dir=SERVER.parents[1]/'tmp'))
            await asyncio.to_thread(alembic,temporary,logs,'upgrade','head')
            report,result=await asyncio.to_thread(run,store,temporary)
        finally:
            if created:await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        result['temporary_database_removed']=True
        (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
