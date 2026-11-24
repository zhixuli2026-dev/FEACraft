"""Real API death must retain frozen storage limits while resuming publication."""
import argparse,asyncio,json,re,tempfile,time,uuid
from pathlib import Path
from testsupport import IsolatedServer,SERVER
from publication_finalizing_smoke import CONTROLLED as FINAL,peer
from publication_preparation_recovery_smoke import CONTROLLED as PREPARATION
from job_release_smoke import wait_file
from node_storage_budget_smoke import stop

INSPECT='''
from app.services import process_identity_native as identity
@app.get('/api/v1/fixture/storage-binding/{job_id}')
async def storage_binding(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db,write=False)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  return {'prefix':case.s3_prefix,'inputs':json.loads(a.input_manifest_json),'owner':identity.observe(os.getpid())}
'''

FAULT='''
from app.services import publication_finalizing,publication_preparation_recovery,execution_budget
from app import storage
from pathlib import Path
import time
operation=TARGET
def observed(records):
 marker=Path(__file__).with_suffix('.copy-started');marker.touch()
 if DURING:
  directory=storage._fs_path(PREFIX,RELATIVE)
  directory.mkdir(parents=True,exist_ok=True)
  (directory/'recovery-growth.bin').write_bytes(b'x'*(2*1024*1024))
  time.sleep(.4)
 result=operation(records)
 Path(__file__).with_suffix('.copy-drained').touch()
 return result
TARGET=observed
'''


def verify(modes,url=None,store=None):
    from app.services import process_identity_native as identity
    report=Path(tempfile.mkdtemp(prefix='recovery-storage-runs-',dir=SERVER.parents[1]/'tmp'))
    database=url or 'sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix()
    checks=[];runs=[]
    for mode in modes:
        prebundle=mode.startswith('prebundle');over=mode.endswith('over');during=mode.endswith('during');missing=mode.endswith('missing')
        api=IsolatedServer('recovery-storage-'+mode,app='storage_recovery:app',database_url=url)
        settings={'VIBEFLOW_EXECUTION_WORKSPACE_LIMIT_MB':'1','VIBEFLOW_EXECUTION_STORAGE_POLL_SECONDS':'.05','VIBEFLOW_NODE_STORAGE_MIN_FREE_MB':'0'}
        if url is None:
            assert report.resolve().is_relative_to((SERVER.parents[1]/'tmp').resolve())
            settings['VIBEFLOW_DATABASE_URL']=database
        if store:settings.update(store.settings())
        fixture='import os\nos.environ.update('+repr(settings)+')\n'+(PREPARATION if prebundle else FINAL)+INSPECT
        (api.root/'storage_recovery.py').write_text(fixture,encoding='utf8');(api.root/'mode').write_text('before_copy' if prebundle else 'prepared')
        with api:
            api.env['VIBEFLOW_DATABASE_URL']=database
            api.create_case('Recovery storage '+mode);api.put('E-report/report.md','# Storage recovery\n\nActual report.\n')
            job=api.call('POST',api.prefix+'/report:build',{});job_id=job['job_id']
            wait_file(api.root/'ready',seconds=90)
            before=api.call('GET',f'/fixture/finalizing/{job_id}');binding=api.call('GET',f'/fixture/storage-binding/{job_id}')
            assert binding['inputs']['storage_policy']['max_workspace_bytes']==1024*1024
            stop(api.proc)
            deadline=time.monotonic()+5
            while True:
                observation=identity.probe(binding['owner'])
                (api.root/'owner-exit-observation.json').write_text(json.dumps(observation),encoding='utf8')
                if observation['state']=='exited':break
                assert time.monotonic()<deadline,observation
                time.sleep(.02)
            reader=peer(api);script=reader.root/'finalizing_peer.py'
            prefix=binding['prefix'];relative='.vibeflow/attempts/'+before['attempt_id']
            old_root=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])
            cache=reader.root/'fresh-cache' if store else old_root
            code=script.read_text(encoding='utf8')
            if store:code=code.replace(repr(str(old_root)),repr(str(cache)))
            # Raising live configuration must not relax the original 1 MiB policy.
            extra={'VIBEFLOW_EXECUTION_WORKSPACE_LIMIT_MB':'16','VIBEFLOW_EXECUTION_STORAGE_POLL_SECONDS':'.05','VIBEFLOW_NODE_STORAGE_MIN_FREE_MB':'0'}
            if store:extra.update(store.settings())
            operation='publication_preparation_recovery.resume_copy' if prebundle else 'publication_finalizing._prepared'
            code='import os\nos.environ.update('+repr(extra)+')\n'+code+FAULT.replace('TARGET',operation).replace('DURING',repr(during)).replace('PREFIX',repr(prefix)).replace('RELATIVE',repr(relative))
            script.write_text(code,encoding='utf8')
            directory=cache/prefix/relative
            if over:
                directory.mkdir(parents=True,exist_ok=True);(directory/'retained-overflow.bin').write_bytes(b'o'*(2*1024*1024))
            if missing:
                assert not store and directory.resolve().is_relative_to(api.root.resolve())
                retained=directory.with_name(directory.name+'-retained')
                assert retained.resolve().is_relative_to(api.root.resolve())
                directory.rename(retained)
            with reader:
                deadline=time.monotonic()+90
                while True:
                    final=reader.call('GET',f'/jobs/{job_id}')
                    if final['status'] not in {'queued','running'} and not final['completion_pending']:break
                    assert time.monotonic()<deadline,(mode,final)
                    time.sleep(.1)
                after=reader.call('GET',f'/fixture/finalizing/{job_id}')
                observations=[e['payload'] for e in after['events'] if e['type']=='attempt.storage_usage']
                usage=[e['payload'] for e in after['events'] if e['type']=='attempt.resource_usage']
                (api.root/'storage-recovery-observation.json').write_text(json.dumps({'before':before,'after':after,'job':final,'reader':str(reader.root)},indent=2),encoding='utf8')
                assert len(observations)==len(usage)==1,after
                observed=observations[0]
                assert observed['scope']=='recovery_node' and observed['policy']==binding['inputs']['storage_policy']
                assert after['attempt_count']==1 and after['attempt_id']==before['attempt_id']
                assert not script.with_suffix('.replayed').exists()
                if over or during or missing:
                    assert final['status']=='failed' and final['attempt']['reason_code']=='resource_limit',final
                    assert after['publication_status']=='failed'
                    assert usage[0]['violation']==('disk_unverified' if missing else 'disk_limit')
                    if missing:assert observed['current_bytes'] is None and observed['state']=='unverified'
                    else:assert observed['peak_bytes']>1024*1024
                    if over or missing:assert not script.with_suffix('.copy-started').exists()
                    if during:
                        assert script.with_suffix('.copy-started').exists() and script.with_suffix('.copy-drained').exists()
                        if prebundle:assert any(e['type']=='attempt.bundle_copy_drained' for e in after['events'])
                else:
                    assert final['status']=='succeeded' and usage[0]['violation'] is None
                    assert observed['state'] in {'measured','absent'} and observed['samples']>0
                events=list(after['events'])
                # Repeated reads after terminal commit retain the same usage event.
                again=reader.call('GET',f'/fixture/finalizing/{job_id}')
                assert [e for e in again['events'] if e['type'] in {'attempt.storage_usage','attempt.resource_usage'}]==[
                    e for e in events if e['type'] in {'attempt.storage_usage','attempt.resource_usage'}]
            checks.append(mode+'_retains_frozen_storage_scope_and_failure_across_recovery')
            runs.append({'mode':mode,'source':str(api.root),'reader':str(reader.root)})
            (report/'checks.json').write_text(json.dumps({'passed':False,'count':len(checks),'checks':checks,'runs':runs},indent=2),encoding='utf8')
            print('PASS '+checks[-1],flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(report)}
    (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--s3',action='store_true');parser.add_argument('--modes',nargs='+');args=parser.parse_args()
    from contextlib import nullcontext
    from s3testsupport import ObjectStore
    from s3_typed_publication_smoke import RUNTIME
    modes=args.modes or (['prepared_normal','prepared_over','prepared_during'] if args.s3 else
        ['prepared_normal','prebundle_normal','prepared_over','prebundle_over','prepared_during','prebundle_during','prepared_missing','prebundle_missing'])
    report=Path(tempfile.mkdtemp(prefix='recovery-storage-evidence-',dir=SERVER.parents[1]/'tmp'))
    with ObjectStore(executable=RUNTIME) if args.s3 else nullcontext() as store:
        results={'sqlite':await asyncio.to_thread(verify,modes,None,store)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_recoverstorage_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_recoverstorage_[0-9a-f]{32}',name)
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
    print('RECOVERY_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
