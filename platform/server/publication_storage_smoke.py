"""Real report copies, API termination and peer quota admission on FS or S3."""
import argparse,asyncio,json,re,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from testsupport import IsolatedServer,SERVER
from publication_preparation_recovery_smoke import CONTROLLED
from publication_finalizing_smoke import peer
from job_release_smoke import wait_file
from node_storage_budget_smoke import stop

INSPECT='''
import asyncio,time
from app.services import quota,publication_storage,process_identity_native as identity
from app.models import PublicationStorageReservation,Case
from app.services.artifact_publication import PublicationIdentity
from app.services import publication_journal
from app import storage
async def quotas(db,user):
 limit=Path(__file__).parent/'quota-limit'
 return {**quota.DEFAULT_QUOTAS,'storage_mb':int(limit.read_text()) if limit.exists() else 4}
quota.quotas_for=quotas
@app.post('/api/v1/fixture/hold-owner')
async def hold_owner(user:User=Depends(get_current_user)):
 from app.services.storage_admission import lock_owner
 root=Path(__file__).parent
 async with SessionLocal() as db:
  await lock_owner(db,user.id)
  (root/'capacity-lock-held').touch()
  deadline=time.monotonic()+10
  while not (root/'capacity-lock-release').exists():
   if time.monotonic()>deadline:raise TimeoutError('Missing owner lock release')
   await asyncio.sleep(.02)
  await db.commit()
 return {'released':True}
@app.get('/api/v1/fixture/capacity/{job_id}')
async def capacity_state(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db,write=False)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  pub=await db.scalar(select(ArtifactPublication).where(ArtifactPublication.attempt_id==a.id))
  row=await db.get(PublicationStorageReservation,pub.id)
  return {'reservation':None if row is None else {key:getattr(row,key) for key in ('state','size','remaining_bytes','manifest_sha256','token','writer_json')},
   'usage':await quota.usage(db,user),'prefix':case.s3_prefix,'publication_id':pub.id,'owner':identity.observe(os.getpid())}
@app.post('/api/v1/fixture/remove-ready/{job_id}')
async def remove_ready(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  pub=await db.scalar(select(ArtifactPublication).where(ArtifactPublication.attempt_id==a.id))
  # Compare the actual immutable payload, manifest, journal and lock bytes.
  paths=[pub.immutable_path,publication_journal.DIRECTORY+'/'+pub.id]
  size=sum(item['size'] for path in paths for item in storage.list_tree(case.s3_prefix,path))
  await asyncio.to_thread(storage._s3_delete if storage._use_s3() else storage._fs_delete,case.s3_prefix,paths[1]+'/ready.json')
  return {'copy_bytes':size}
@app.post('/api/v1/fixture/duplicate-copy/{job_id}')
async def duplicate(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  pub=await db.scalar(select(ArtifactPublication).where(ArtifactPublication.attempt_id==a.id))
  captured=PublicationIdentity.capture(pub)
 manifest=(await asyncio.to_thread(publication_journal.read_intent,case.s3_prefix,captured))['manifest']
 async with publication_storage.capacity(case.id,case.s3_prefix,captured,manifest['files'],manifest['descriptor']):
  raise AssertionError('A second writer entered the same reserved copy')
'''

FAULT='''
original_finish=pub.prepare_bundle_for_finish
async def finish(*args,**kwargs):
 if mode()=='quota':(root/'quota-limit').write_text('0')
 return await original_finish(*args,**kwargs)
pub.prepare_bundle_for_finish=finish
original_copy=pub.prepare_bundle
def failing(*args,**kwargs):
 if mode()=='error':
  hold()
  raise OSError('Injected drained copy failure')
 result=original_copy(*args,**kwargs)
 if mode()=='complete_error':raise TimeoutError('Lost completed publication copy response')
 return result
pub.prepare_bundle=failing
'''


def terminal(api,job_id):
    deadline=time.monotonic()+95
    while True:
        job=api.call('GET',f'/jobs/{job_id}')
        assert not job.get('completion_error'),job['completion_error']
        if job['status'] not in {'queued','running'} and not job['completion_pending']:return job
        assert time.monotonic()<deadline,job
        time.sleep(.1)


def verify(modes,url=None,store=None):
    from app.services import process_identity_native as identity
    report=Path(tempfile.mkdtemp(prefix='publication-storage-runs-',dir=SERVER.parents[1]/'tmp'))
    database=url or 'sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix()
    checks=[];runs=[]
    for mode in modes:
        api=IsolatedServer('publication-storage-'+mode,app='capacity_fixture:app',database_url=url)
        settings={'VIBEFLOW_NODE_STORAGE_MIN_FREE_MB':'0','VIBEFLOW_DATABASE_URL':database}
        if store:settings.update(store.settings())
        (api.root/'capacity_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+CONTROLLED+INSPECT+FAULT,encoding='utf8')
        (api.root/'mode').write_text('after_copy' if mode in {'after_copy','missing_ready'} else mode)
        if mode=='quota':(api.root/'release').touch()
        with api:
            api.env['VIBEFLOW_DATABASE_URL']=database
            api.create_case('Publication capacity '+mode)
            api.put('E-report/report.md','# Publication capacity\n\nImmutable report.\n')
            job_id=api.call('POST',api.prefix+'/report:build',{})['job_id']
            if mode=='quota':
                final=terminal(api,job_id)
                after=api.call('GET',f'/fixture/capacity/{job_id}')
                details=api.call('GET',f'/fixture/finalizing/{job_id}')
                assert final['status']=='failed' and final['attempt']['reason_code']=='resource_limit',final
                assert after['reservation'] is None and details['publication_status']=='failed'
                assert not (api.root/'ready').exists(),'Copy was admitted despite exhausted quota'
                usage=[e['payload'] for e in details['events'] if e['type']=='attempt.resource_usage']
                assert len(usage)==1 and usage[0]['violation']=='storage_limit',usage
                before=None;reader_root=None
            else:
                wait_file(api.root/'ready',seconds=90)
                before=api.call('GET',f'/fixture/capacity/{job_id}')
                claim=before['reservation']
                assert claim['state']=='active' and claim['remaining_bytes']==claim['size']>0,before
                assert before['usage']['storage_bytes_reserved']==claim['size']
                if mode in {'normal','error','settlement_race','complete_error'}:
                    reader=peer(api);script=reader.root/'finalizing_peer.py'
                    extra=store.settings() if store else {}
                    script.write_text('import os\nos.environ.update('+repr(extra)+')\n'+script.read_text(encoding='utf8')+INSPECT,encoding='utf8')
                    with reader:
                        observed=reader.call('GET',f'/fixture/capacity/{job_id}')
                        assert observed['reservation']==claim
                        reader.call('POST',f'/fixture/duplicate-copy/{job_id}',expect=409)
                        # This payload would fit without the durable copy claim.
                        amount=4*1024*1024-observed['usage']['storage_bytes_used']
                        assert amount>claim['size']
                        reader.call('POST',api.prefix+'/uploads',{'path':'Ref/competing.bin','size':amount},expect=429)
                        if mode=='settlement_race':
                            with ThreadPoolExecutor(max_workers=1) as pool:
                                held=pool.submit(reader.call,'POST','/fixture/hold-owner')
                                wait_file(reader.root/'capacity-lock-held',seconds=8)
                                (api.root/'release').touch()
                                try:
                                    ready=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/before['prefix']/'.vibeflow/publications/.journal'/before['publication_id']/'ready.json'
                                    wait_file(ready,seconds=8)
                                    time.sleep(.3)
                                finally:(reader.root/'capacity-lock-release').touch()
                                assert held.result(timeout=10)=={'released':True}
                        else:(api.root/'release').touch()
                        final=terminal(api,job_id)
                        after=reader.call('GET',f'/fixture/capacity/{job_id}')
                        failed=mode in {'error','complete_error'}
                        assert final['status']==('failed' if failed else 'succeeded'),final
                        assert after['reservation']['state']==('active' if failed and store else 'released' if failed else 'materialized')
                    reader_root=str(reader.root)
                    if mode=='complete_error' and store:
                        recovered=peer(api);script=recovered.root/'finalizing_peer.py'
                        script.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+script.read_text(encoding='utf8')+INSPECT,encoding='utf8')
                        with recovered:
                            after=recovered.call('GET',f'/fixture/capacity/{job_id}')
                            assert after['reservation']['state']=='materialized',after
                            assert recovered.call('GET',f'/jobs/{job_id}')['status']=='failed'
                            assert not script.with_suffix('.replayed').exists()
                        reader_root=str(recovered.root)
                else:
                    if mode=='missing_ready':
                        actual=api.call('POST',f'/fixture/remove-ready/{job_id}')
                        assert actual['copy_bytes']==claim['size'],(actual,claim)
                    stop(api.proc)
                    deadline=time.monotonic()+5
                    while identity.probe(before['owner'])!={'state':'exited','reason':'process_exited'}:
                        assert time.monotonic()<deadline
                        time.sleep(.02)
                    reader=peer(api);script=reader.root/'finalizing_peer.py'
                    extra=store.settings() if store else {}
                    script.write_text('import os\nos.environ.update('+repr(extra)+')\n'+script.read_text(encoding='utf8')+INSPECT,encoding='utf8')
                    if mode in {'after_copy','recover_over','missing_ready'}:(reader.root/'quota-limit').write_text('0')
                    with reader:
                        final=terminal(reader,job_id)
                        after=reader.call('GET',f'/fixture/capacity/{job_id}')
                        details=reader.call('GET',f'/fixture/finalizing/{job_id}')
                        rejected=mode in {'recover_over','missing_ready'}
                        assert final['status']==('failed' if rejected else 'succeeded'),final
                        assert after['reservation']['state']==('active' if rejected and store else 'released' if rejected else 'materialized'),after
                        assert details['attempt_count']==1 and not script.with_suffix('.replayed').exists()
                        if rejected:assert final['attempt']['reason_code']=='resource_limit'
                    reader_root=str(reader.root)
                expected_remaining=claim['size'] if after['reservation']['state']=='active' else 0
                assert after['reservation']['remaining_bytes']==expected_remaining and after['usage']['storage_bytes_reserved']==expected_remaining
            evidence={'mode':mode,'before':before,'after':after,'job':final,'reader':reader_root}
            (api.root/'capacity-result.json').write_text(json.dumps(evidence,indent=2),encoding='utf8')
            checks.append(mode);runs.append({'mode':mode,'root':str(api.root),'reader':reader_root})
            (report/'checks.json').write_text(json.dumps({'passed':False,'checks':checks,'runs':runs},indent=2),encoding='utf8')
            print('PASS publication capacity '+mode,flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs}
    (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--postgres-only',action='store_true');parser.add_argument('--s3',action='store_true');parser.add_argument('--modes',nargs='+')
    args=parser.parse_args();modes=args.modes or ['normal','quota','error','death','after_copy','recover_over']
    from contextlib import nullcontext
    from s3testsupport import ObjectStore
    from s3_typed_publication_smoke import RUNTIME
    report=Path(tempfile.mkdtemp(prefix='publication-storage-evidence-',dir=SERVER.parents[1]/'tmp'))
    assert not args.postgres_only or args.postgres
    results={}
    if not args.postgres_only:
        with ObjectStore(executable=RUNTIME) if args.s3 else nullcontext() as store:
            results['sqlite']=await asyncio.to_thread(verify,modes,None,store)
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_pubstorage_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_pubstorage_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            with ObjectStore(executable=RUNTIME) if args.s3 else nullcontext() as store:
                results['postgresql']=await asyncio.to_thread(verify,modes,target,store)
        finally:
            if created:await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('PUBLICATION_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
