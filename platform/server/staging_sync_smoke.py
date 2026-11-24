"""Real S3 staging transactions, crash recovery without the originating cache."""
import argparse, asyncio, json, tempfile, uuid
from pathlib import Path
from testsupport import IsolatedServer, SERVER
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME
from s3_file_transactions_smoke import FIXTURE, get
from case_provision_smoke import crash

STAGING = '''
from app.services import staging_sync
@app.get('/fixture/storage-owner/{case_id}')
async def storage_owner(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
 import json
 from app.services.storage_operations import active
 await get_owned_case(case_id,user,db)
 row=await active(db,case_id)
 return {'process':json.loads(row.owner_json)}
@app.post('/fixture/legacy-worker/{case_id}')
async def legacy_worker(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
 import sys
 from app.models import Job
 from app.services import jobrunner
 case=await get_owned_case(case_id,user,db)
 job=Job(case_id=case.id,type='shell',command='fixture legacy')
 db.add(job);await db.flush();job.log_s3_key=f'.vibeflow/jobs/job-{job.id}.log';await db.commit()
 proc=await jobrunner.start_job(job.id,job.command,case.s3_prefix,
  argv=[sys.executable,'-c',"from pathlib import Path; Path('legacy-output.txt').write_text('REAL STAGING')"])
 status=await proc.wait()
 return {'status':status,'stage':proc.staging_prefix,'isolated':proc.isolated_workspace}
@app.post('/fixture/stage/{case_id}')
async def stage(case_id:int,body:dict,db=Depends(get_db),user=Depends(get_current_user)):
 case=await get_owned_case(case_id,user,db)
 async with coordinate(db,case):
  selected=await journal._io(storage.prepare_job_staging,case.s3_prefix,case_id)
  for path,data in body.items():
   if data is None:storage._fs_delete(selected,path)
   else:storage._fs_put(selected,path,data.encode())
 await db.commit()
 return {'stage':selected,'prefix':case.s3_prefix}
@app.post('/fixture/sync/{case_id}')
async def sync_stage(case_id:int,body:dict,db=Depends(get_db),user=Depends(get_current_user)):
 case=await get_owned_case(case_id,user,db)
 if mode()=='scaled-size':journal.journal.MAX_BYTES=8;journal.MAX_TRANSACTION_BYTES=16
 async with coordinate(db,case):result=await staging_sync.sync(db,case,body['stage'])
 await db.commit()
 return result
@app.post('/fixture/recover/{case_id}')
async def recover_stage(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
 from app.routers.files import recover_file_transactions
 await recover_file_transactions(case_id,user)
 return {'ok':True}
'''


def boot(settings,database_url=None):
    api=IsolatedServer('staging-sync',app='staging_fixture:app',database_url=database_url)
    (api.root/'staging_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE+STAGING,encoding='utf8')
    return api


def run(store,database_url=None,modes=None):
    output=Path(tempfile.mkdtemp(prefix='staging-sync-evidence-',dir=SERVER.parents[1]/'tmp'))
    settings=store.settings()
    if not database_url:settings['VIBEFLOW_DATABASE_URL']='sqlite+aiosqlite:///'+(output/'test.sqlite').as_posix()
    checks=[]
    for mode in modes or ['normal','after_one','before_commit','after_commit','write_race','response_lost','capacity']:
        with boot(settings,database_url) as api:
            case=api.create_case('Staging '+mode)
            if mode=='legacy-worker':
                result=api.call('POST',f'/fixture/legacy-worker/{case["id"]}',api_prefix=False)
                assert result['status']=='succeeded' and result['stage'] and not result['isolated'],result
                assert api.call('GET',api.prefix+'/raw?path=legacy-output.txt',raw=True)==b'REAL STAGING'
                assert not (api.root/'files'/result['stage']).exists()
                checks.append({'mode':mode,'api':str(api.root)})
                print('PASS '+mode,flush=True)
                continue
            api.put('fault.txt','old');api.put('remove.txt','remove')
            body={'fault.txt':'new','new.txt':'created','remove.txt':None}
            if mode=='capacity':body={f'bulk/{i:04}.txt':'bulk' for i in range(300)}
            if mode=='scaled-size':body['new.txt']='x'*1024
            chosen=api.call('POST',f'/fixture/stage/{case["id"]}',body,api_prefix=False)
            if mode=='restored-version':
                manifest_path=api.root/'files'/(chosen['stage']+'.manifest.json')
                manifest=json.loads(manifest_path.read_text())
                manifest['files']['fault.txt']['etag']='earlier-multipart-version-2'
                manifest_path.write_text(json.dumps(manifest))
            prefix=chosen['prefix']
            def read(path):return get(store,store.bucket,prefix+'/'+path)
            (api.root/'mode').write_text(mode)
            request=lambda:api.call('POST',f'/fixture/sync/{case["id"]}',chosen,api_prefix=False,
                                    expect=409 if mode=='write_race' else 500 if mode=='response_lost' else 200,
                                    raw=mode in {'write_race','response_lost'})
            if mode in {'after_one','before_commit','after_commit'}:crash(api,request)
            else:request()
            shared={**settings,'VIBEFLOW_DATABASE_URL':database_url or settings['VIBEFLOW_DATABASE_URL'],
                    'VIBEFLOW_JWT_SECRET':api.env['VIBEFLOW_JWT_SECRET']}
            with boot(shared,database_url) as peer:
                peer.token=api.token
                if mode=='write_race':
                    assert read('fault.txt')==b'external edit'
                    peer.call('POST',f'/fixture/recover/{case["id"]}',api_prefix=False,expect=409)
                    assert read('fault.txt')==b'external edit' and read('new.txt') is None
                else:
                    peer.call('POST',f'/fixture/recover/{case["id"]}',api_prefix=False)
                    committed=mode in {'normal','after_commit','capacity','scaled-size','restored-version'}
                    assert read('fault.txt')==(b'new' if committed and mode!='capacity' else b'old')
                    if mode!='capacity':
                        assert read('new.txt')==((b'x'*1024 if mode=='scaled-size' else b'created') if committed else None)
                        assert read('remove.txt')==(None if committed else b'remove')
                    else:
                        assert read('bulk/0000.txt')==read('bulk/0299.txt')==b'bulk'
                    assert read('.vibeflow/file-transactions/pending.json') is None
                    peer.call('POST',f'/fixture/recover/{case["id"]}',api_prefix=False)
            assert (api.root/'files'/chosen['stage']).exists()
            checks.append({'mode':mode,'api':str(api.root),'peer':str(peer.root)})
        print('PASS '+mode,flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'database':'postgresql' if database_url else 'sqlite'}
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'evidence':str(output)}),flush=True)
    return output,result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+');args=parser.parse_args()
    with ObjectStore(executable=RUNTIME,volume_max=12) as store:
        if not args.postgres:
            await asyncio.to_thread(run,store,None,args.modes);return
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_stagesync_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            logs=Path(tempfile.mkdtemp(prefix='stagesync-migration-',dir=SERVER.parents[1]/'tmp'))
            await asyncio.to_thread(alembic,temporary,logs,'upgrade','head')
            output,result=await asyncio.to_thread(run,store,temporary,args.modes)
        finally:
            if created:
                await admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1',name)
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        result['temporary_database_removed']=True
        (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
