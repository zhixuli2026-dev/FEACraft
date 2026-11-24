"""Real S3 read-to-action transactions, native jobs and cancellation; no LLM."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

from testsupport import IsolatedServer, SERVER
from publication_read_capacity_checks import FIXTURE
from professional_launch_smoke import settled as settled_job

OBSERVE = '''
from contextlib import asynccontextmanager
from app.services import case_read_capacity, owner_storage_budget
from app.routers import jobs, postprocess
original_read=case_read_capacity.reading_before_action
@asynccontextmanager
async def observe_read(db,case):
 identifier=case.id
 async with original_read(db,case) as snapshot:
  assert owner_storage_budget.current() is not None
  yield snapshot
 assert owner_storage_budget.current() is None
 assert not db.in_transaction()
 assert case.id==identifier
 with (root/'boundaries.jsonl').open('a',encoding='utf8') as handle:
  handle.write(json.dumps({'case_id':identifier,'transaction_closed':True,'credit_released':True,'orm_retained':True})+'\\n')
jobs.reading_before_action=postprocess.reading_before_action=observe_read
'''


def verify(store, database_url=None, group='batch'):
    api=IsolatedServer('mixed-read-capacity',app='mixed_fixture:app',database_url=database_url)
    store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    (api.root/'mixed_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE+OBSERVE,encoding='utf8')
    result={'passed':False,'llm_calls':0,'database':'postgresql' if database_url else 'sqlite','group':group,'checks':[]}
    def save():
        (api.root/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    def check(name,**evidence):
        result['checks'].append({'name':name,**evidence});save()
        print('MIXED_READ_PASS '+name,flush=True)
    def credit():
        value=api.call('GET','/fixture/credit')
        assert value['rows'] and all(row['state']=='settled' and row['receipt']['closed'] for row in value['rows']),value
    print('MIXED_READ_EVIDENCE '+str(api.root),flush=True)
    try:
        with api:
            api.create_case('Mixed transaction '+group)
            endpoint='/job-batches/'+'0'*32+':cancel' if group=='batch' else '/postprocess:run'
            before=api.call('GET','/fixture/credit?measure=true')['bytes']
            (api.root/'quota-zero').touch()
            try:
                api.call('POST',api.prefix+endpoint,{},expect=429)
                assert api.call('GET','/fixture/credit?measure=true')['bytes']==before
                assert not api.call('GET',api.prefix+'/jobs')
                credit();check('zero_quota_no_remote_growth_or_job')
            finally:(api.root/'quota-zero').unlink()
            if group=='batch':
                api.call('POST',api.prefix+endpoint,{},expect=404)
                credit();check('missing_batch_rollback_settled')
                batch=api.call('POST',api.prefix+'/jobs:batch',{
                    'commands':['"'+sys.executable+'" -c "import time; print(123,flush=True); time.sleep(90)"','echo MUST_NOT_START'],
                    'max_concurrent':1,'type':'shell','runtime':'windows'},expect=202)
                first=batch['jobs'][0]['id'];deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    state=api.call('GET',f'/jobs/{first}')
                    if state['status']=='running' and state.get('attempt',{}).get('process_identity'):break
                    time.sleep(.1)
                else:raise AssertionError('Batch first job never acquired a real process: '+json.dumps(state)[:1200])
                cancelled=api.call('POST',api.prefix+'/job-batches/'+batch['id']+':cancel',{})
                assert cancelled['ok'],cancelled
                stopped=settled_job(api,first,timeout=90)
                assert stopped['status']=='cancelled',stopped
                deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    detail=api.call('GET',api.prefix+'/job-batches/'+batch['id'])
                    if detail['active']==0 and all(not job['completion_pending'] for job in detail['jobs']):break
                    time.sleep(.1)
                else:raise AssertionError('Batch did not settle')
                assert detail['counts']['cancelled']==2,detail
                credit();check('actual_process_and_queued_job_cancelled',jobs=[j['id'] for j in batch['jobs']],process_stop=stopped['attempt']['process_stop'])
                replay=api.call('POST',api.prefix+'/job-batches/'+batch['id']+':cancel',{})
                assert not replay['ok'] and replay['counts']['cancelled']==2,replay
                credit();check('repeat_cancel_idempotent')
            else:
                from app.services.casedict import set_block
                from cae_benchmarks import thermal_files
                for path,content in thermal_files().items():api.put(path,content)
                source=api.call('POST',api.prefix+'/jobs',{'type':'solve','runtime':'wsl',
                    'command':'source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/conduction && blockMesh && laplacianFoam'})
                solved=settled_job(api,source['id'],timeout=180)
                assert solved['status']=='succeeded',solved
                saved=api.call('GET',api.prefix+'/caseDict')
                post={'runDir':'C-run/conduction','fields':{'T':{'unit':'K'}},
                    'dataExtraction':{'volumeAverages':{'mean':{'field':'T','time':1}}}}
                content=set_block(saved['content'],'postProcessing',post)
                api.call('PUT',api.prefix+'/caseDict',{'content':content,'expected_revision':saved['dict_revision']})
                headers={'Idempotency-Key':'mixed-postprocess'}
                submitted=api.call('POST',api.prefix+'/postprocess:run',{},extra_headers=headers)
                done=settled_job(api,submitted['job_id'],timeout=180)
                assert done['status']=='succeeded',done
                value=api.call('GET',api.prefix+'/postprocess/results')
                mean=next(row for row in value['metrics']['metrics'] if row['name']=='mean.T@1')
                assert abs(mean['value']-350)<1e-6,mean
                credit();check('actual_openfoam_and_postprocess_350K',source_job=source['id'],job_id=done['id'],mean=mean['value'])
                count=len(api.call('GET',api.prefix+'/jobs'))
                replay=api.call('POST',api.prefix+'/postprocess:run',{},extra_headers=headers)
                assert replay['job_id']==done['id'] and len(api.call('GET',api.prefix+'/jobs'))==count
                credit();check('submission_replay_no_new_job')
            boundaries=[json.loads(line) for line in (api.root/'boundaries.jsonl').read_text().splitlines()]
            assert len(boundaries)>=2,boundaries
            check('credit_and_sql_released_orm_retained',boundaries=boundaries)
            result['passed']=True;save()
    except BaseException as error:
        result['failure']={'type':type(error).__name__,'detail':str(error)[:2200]};save()
        print('MIXED_READ_FAILED '+json.dumps(result['failure']),flush=True);return 1
    print('MIXED_READ_OK '+str(api.root/'checks.json'),flush=True);return 0


async def postgres(store,group,verifier=verify):
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    url=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
    name='tmp_vf_mixedread_'+uuid.uuid4().hex
    report=Path(tempfile.mkdtemp(prefix='mixedread-postgres-',dir=SERVER.parents[1]/'tmp'))
    admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
    created=False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        target=url.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,target,report,'upgrade','head')
        return await asyncio.to_thread(verifier,store,target,group)
    finally:
        try:
            if created:
                await admin.execute('DROP DATABASE "'+name+'"')
                assert not await admin.fetchval('SELECT 1 FROM pg_database WHERE datname=$1',name)
                (report/'cleanup.json').write_text(json.dumps({'temporary_database_removed':True,'database':name}),encoding='utf8')
                print('MIXED_READ_PG_CLEANUP '+str(report/'cleanup.json'),flush=True)
        finally:await admin.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',choices=('sqlite','postgresql'),default='sqlite')
    parser.add_argument('--group',choices=('batch','postprocess'),default='batch')
    args=parser.parse_args()
    from s3testsupport import ObjectStore
    with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') as store:
        code=asyncio.run(postgres(store,args.group)) if args.database=='postgresql' else verify(store,group=args.group)
    sys.exit(code)
