"""Actual abandoned sealing, protected records, and maintenance page resumption."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

import httpx

from case_copy_source_lock_smoke import RUNTIME, read_observation
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


PREFIX = '.vibeflow/attempt-provisions'
CURSOR = PREFIX+'/.maintenance.json'
UNKNOWN_ID = '00000000-0000-4000-8000-000000000001'
UNKNOWN = PREFIX+'/pending/'+UNKNOWN_ID+'.json'
INPUT = b'Original real sealed input\n'
SENTINEL = b'Unknown history retained exactly\n'
MAINTENANCE = '/case-provisions:maintenance'

FIXTURE = '''
from pathlib import Path
import json
import os
import time

from fastapi import Depends
from app import storage
from app.deps import get_current_user
from app.main import app
from app.services import attempt_objects, attempt_workspace, process_identity_native, storage_maintenance

root = Path(__file__).parent
storage_maintenance.SEAL_PAGE_SIZE = 1
selected = attempt_objects.Snapshot if storage._use_s3() else attempt_workspace.Snapshot
actual_prepare = selected.prepare
actual_maintain = storage_maintenance.maintain_owner


def prepare(self):
    result = actual_prepare(self)
    if (root/'hold-seal').exists():
        proof = {'identity':process_identity_native.observe(os.getpid()), 'attempt_id':result['attempt_id']}
        temporary = root/'seal-ready.writing'
        temporary.write_text(json.dumps(proof), encoding='utf8')
        temporary.replace(root/'seal-ready.json')
        until = time.monotonic()+40
        while time.monotonic()<until:
            time.sleep(.04)
        raise TimeoutError('Test did not end the API holding a prepared seal')
    return result


selected.prepare = prepare


async def observe_maintenance(*args, **options):
    result = await actual_maintain(*args, **options)
    if os.environ.get('VIBEFLOW_STORAGE_MAINTENANCE_ENABLED') == 'true' and result['attempts']:
        temporary = root/'background-page.writing'
        temporary.write_text(json.dumps({'attempts':result['attempts']}), encoding='utf8')
        temporary.replace(root/'background-page.json')
    return result


storage_maintenance.maintain_owner = observe_maintenance


@app.get('/fixture/seal-process')
def identity(user=Depends(get_current_user)):
    return process_identity_native.observe(os.getpid())
'''


def boot(label, config, database_url):
    api = IsolatedServer('seal-page-'+label, app='seal_page_fixture:app', database_url=database_url)
    temp = api.root/'native-temp'
    temp.mkdir()
    configuration = {**config, 'TEMP':str(temp), 'TMP':str(temp), 'TMPDIR':str(temp)}
    module = api.root/'seal_page_fixture.py'
    module.write_text('import os\nos.environ.update('+repr(configuration)+')\n'+FIXTURE, encoding='utf8')
    return api, module


def process_exited(identity):
    from app.services.process_identity_native import probe

    until = time.monotonic()+12
    while True:
        outcome = probe(identity)
        if outcome['state'] == 'exited':
            return outcome
        if time.monotonic() >= until:
            raise AssertionError(outcome)
        time.sleep(.04)


def local(root, prefix, path):
    root = Path(root).resolve()
    target = (root / prefix / path).resolve()
    assert target.is_relative_to(root), target
    return target


def get(root, store, prefix, path):
    if store is None:
        try:
            return local(root,prefix,path).read_bytes()
        except FileNotFoundError:
            return None
    from botocore.exceptions import ClientError
    try:
        response = store.client.get_object(Bucket=store.bucket,Key=prefix.rstrip('/')+'/'+path)
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] == 404:
            return None
        raise
    with response['Body'] as body:
        return body.read()


def create(root, store, prefix, path, body):
    if store is not None:
        store.client.put_object(Bucket=store.bucket, Key=prefix.rstrip('/')+'/'+path, Body=body, IfNoneMatch='*')
        return
    target = local(root,prefix,path)
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('xb') as stream:
        stream.write(body)


def seed_writing(root, prefix, plan):
    from app.services.process_identity_native import observe

    writer = observe(os.getpid())
    active = PREFIX+'/writing/'+'0'*32
    detached = PREFIX+'/writing/'+'f'*32+'.part'
    document = {'schema':'vibeflow.attempt-plan-write.v1','id':'0'*32,'binding':plan['binding'],
        'target':PREFIX+'/history/'+UNKNOWN_ID+'.json','owner':writer,
        'file':{'size':len(SENTINEL),'sha256':hashlib.sha256(SENTINEL).hexdigest()}}
    raw = json.dumps(document).encode('utf8')
    for path, body in ((active+'.json',raw),(active+'.part',SENTINEL),(detached,SENTINEL)):
        create(root,None,prefix,path,body)
    return {'active_intent':active+'.json','detached':detached}, writer


def read_cursor(root, prefix):
    raw = get(root,None,prefix,CURSOR)
    if raw is None:
        return None
    return json.loads(raw)


def maintain(api):
    result = api.call('POST',MAINTENANCE)
    return result['attempts']


def peer(label, settings, database_url, source, *, cache_root=None):
    configuration = {**settings, 'VIBEFLOW_DATABASE_URL':source.env['VIBEFLOW_DATABASE_URL'],
                     'VIBEFLOW_JWT_SECRET':source.env['VIBEFLOW_JWT_SECRET']}
    if cache_root is not None:
        configuration['VIBEFLOW_STORAGE_FS_ROOT'] = str(cache_root)
    result, module = boot(label,configuration,database_url)
    result.token = source.token
    return result, module


def exercise(settings, store, database_url, baseline, result):
    from app.services.process_identity_native import probe

    source, module = boot('abandoned',settings,database_url)
    print('SEAL_PAGE_SOURCE',source.root,flush=True)
    try:
        with source:
            case = source.create_case('Seal recovery pagination')
            case_id = case['id']
            prefix = source.call('GET',source.prefix)['storage_prefix']
            root = source.env['VIBEFLOW_STORAGE_FS_ROOT']
            source.put('Ref/input.txt',INPUT.decode('utf8'))
            (source.root/'hold-seal').touch()
            with ThreadPoolExecutor(max_workers=1) as pool:
                operation = pool.submit(source.call,'POST',source.prefix+'/jobs',{
                    'command':'echo SEAL_PAGE_SOURCE','input_paths':['Ref/input.txt']})
                held = read_observation(source,'seal-ready.json')
                attempt = held['attempt_id']
                assert not operation.done() and probe(held['identity'])['state']=='match'
                assert attempt > UNKNOWN_ID, attempt
                record = PREFIX+'/pending/'+attempt+'.json'
                plan = json.loads(get(root,store,prefix,record))
                live_input = plan['manifest']['inputs_path']+'/Ref/input.txt'
                assert get(root,store,prefix,live_input)==INPUT
                source.proc.terminate()
                source.proc.wait(timeout=15)
                try:
                    operation.result(timeout=15)
                except (OSError,httpx.TransportError):
                    pass
                else:
                    raise AssertionError('Abandoned sealing returned an HTTP result')
                stopped = process_exited(held['identity'])
                result['abandoned'] = {'process':held['identity'],'api_port':source.port,'exit':stopped,
                    'attempt_id':attempt,'source_before_exit':True}
    finally:
        module.unlink(missing_ok=True)

    create(root,store,prefix,UNKNOWN,SENTINEL)
    writing = None
    if store is None:
        writing, live = seed_writing(root,prefix,plan)
        result['live_writing_process'] = live
    else:
        legacy = '.vibeflow/attempts/'+UNKNOWN_ID+'/unknown.bin'
        create(root,None,prefix,legacy,SENTINEL)

    first, module = peer('first',settings,database_url,source,cache_root=root if store is None else None)
    try:
        with first:
            first_id = first.call('GET','/fixture/seal-process',api_prefix=False)
            first_root = root if store is None else first.env['VIBEFLOW_STORAGE_FS_ROOT']
            first_records = result['first_process_pages'] = []
            for _ in range(4):
                records = maintain(first)
                cursor = read_cursor(first_root,prefix)
                first_records.append({'items':records,'cursor':cursor})
                if baseline:
                    assert cursor is None and any(row.get('attempt_id')==attempt and row['status']=='rolled_back' for row in records), records
                    assert get(root,store,prefix,record) is None
                    break
                assert len(records)<=1, records
                if any(row.get('record')==UNKNOWN for row in records):
                    assert cursor['phase']=='pending' and cursor['after']==Path(UNKNOWN).name, cursor
                    assert get(root,store,prefix,record) is not None and get(root,store,prefix,live_input)==INPUT
                    break
            else:
                raise AssertionError(first_records)
        result['first_exit'] = {'process':first_id,'api_port':first.port,'exit':process_exited(first_id)}
    finally:
        module.unlink(missing_ok=True)

    if not baseline:
        assert (read_cursor(first_root,prefix) or {})['after']==Path(UNKNOWN).name
        background = {**settings, 'VIBEFLOW_STORAGE_MAINTENANCE_ENABLED':'true',
                      'VIBEFLOW_STORAGE_MAINTENANCE_INTERVAL_SECONDS':'300'}
        second, module = peer('resumed',background,database_url,source,cache_root=first_root)
        try:
            with second:
                second_id = second.call('GET','/fixture/seal-process',api_prefix=False)
                after = result['resumed_page'] = read_observation(second,'background-page.json')['attempts']
                assert after==[{'attempt_id':attempt,'status':'rolled_back'}], after
                assert get(root,store,prefix,record) is None and get(root,store,prefix,live_input) is None
                receipt = json.loads(get(root,store,prefix,PREFIX+'/history/'+attempt+'.json'))
                assert receipt=={'plan':plan,'outcome':'rolled_back'}, receipt
                assert second.call('GET',source.prefix+'/jobs')==[]
                assert get(root,store,prefix,'Ref/input.txt')==INPUT
                assert get(root,store,prefix,UNKNOWN)==SENTINEL
                diagnosed = second.call('GET',source.prefix+'/attempt-preparations')
                assert any(row.get('record')==UNKNOWN and row['status']=='recovery_required' for row in diagnosed['results'])
                if writing:
                    assert probe(live)['state']=='match'
                    assert any(row.get('record')==writing['active_intent'] and row['status']=='active_writer'
                               for row in diagnosed['results']), diagnosed
                    assert get(root,None,prefix,writing['detached'])==SENTINEL
        finally:
            module.unlink(missing_ok=True)
        result['resumed_exit'] = {'process':second_id,'api_port':second.port,'exit':process_exited(second_id)}

        if store is not None:
            assert get(root,None,prefix,live_input)==INPUT, 'Remote peer must leave the older node cache'
            cache, module = peer('old-cache',settings,database_url,source,cache_root=root)
            try:
                with cache:
                    cache_id = cache.call('GET','/fixture/seal-process',api_prefix=False)
                    pages = result['old_node_pages'] = []
                    for _ in range(4):
                        rows = maintain(cache)
                        pages.append({'items':rows,'cursor':read_cursor(root,prefix)})
                        assert len(rows)<=1, rows
                        if any(row.get('status')=='node_cache_removed' and row['attempt_id']==attempt for row in rows):
                            break
                    else:
                        raise AssertionError(pages)
                    assert get(root,None,prefix,live_input) is None
                    assert get(root,None,prefix,legacy)==SENTINEL
                    assert get(root,store,prefix,UNKNOWN)==SENTINEL
            finally:
                module.unlink(missing_ok=True)
            result['cache_exit'] = {'process':cache_id,'api_port':cache.port,'exit':process_exited(cache_id)}


@asynccontextmanager
async def database(kind, evidence):
    if kind=='sqlite':
        yield None
        return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic

    url = make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
    name = 'tmp_vf_sealpage_'+uuid.uuid4().hex
    admin = await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,
                                 database='postgres',timeout=10)
    created = False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"')
        created = True
        temporary = url.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,temporary,evidence,'upgrade','head')
        yield temporary
    finally:
        if created:
            await admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1',name)
            await admin.execute('DROP DATABASE "'+name+'"')
            assert not await admin.fetchval('SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)',name)
            (evidence/'postgres-cleanup.json').write_text(json.dumps({'database':name,'deleted':True}),encoding='utf8')
        await admin.close()


async def run(storage_name, database_name, baseline):
    import tempfile

    evidence = Path(tempfile.mkdtemp(prefix=f'seal-maintenance-{database_name}-{storage_name}-',dir=SERVER.parents[1]/'tmp'))
    report = {'passed':False,'baseline':baseline,'storage':storage_name,'database':database_name,
              'model_calls':0,'mock_llm_calls':0}
    context = ObjectStore(executable=RUNTIME,volume_max=24,authenticated=True) if storage_name=='s3' else nullcontext()
    print('SEAL_PAGE_EVIDENCE',evidence,flush=True)
    try:
        async with database(database_name,evidence) as url:
            with context as store:
                settings = store.settings() if store is not None else {}
                if store is not None:
                    report['store_receipt'] = str(store.root/'store.json')
                await asyncio.to_thread(exercise,settings,store,url,baseline,report)
            if store is not None:
                report['store_exit'] = store.proc.poll()
                assert report['store_exit']==0
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__,'message':str(error)[:1200]}
        raise
    finally:
        (evidence/'seal-maintenance-page.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('SEAL_PAGE_PASS',evidence,flush=True)


async def main():
    baseline = os.environ.get('VIBEFLOW_SEAL_MAINTENANCE_BASELINE')=='1'
    await run('fs','sqlite',baseline)
    if not baseline:
        await run('s3','sqlite',False)
        await run('fs','postgresql',False)
        await run('s3','postgresql',False)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    asyncio.run(main())
