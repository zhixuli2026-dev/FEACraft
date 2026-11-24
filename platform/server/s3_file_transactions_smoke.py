"""Real S3/SQL file journals across process death, fresh caches and CAS races."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import uuid

import httpx
from botocore.exceptions import ClientError

from app.services.casedict import set_block
from file_transactions_smoke import interrupt, wait_ready, preparation_pages
from s3testsupport import ObjectStore,LEGACY_CASE_FIXTURE,create_legacy_case
from sandbox_normal_close_resource_smoke import RUNTIME
from testsupport import IsolatedServer, SERVER

PREFIX = '.vibeflow/file-transactions'
PATHS = ('0-caseDict/caseDict', '0-caseDict/case-events.jsonl', '0-caseDict/provenance.jsonl')
FIXTURE = '''
import time, asyncio
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from app import storage
from app.services import s3_file_transactions as journal
from app.services import attempt_objects as objects
from app.main import app
from fastapi import Depends
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services.case_write import coordinate
root=Path(__file__).parent
def mode(): return (root/'mode').read_text() if (root/'mode').exists() else ''
def hold():
    (root/'ready').touch()
    deadline=time.monotonic()+90
    while not (root/'release').exists():
        if time.monotonic()>deadline: raise TimeoutError('S3 transaction fixture')
        time.sleep(.02)
publish,apply,conditional,commit=journal._publish_pending,journal._apply,journal._conditional,AsyncSession.commit
cancel_loop,cancel_callback=None,None
def publishing(*args):
    if mode()=='before_pending': hold()
    return publish(*args)
def applying(case,directory,index,entry,side,expected_side,**options):
    if mode()=='cancel_write' and side=='after' and index==0:
        cancel_loop.call_soon_threadsafe(cancel_callback)
        hold()
    result=apply(case,directory,index,entry,side,expected_side,**options)
    if side=='after' and ((mode()=='after_one' and index==0) or (mode()=='after_two' and index==1)): hold()
    if side=='before' and mode()=='recovery_crash' and index==0: hold()
    return result
def changing(prefix,path,data,etag,metadata,**options):
    if mode() in {'write_race','recovery_race'} and path in {'fault.txt','new.txt'}:
        storage._s3_put(prefix,path,b'external edit','application/external')
    if mode()=='pointer_race' and path==journal.PENDING and data is None:
        storage._s3_put(prefix,path,b'foreign pointer','application/json')
    result=conditional(prefix,path,data,etag,metadata,**options)
    if mode()=='response_lost' and path=='fault.txt':
        raise TimeoutError('fixture response lost after successful object mutation')
    return result
async def committing(self):
    owned=self.info.get('local_file_transactions',{})
    if owned and mode()=='before_commit': hold()
    result=await commit(self)
    if owned and mode()=='after_commit': hold()
    return result
journal._publish_pending,journal._apply,journal._conditional,AsyncSession.commit=publishing,applying,changing,committing
original_write=journal.write
async def writing(*args,**kwargs):
    global cancel_loop,cancel_callback
    if mode()=='cancel_write':
        task=asyncio.current_task()
        def cancel():
            task.cancel()
            (root/'cancelled').touch()
        cancel_loop,cancel_callback=asyncio.get_running_loop(),cancel
    return await original_write(*args,**kwargs)
journal.write=writing
@app.post('/fixture/own-marker/{case_id}')
async def own_marker(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
    case=await get_owned_case(case_id,user,db)
    async with coordinate(db,case):
        await journal.write(db,case,{'fault.txt':b'uncommitted'})
        async with coordinate(db,case,content_write=False):
            assert storage.get_bytes(case.s3_prefix,'fault.txt')==b'uncommitted'
            assert storage.exists(case.s3_prefix,journal.PENDING)
    await db.rollback()
    await db.refresh(user)
    case=await get_owned_case(case_id,user,db)
    async with coordinate(db,case):
        assert storage.get_bytes(case.s3_prefix,'fault.txt')==b'old'
        assert not storage.exists(case.s3_prefix,journal.PENDING)
    await db.commit()
    return {'ok':True}
'''


def get(store, bucket, key):
    try:
        response = store.client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] == 404:
            return None
        raise
    try:
        return response['Body'].read()
    finally:
        response['Body'].close()


def fresh(api, settings, mode=''):
    peer = IsolatedServer('s3-file-transaction-peer', app='s3_peer:app', database_url=api.database_url)
    overrides = {key: api.env[key] for key in ('VIBEFLOW_DATABASE_URL', 'VIBEFLOW_JWT_SECRET')}
    overrides.update(settings)
    (peer.root/'s3_peer.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+FIXTURE, encoding='utf8')
    (peer.root/'mode').write_text(mode)
    peer.token = api.token
    assert not (peer.root/'files').exists(), 'Recovery must start with an empty independent local cache'
    return peer


MODES = ('before_pending', 'after_one', 'before_commit', 'after_commit', 'create', 'delete',
         'dict_partial', 'dict_committed', 'advance_partial', 'advance_committed',
         'external', 'corrupt', 'binding', 'response_lost',
         'write_race', 'create_race', 'delete_race', 'recovery_race', 'recovery_crash', 'pointer_race', 'metadata', 'cancel_write', 'normal')
COMMITTED_MODES = {'after_commit','dict_committed','advance_committed'}
DICTIONARY_MODES = {'dict_partial','dict_committed','advance_partial','advance_committed'}


def run(store, database_url=None, modes=None):
    checks, runs = [], []
    # One disposable database supplies unique owner/case prefixes. Creating one
    # bucket per crash case would consume the fixture's three volume slots.
    bucket = store.bucket
    settings = store.settings()
    if database_url is None:
        settings['VIBEFLOW_DATABASE_URL'] = 'sqlite+aiosqlite:///' + (store.root/'journal.sqlite').as_posix()
    for mode in modes or MODES:
        api = IsolatedServer('s3-file-transaction-'+mode, app='s3_fixture:app', database_url=database_url)
        (api.root/'s3_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE, encoding='utf8')
        with api:
            case = api.create_case('S3 file transaction '+mode)
            keys = [item for page in store.client.get_paginator('list_objects_v2').paginate(Bucket=bucket)
                    for item in page.get('Contents', [])]
            suffix = '/0-caseDict/caseDict'
            source_key = next(item['Key'] for item in keys if item['Key'].endswith('/cases/'+str(case['id'])+suffix))
            prefix = source_key[:-len(suffix)]
            def read(path): return get(store, bucket, prefix+'/'+path)
            def put(path, data, **metadata):
                store.client.put_object(Bucket=bucket, Key=prefix+'/'+path, Body=data, **metadata)
            api.put('fault.txt', 'old')
            before_dict = api.call('GET', api.prefix+'/caseDict')
            before_mirror = api.call('GET', api.prefix)
            old_dictionary = {path: read(path) for path in PATHS}
            if mode == 'metadata':
                put('fault.txt', b'old', ContentType='application/test', Metadata={'source': 'original'}, CacheControl='no-cache')
            headers = {'Authorization': 'Bearer '+api.token}
            with httpx.Client(base_url=api.base, headers=headers, trust_env=False, timeout=60) as client:
                endpoint = '/api/v1'+api.prefix
                if mode == 'cancel_write':
                    (api.root/'mode').write_text(mode)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        writing = pool.submit(client.put, endpoint+'/file', json={'path':'fault.txt','content':'cancelled write'})
                        try:
                            wait_ready(api)
                            deadline=time.monotonic()+10
                            while not (api.root/'cancelled').exists():
                                if time.monotonic()>deadline: raise AssertionError('Cancellation not injected')
                                time.sleep(.02)
                            assert client.get('/api/health',timeout=3).status_code==200
                            reading=pool.submit(client.get,endpoint+'/raw?path=fault.txt')
                            time.sleep(.2)
                            assert not writing.done() and not reading.done(), 'Cancellation released the SQL lock before S3 worker completion'
                        finally:
                            (api.root/'release').touch()
                        assert writing.result(timeout=15).status_code==500
                        assert reading.result(timeout=15).content==b'old'
                    assert read('fault.txt')==b'old' and read(PREFIX+'/pending.json') is None
                    checks.append(mode)
                    runs.append({'mode':mode,'api':str(api.root),'bucket':bucket})
                    continue
                if mode == 'normal':
                    owned = client.post('/fixture/own-marker/'+str(case['id']))
                    assert owned.status_code == 200, owned.text
                    uploaded = client.post(endpoint+'/upload?path=image.bin', files={'file': ('image.bin', b'\x00\xffsample', 'image/custom')})
                    assert uploaded.status_code == 200, uploaded.text
                    assert read('image.bin') == b'\x00\xffsample'
                    info = store.client.head_object(Bucket=bucket, Key=prefix+'/image.bin')
                    assert info['ContentType'] == 'image/custom'
                    assert client.delete(endpoint+'/file?path=image.bin').status_code == 200
                    assert read('image.bin') is None and read(PREFIX+'/pending.json') is None
                    protected = client.put(endpoint+'/file', json={'path': PREFIX+'/pending.json', 'content': 'bad'})
                    assert protected.status_code == 409, protected.text
                    assert api.call('GET', api.prefix+'/file-transactions') == {'backend': 's3', 'status': 'idle'}
                    checks.append(mode)
                    runs.append({'mode': mode, 'api': str(api.root), 'bucket': bucket})
                    continue
                fault = ('after_two' if mode in {'dict_partial','advance_partial'} else 'after_commit' if mode in COMMITTED_MODES
                         else 'write_race' if mode in {'create_race','delete_race'} else mode
                         if mode in {'before_pending','after_one','before_commit','after_commit','response_lost','write_race'} else 'after_one')
                (api.root/'mode').write_text(fault)
                if mode.startswith('dict_'):
                    content = set_block(before_dict['content'], 'caseMeta', {'caseName': 'S3-journal'})
                    request = lambda: client.put(endpoint+'/caseDict', json={'content': content, 'expected_revision': before_dict['dict_revision']})
                elif mode.startswith('advance_'):
                    request = lambda: client.post(endpoint+'/caseDict:advance', json={
                        'state':'V','confirmed':True,'expected_revision':before_dict['dict_revision']})
                elif mode in {'delete', 'delete_race'}:
                    request = lambda: client.delete(endpoint+'/file?path=fault.txt')
                else:
                    target = 'new.txt' if mode in {'create','create_race'} else 'fault.txt'
                    request = lambda: client.put(endpoint+'/file', json={'path': target, 'content': 'new'})
                if mode in {'response_lost','write_race','create_race','delete_race'}:
                    response = request()
                    assert response.status_code == (500 if mode=='response_lost' else 409), response.text
                    # Stop the original server: recovery must not reuse its SQL
                    # session, cache, in-memory owner or successful response.
                    api.proc.terminate(); api.proc.wait(timeout=10)
                else:
                    interrupt(api, request)
            pending = read(PREFIX+'/pending.json')
            document = json.loads(pending) if pending else None
            if mode != 'before_pending':
                assert document is not None
            if mode == 'external':
                put('fault.txt', b'external edit', ContentType='application/external')
            if mode == 'corrupt':
                put(PREFIX+'/objects/'+document['plan']['id']+'/before/0', b'corrupt')
            if mode == 'binding':
                document['plan']['binding']['case_id'] += 1
                put(PREFIX+'/pending.json', json.dumps(document).encode(), ContentType='application/json')
            if mode == 'recovery_crash':
                with fresh(api, settings, 'recovery_crash') as recovery:
                    with httpx.Client(base_url=recovery.base, headers=headers, trust_env=False, timeout=60) as client:
                        interrupt(recovery, lambda: client.get('/api/v1'+api.prefix+'/raw?path=fault.txt'))
                    assert read('fault.txt') == b'old' and read(PREFIX+'/pending.json') is not None
            recovery_mode = mode if mode in {'recovery_race','pointer_race'} else ''
            with fresh(api, settings, recovery_mode) as recovered:
                diagnosis = recovered.call('GET', api.prefix+'/file-transactions')
                if mode == 'before_pending':
                    assert diagnosis['status'] == 'idle'
                    detail = recovered.call('GET', api.prefix+'/file-transactions?include_preparations=true')
                    inventory = detail['preparations']
                    assert inventory['read_only'] and not inventory['has_more'], detail
                    unresolved = [row for row in inventory['entries'] if row['state']=='preparation_retained']
                    assert len(unresolved)==1, inventory
                    row = unresolved[0]
                    assert row['transaction_id'] and row['commit_state']=='not_determined' and row['decision']=='retain', row
                    manifest = read(row['path']+'/manifest.json')
                    assert json.loads(manifest)['plan']['id']==row['transaction_id']
                    assert read('fault.txt')==b'old' and read(PREFIX+'/pending.json') is None
                    again = recovered.call('GET', api.prefix+'/file-transactions?include_preparations=true')
                    assert again==detail and read(row['path']+'/manifest.json')==manifest
                    partial = PREFIX+'/objects/'+str(uuid.uuid4())
                    forged = PREFIX+'/objects/'+str(uuid.uuid4())
                    put(partial+'/after/0', b'partial preparation without manifest')
                    put(forged+'/resolution.json', b'{broken receipt is not proof')
                    expected = [r['path'] for r in inventory['entries']]+[partial,forged]
                    paged = preparation_pages(recovered, api.prefix, expected)
                    assert next(r for r in paged if r['path']==partial)['state']=='preparation_retained'
                    assert next(r for r in paged if r['path']==forged)['state']=='receipt_retained'
                    assert read(partial+'/after/0')==b'partial preparation without manifest'
                    assert read(forged+'/resolution.json')==b'{broken receipt is not proof'
                elif mode in {'corrupt','binding'}:
                    assert diagnosis['status'] == 'journal_invalid'
                else:
                    assert diagnosis['database_committed'] == (mode in COMMITTED_MODES), diagnosis
                conflict = mode in {'external','corrupt','binding','write_race','create_race','delete_race','recovery_race','pointer_race'}
                if conflict:
                    response = recovered.call('GET', api.prefix+'/raw?path=fault.txt', expect=409)
                    assert response['detail']['code'] in {'file_recovery_required','input_stale'}, response
                    if mode == 'pointer_race':
                        assert read(PREFIX+'/pending.json') == b'foreign pointer'
                        assert read('fault.txt') == b'old'
                    elif mode in {'external','write_race','delete_race','recovery_race'}:
                        assert read('fault.txt') == b'external edit'
                    elif mode == 'create_race':
                        assert read('new.txt') == b'external edit'
                    else:
                        assert read('fault.txt') == b'new'
                    assert read(PREFIX+'/pending.json') is not None
                elif mode in DICTIONARY_MODES:
                    current = recovered.call('GET', api.prefix+'/caseDict')
                    if mode not in COMMITTED_MODES:
                        assert current == before_dict and recovered.call('GET', api.prefix) == before_mirror
                        assert {path: read(path) for path in PATHS} == old_dictionary
                    else:
                        assert current['dict_revision'] == before_dict['dict_revision']+1
                        if mode == 'advance_committed':
                            assert current['workflow_state'] == 'V'
                        else:
                            assert current['parsed']['caseMeta']['caseName'] == 'S3-journal'
                        provenance = recovered.call('GET', api.prefix+'/provenance')
                        assert provenance['chain_valid'] and provenance['events'][0]['casedict_sha256'] == current['content_sha256']
                    assert read(PREFIX+'/pending.json') is None
                else:
                    response = recovered.call('GET', api.prefix+'/raw?path=fault.txt', raw=True)
                    assert response == (b'new' if mode=='after_commit' else b'old'), (mode, response)
                    if mode=='create': assert read('new.txt') is None
                    if mode=='metadata':
                        info = store.client.head_object(Bucket=bucket, Key=prefix+'/fault.txt')
                        assert info['ContentType']=='application/test' and info['Metadata']=={'source':'original'} and info['CacheControl']=='no-cache'
                    assert read(PREFIX+'/pending.json') is None
                if not conflict and document is not None:
                    receipt = json.loads(read(PREFIX+'/objects/'+document['plan']['id']+'/resolution.json'))
                    assert receipt['decision'] == ('committed' if mode in COMMITTED_MODES else 'rolled_back')
                    snapshot = {path: read(path) for path in ('fault.txt',)+PATHS}
                    recovered.call('GET', api.prefix+'/tree')
                    assert snapshot == {path: read(path) for path in ('fault.txt',)+PATHS}
            checks.append(mode)
            runs.append({'mode': mode, 'api': str(api.root), 'peer': str(recovered.root), 'bucket': bucket})
        print('PASS '+mode, flush=True)
    return {'count': len(checks), 'checks': checks, 'runs': runs, 'store': str(store.root)}


def legacy():
    with ObjectStore() as store:
        api = IsolatedServer('s3-file-transaction-legacy', app='s3_fixture:app')
        (api.root/'s3_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE+LEGACY_CASE_FIXTURE, encoding='utf8')
        with api:
            create_legacy_case(api,'Existing case on unsupported conditional backend')
            before = api.call('GET', api.prefix+'/caseDict')
            denied = api.call('PUT', api.prefix+'/file', {'path':'new.txt','content':'must not write'}, expect=422)
            assert 'conditional' in denied['detail']
            api.call('PUT', api.prefix+'/caseDict', {'content':before['content']}, expect=422)
            assert api.call('GET', api.prefix+'/caseDict') == before
            api.call('GET', api.prefix+'/raw?path=new.txt', expect=404)
        return {'count': 1, 'checks': ['legacy_conditions_refused_without_live_mutations'], 'store': str(store.root), 'api': str(api.root)}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--modes', nargs='+')
    parser.add_argument('--postgres-only', action='store_true')
    parser.add_argument('--legacy-only', action='store_true')
    args = parser.parse_args()
    output = Path(tempfile.mkdtemp(prefix='s3-file-transactions-evidence-', dir=SERVER.parent.parent/'tmp'))
    if args.legacy_only:
        result = await asyncio.to_thread(legacy)
    else:
        with ObjectStore(executable=RUNTIME, authenticated=True) as store:
            if args.postgres_only:
                import asyncpg
                from sqlalchemy.engine import make_url
                from app.config import Settings
                from workflow_migration_smoke import alembic
                pg = make_url(Settings(_env_file=SERVER/'.env').database_url)
                assert pg.drivername=='postgresql+asyncpg' and pg.host in {'127.0.0.1','localhost','::1'}
                name='tmp_vf_objectjournal_'+uuid.uuid4().hex
                admin=await asyncpg.connect(host=pg.host,port=pg.port or 5432,user=pg.username,password=pg.password,database='postgres',timeout=10)
                created=False
                try:
                    await admin.execute('CREATE DATABASE "'+name+'"'); created=True
                    url=pg.set(database=name).render_as_string(hide_password=False)
                    await asyncio.to_thread(alembic,url,output,'upgrade','head')
                    result=await asyncio.to_thread(run,store,url,args.modes)
                finally:
                    if created:
                        assert await admin.fetchval('SELECT current_database()')=='postgres'
                        await admin.execute('DROP DATABASE "'+name+'"')
                    await admin.close()
                result.update(database='postgresql',temporary_database_removed=True)
            else:
                result=await asyncio.to_thread(run,store,modes=args.modes)
                result['database']='sqlite'
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'status':'passed','count':result['count'],'evidence':str(output)}),flush=True)


if __name__=='__main__': asyncio.run(main())
