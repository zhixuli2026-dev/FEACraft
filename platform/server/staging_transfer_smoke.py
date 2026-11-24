"""Real S3 legacy transfers: active-writer fencing, death, commit and cancellation."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import hashlib
import os
from pathlib import Path
import signal
import tempfile
import time
import uuid

import httpx
from staging_sync_smoke import boot
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME
from s3_file_transactions_smoke import get
from testsupport import SERVER
from job_release_smoke import wait_file
from app.services import process_identity_native as identity
from app.services.durable_events import json_text

EXTRA = '''
@app.post('/fixture/transfer/{case_id}')
async def transfer(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
 import sys,json
 from app.models import Job
 from app.services import jobrunner
 case=await get_owned_case(case_id,user,db)
 job=Job(case_id=case.id,type='shell',command='legacy transfer fixture')
 db.add(job);await db.flush();job.log_s3_key=f'.vibeflow/jobs/job-{job.id}.log';await db.commit()
 payload="from pathlib import Path; Path('fault.txt').write_text('new'); Path('new.txt').write_text('created'); Path('remove.txt').unlink()"
 async def previous_commit(*args):
  from app.db import SessionLocal
  from app.models import Case
  async with SessionLocal() as other:
   current=await other.get(Case,case.id)
   async with coordinate(other,current):await journal.write(other,current,{'unrelated.txt':b'KEEP'})
   await other.commit()
 proc=await jobrunner.start_job(job.id,job.command,case.s3_prefix,argv=[sys.executable,'-c',payload],
  on_finish=previous_commit if mode()=='settled_pending' else None)
 selected={'job_id':job.id,'stage':proc.staging_prefix,'prefix':case.s3_prefix}
 (root/'run.json').write_text(json.dumps(selected))
 status=await proc.wait()
 return dict(selected,status=status)
publish_for_transfer=journal._publish_pending
def transfer_pending(*args):
 result=publish_for_transfer(*args)
 if mode()=='pending_response_lost':raise TimeoutError('Injected pending publication response loss')
 return result
journal._publish_pending=transfer_pending
'''


def kill_fixture(process,future):
    assert identity.probe(process)['state']=='match'
    os.kill(process['pid'],signal.SIGTERM)
    deadline=time.monotonic()+10
    while identity.probe(process)['state']!='exited' and time.monotonic()<deadline:time.sleep(.05)
    assert identity.probe(process)['state']=='exited'
    try:future.result(timeout=10)
    except (httpx.TransportError,OSError):pass
    else:raise AssertionError('Killed writer returned success')


def observe_committed_resolution(store, prefix):
    journal = prefix + '/.vibeflow/file-transactions/objects/'
    listed = store.raw_client.list_objects_v2(Bucket=store.bucket, Prefix=journal)
    manifests = [key for row in listed.get('Contents', [])
                 if (key := row['Key']).endswith('/manifest.json')]
    documents = [(key, json.loads(get(store, store.bucket, key))) for key in manifests]
    key, document = next((key, document) for key, document in documents
                         if {entry['path'] for entry in document['plan']['files']} == {'fault.txt', 'new.txt', 'remove.txt'})
    directory = key.rsplit('/', 1)[0]
    receipt = json.loads(get(store, store.bucket, directory + '/resolution.json'))
    assert receipt['decision'] == 'committed' and receipt['plan_sha256'] == document['plan_sha256']
    assert get(store, store.bucket, directory + '/drained.json') is None
    return receipt


def run(store, database_url=None, modes=None):
    report = Path(tempfile.mkdtemp(prefix='staging-transfer-evidence-', dir=SERVER.parents[1]/'tmp'))
    settings = store.settings()
    if database_url is None:
        settings['VIBEFLOW_DATABASE_URL'] = 'sqlite+aiosqlite:///' + (report/'test.sqlite').as_posix()
    checks = []
    for selected in modes or ['normal','active','cancel','before_pending','after_one','before_commit','after_commit','response_lost','write_race','foreign','corrupt_drain','settled_pending','pending_response_lost','recovery_crash']:
        api = boot(settings, database_url)
        fixture = api.root/'staging_fixture.py'
        fixture.write_text(fixture.read_text(encoding='utf8')+EXTRA, encoding='utf8')
        with api:
            case = api.create_case('Legacy transfer '+selected)
            api.put('fault.txt', 'old'); api.put('remove.txt', 'remove')
            (api.root/'mode').write_text('after_one' if selected in {'active','cancel','foreign','corrupt_drain','recovery_crash'} else selected)
            shared = {**settings, 'VIBEFLOW_JWT_SECRET': api.env['VIBEFLOW_JWT_SECRET']}
            with boot(shared, database_url) as peer,ExitStack() as extras:
                peer.token = api.token
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(api.call, 'POST', f'/fixture/transfer/{case["id"]}', api_prefix=False)
                    if selected in {'active','cancel','before_pending','after_one','before_commit','after_commit','foreign','corrupt_drain','recovery_crash'}:
                        wait_file(api.root/'ready')
                        wait_file(api.root/'run.json')
                        chosen = json.loads((api.root/'run.json').read_text())
                        pending_key = chosen['prefix']+'/.vibeflow/file-transactions/pending.json'
                        pending_bytes=get(store,store.bucket,pending_key)
                        pending=json.loads(pending_bytes) if pending_bytes is not None else None
                        if selected in {'active','cancel'}:
                            start = time.monotonic()
                            reply = peer.call('POST', f'/fixture/recover/{case["id"]}', api_prefix=False, expect=409)
                            assert time.monotonic()-start < 3 and reply['detail']['code']=='file_sync_busy', reply
                            assert get(store, store.bucket, pending_key) is not None
                            if selected=='cancel':
                                start=time.monotonic()
                                reply=peer.call('POST',f'/jobs/{chosen["job_id"]}:cancel')
                                assert reply['ok'] and time.monotonic()-start<3
                            (api.root/'release').touch()
                            result = future.result(timeout=30)
                            assert result['status']==('cancelled' if selected=='cancel' else 'succeeded'), result
                        else:
                            process=(pending['plan']['writer']['process'] if pending else
                                     api.call('GET',f'/fixture/storage-owner/{case["id"]}',api_prefix=False)['process'])
                            kill_fixture(process,future)
                            if selected in {'foreign','corrupt_drain'}:
                                directory=chosen['prefix']+'/.vibeflow/file-transactions/objects/'+pending['plan']['id']
                            if selected=='foreign':
                                pending['plan']['writer']['process']['host_id']='0'*64
                                pending['plan_sha256']=hashlib.sha256(json_text(pending['plan']).encode()).hexdigest()
                                encoded=json_text(pending).encode()
                                for key in (directory+'/manifest.json',pending_key):
                                    store.client.put_object(Bucket=store.bucket,Key=key,Body=encoded)
                            elif selected=='corrupt_drain':
                                store.client.put_object(Bucket=store.bucket,Key=directory+'/drained.json',Body=b'{}')
                    else:
                        result = future.result(timeout=40)
                        chosen = json.loads((api.root/'run.json').read_text())
                        assert result['status']==('failed' if selected in {'response_lost','write_race','pending_response_lost'} else 'succeeded'), result
                    prefix = chosen['prefix']
                    read = lambda path: get(store, store.bucket, prefix+'/'+path)
                    evidence = {'mode':selected,'api':str(api.root),'peer':str(peer.root)}
                    if selected == 'normal':
                        evidence['same_writer_resolution'] = observe_committed_resolution(store, prefix)
                    if selected in {'write_race','foreign'}:
                        before={path:read(path) for path in ('fault.txt','new.txt','remove.txt')}
                        reply=peer.call('POST',f'/fixture/recover/{case["id"]}',api_prefix=False,expect=409)
                        assert {path:read(path) for path in before}==before
                        assert read('.vibeflow/file-transactions/pending.json') is not None
                        if selected=='write_race':assert read('fault.txt')==b'external edit'
                        if selected=='foreign':assert reply['detail']['code']=='file_sync_busy'
                    else:
                        if selected=='recovery_crash':
                            (peer.root/'mode').write_text('recovery_crash')
                            recovering=pool.submit(peer.call,'POST',f'/fixture/recover/{case["id"]}',api_prefix=False)
                            wait_file(peer.root/'ready')
                            process=peer.call('GET',f'/fixture/storage-owner/{case["id"]}',api_prefix=False)['process']
                            kill_fixture(process,recovering)
                            peer=extras.enter_context(boot(shared,database_url));peer.token=api.token
                        peer.call('POST',f'/fixture/recover/{case["id"]}',api_prefix=False)
                        committed=selected in {'normal','active','after_commit','settled_pending'}
                        assert read('fault.txt')==(b'new' if committed else b'old')
                        assert read('new.txt')==(b'created' if committed else None)
                        assert read('remove.txt')==(None if committed else b'remove')
                        assert read('.vibeflow/file-transactions/pending.json') is None
                        peer.call('POST',f'/fixture/recover/{case["id"]}',api_prefix=False)
                        if selected=='settled_pending':assert read('unrelated.txt')==b'KEEP'
                    if selected not in {'normal','active','settled_pending'}:
                        assert (api.root/'files'/chosen['stage']).exists(), 'Failure must preserve the source stage'
                    checks.append(evidence)
        print('PASS '+selected, flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'database':'postgresql' if database_url else 'sqlite'}
    (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'evidence':str(report)}),flush=True)
    return report,result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+')
    args=parser.parse_args()
    with ObjectStore(executable=RUNTIME,volume_max=12) as store:
        if not args.postgres:
            await asyncio.to_thread(run,store,None,args.modes);return
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_transfer_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            logs=Path(tempfile.mkdtemp(prefix='transfer-migration-',dir=SERVER.parents[1]/'tmp'))
            await asyncio.to_thread(alembic,temporary,logs,'upgrade','head')
            report,result=await asyncio.to_thread(run,store,temporary,args.modes)
        finally:
            if created:
                await admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1',name)
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        result['temporary_database_removed']=True
        (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
