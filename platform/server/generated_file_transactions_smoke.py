"""Actual UDF scaffold and batch publication, API death and fresh-reader recovery."""
import argparse
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import uuid

import httpx

from file_transactions_smoke import FIXTURE as LOCAL_FIXTURE, interrupt
from s3_file_transactions_smoke import FIXTURE as REMOTE_FIXTURE
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME
from testsupport import IsolatedServer, SERVER

EXTRA = '''
from app.services import batch_jobs,job_delivery
async def no_dispatch(*args,**kwargs):pass
batch_jobs.start_batch=no_dispatch
job_delivery.deliver=no_dispatch
@app.get('/fixture/prefix/{case_id}')
async def fixture_prefix(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
    case=await get_owned_case(case_id,user,db)
    return {'prefix':case.s3_prefix}
'''


def run(store=None, database_url=None):
    checks, runs = [], []
    settings = store.settings() if store else {}
    if store and database_url is None:
        settings['VIBEFLOW_DATABASE_URL']='sqlite+aiosqlite:///'+(store.root/'generated.sqlite').as_posix()
    fixture = (REMOTE_FIXTURE if store else LOCAL_FIXTURE) + EXTRA
    for kind in ('udf','batch'):
        for boundary in ('partial','before_commit','after_commit'):
            api = IsolatedServer('generated-'+kind+'-'+boundary, app='generated_fixture:app', database_url=database_url)
            (api.root/'generated_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+fixture,encoding='utf8')
            with api:
                case=api.create_case('Generated transaction '+kind+' '+boundary)
                prefix=api.call('GET','/fixture/prefix/'+str(case['id']),api_prefix=False)['prefix']
                api.put('fault.txt','preserved user source')
                identifier=uuid.uuid4().hex
                payload={'name':'transactional'} if kind=='udf' else {'commands':['echo FIRST','echo SECOND'],'max_concurrent':1}
                endpoint=api.prefix+('/udf/scaffold' if kind=='udf' else '/jobs:batch')
                mode=('after_one' if store else 'after_apply') if boundary=='partial' else boundary
                (api.root/'mode').write_text(mode)
                headers={'Authorization':'Bearer '+api.token,'Idempotency-Key':identifier}
                with httpx.Client(base_url=api.base,headers=headers,trust_env=False,timeout=60) as client:
                    interrupt(api,lambda:client.post('/api/v1'+endpoint,json=payload))

                peer=IsolatedServer('generated-reader',app='generated_reader:app',database_url=api.database_url)
                overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET')}
                overrides.update(settings)
                if not store:
                    overrides.update({key:api.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_INDEX_ROOT')})
                (peer.root/'generated_reader.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+fixture,encoding='utf8')
                peer.token=api.token
                with peer:
                    diagnosis=peer.call('GET',api.prefix+'/file-transactions')
                    assert diagnosis['status']=='pending', diagnosis
                    committed=boundary=='after_commit'
                    assert diagnosis['database_committed']==committed, diagnosis
                    assert peer.call('GET',api.prefix+'/raw?path=fault.txt',raw=True)==b'preserved user source'
                    assert peer.call('GET',api.prefix+'/file-transactions')['status']=='idle'
                    entries=peer.call('GET',api.prefix+'/tree')['entries']
                    if kind=='udf':
                        selected=[row['path'] for row in entries if row['path'].startswith('UDF/transactional/')]
                        assert len(selected)==(9 if committed else 0), selected
                        if committed:
                            content=peer.call('GET',api.prefix+'/raw?path=UDF/transactional/myFunctionObject.C',raw=True)
                            assert b'P0_UDF_OK' in content
                            peer.call('POST',endpoint,payload,expect=409)
                            assert peer.call('GET',api.prefix+'/raw?path=UDF/transactional/myFunctionObject.C',raw=True)==content
                        else:
                            created=peer.call('POST',endpoint,payload)
                            assert len(created['files'])==9
                    else:
                        jobs=peer.call('GET',api.prefix+'/jobs')
                        batches=peer.call('GET',api.prefix+'/job-batches')
                        if committed:
                            assert len(jobs)==2 and len(batches)==1, (jobs,batches)
                            saved=batches[0]
                            assert saved['total']==2 and {r['id'] for r in saved['jobs']}=={r['id'] for r in jobs}
                            replay=peer.call('POST',endpoint,payload,expect=202,extra_headers={'Idempotency-Key':identifier})
                            assert replay['id']==saved['id'] and len(peer.call('GET',api.prefix+'/jobs'))==2
                        else:
                            assert jobs==[] and batches==[], (jobs,batches)
                            assert not any(row['path'].startswith('.vibeflow/job-batches/') for row in entries)
                            created=peer.call('POST',endpoint,payload,expect=202,extra_headers={'Idempotency-Key':identifier})
                            assert created['total']==2 and len(peer.call('GET',api.prefix+'/jobs'))==2
                    assert peer.call('GET',api.prefix+'/file-transactions')['status']=='idle'
                    assert peer.call('GET',api.prefix+'/raw?path=fault.txt',raw=True)==b'preserved user source'
                checks.append(kind+'_'+boundary)
                runs.append({'mode':checks[-1],'api':str(api.root),'reader':str(peer.root),'prefix':prefix})
                print('PASS '+checks[-1],flush=True)
    return {'checks':checks,'count':len(checks),'runs':runs,'storage':'s3' if store else 'fs'}


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--s3',action='store_true')
    parser.add_argument('--postgres-only',action='store_true')
    args=parser.parse_args()
    output=Path(tempfile.mkdtemp(prefix='generated-transactions-evidence-',dir=SERVER.parents[1]/'tmp'))
    with ObjectStore(executable=RUNTIME) if args.s3 else nullcontext() as store:
        if args.postgres_only:
            import asyncpg
            from sqlalchemy.engine import make_url
            from app.config import Settings
            from workflow_migration_smoke import alembic
            pg=make_url(Settings(_env_file=SERVER/'.env').database_url)
            assert pg.drivername=='postgresql+asyncpg' and pg.host in {'127.0.0.1','localhost','::1'}
            name='tmp_vf_generated_'+uuid.uuid4().hex
            admin=await asyncpg.connect(host=pg.host,port=pg.port or 5432,user=pg.username,password=pg.password,database='postgres',timeout=10)
            created=False
            try:
                await admin.execute('CREATE DATABASE "'+name+'"');created=True
                url=pg.set(database=name).render_as_string(hide_password=False)
                await asyncio.to_thread(alembic,url,output,'upgrade','head')
                result=await asyncio.to_thread(run,store,url)
            finally:
                if created:
                    assert await admin.fetchval('SELECT current_database()')=='postgres'
                    await admin.execute('DROP DATABASE "'+name+'"')
                await admin.close()
            result.update(database='postgresql',temporary_database_removed=True)
        else:
            result=await asyncio.to_thread(run,store)
            result['database']='sqlite'
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'status':'passed','count':result['count'],'evidence':str(output)}),flush=True)


if __name__=='__main__':asyncio.run(main())
