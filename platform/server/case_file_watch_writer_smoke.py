"""Real WebSocket file observations and a concurrent public SQLite file edit."""

import argparse
import asyncio
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
import sys
import time

import aiohttp

from app.services import process_identity_native as identity
from testsupport import IsolatedServer, SERVER


FIXTURE = r'''
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time

os.environ['VIBEFLOW_FILE_WATCH_NATIVE'] = 'false'

from fastapi import Depends
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.routers.workspaces import get_owned_case
from app import storage
from app.services import case_file_watch as watcher, file_transactions, process_identity_native, s3_live_store
from app.services.case_write import _writes, coordinate

root = Path(__file__).parent
(root/'api-birth.json').write_text(json.dumps(process_identity_native.observe(os.getpid())), encoding='utf8')
original = watcher._entries
original_snapshot = watcher.snapshot
original_remote = s3_live_store.Client._raw_pages
current_watch = ContextVar('current_watch',default=None)
watcher.POLL_SECONDS = .5

def pause(trigger, witness, observation):
    working = witness.with_suffix('.tmp')
    working.write_text(json.dumps(observation),encoding='utf8')
    working.replace(witness)
    deadline = time.monotonic() + 24
    while trigger.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Fixture did not release the real case inventory')
        time.sleep(.02)

def pause_inventory(prefix, path):
    trigger = root/'hold-watch'
    if trigger.exists() and trigger.read_text(encoding='utf8').rstrip('/') == prefix.rstrip('/'):
        locked = _writes.get() is not None
        if not (root/'require-locked-inventory').exists() or locked:
            pause(trigger,root/'watch-reading',{'inside_case_transaction':locked,'first_path':path})

def entries(prefix):
    for index, entry in enumerate(original(prefix)):
        if index==0 and not storage._use_s3():
            pause_inventory(prefix,entry[0])
        yield entry

def remote(self,bucket,prefix,**kwargs):
    for index, row in enumerate(original_remote(self,bucket,prefix,**kwargs)):
        if index==0 and current_watch.get()==prefix.rstrip('/'):
            pause_inventory(prefix,row['Key'])
        yield row

def snapshot(prefix):
    token = current_watch.set(prefix.rstrip('/'))
    try:
        fingerprint, entries = original_snapshot(prefix)
    finally:
        current_watch.reset(token)
    trigger = root/'hold-observation'
    if entries is not None and 'Ref/external-watch-proof.txt' in entries and trigger.exists() and trigger.read_text(encoding='utf8') == prefix:
        pause(trigger,root/'observation-ready',entries)
    return fingerprint, entries

watcher._entries = entries
watcher.snapshot = snapshot
s3_live_store.Client._raw_pages = remote

@app.post('/fixture/pending/{case_id}')
async def pending(case_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    async with coordinate(db, case):
        identifier = await file_transactions.write(db, case, {'Ref/not-present-watch-proof.txt': None})
    await db.rollback()
    return {'id': identifier}

@app.get('/fixture/monitor/{case_id}')
async def monitor(case_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    await get_owned_case(case_id, user, db, write=False)
    return {'active': case_id in watcher._monitors}
'''


async def observe_file_event(ws, predicate):
    async with asyncio.timeout(22):
        while True:
            event = await ws.receive_json()
            if event.get('source') == 'file_watch' and predicate(event):
                return event


async def poll(fetch, predicate, seconds=10):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        state = fetch()
        if predicate(state):
            return state
        await asyncio.sleep(.04)
    raise TimeoutError(f'Missing file watch observation: {state}')


def writer_available(root):
    with closing(sqlite3.connect(root/'test.sqlite', isolation_level=None, timeout=.2)) as db:
        try:
            db.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as error:
            return {'acquired': False, 'sqlite': error.sqlite_errorname}
        db.rollback()
        return {'acquired': True}


def file_watch_events(root, case_id):
    uri = (root/'test.sqlite').resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        payloads = [json.loads(row[0]) for row in db.execute('SELECT payload_json FROM durable_events '
                    'WHERE case_id=? AND type=? ORDER BY seq', (case_id, 'files.changed'))]
    return [payload for payload in payloads if payload.get('source') == 'file_watch']


def observed_entries(root, case_id):
    uri = (root/'test.sqlite').resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        row = db.execute('SELECT entries_json FROM case_file_observations WHERE case_id=?',(case_id,)).fetchone()
    return json.loads(row[0])


def share_database_and_case_files(api, store):
    peer = IsolatedServer('case-file-watch-peer', app='file_watch_writer_fixture:app')
    shared = {key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL', 'VIBEFLOW_STORAGE_FS_ROOT')}
    if store is not None:
        shared.update(store.settings())
    (peer.root/'file_watch_writer_fixture.py').write_text('import os\nos.environ.update('+repr(shared)+')\n'+FIXTURE,
                                                         encoding='utf8')
    return peer


async def verify(api, root, report, *, observe_locked, store):
    case = api.create_case('Live file inventory')
    prefix = api.call('GET', api.prefix)['storage_prefix']
    case_root = root/'files'/prefix
    workspace = api.call('GET', '/workspaces')[0]
    peer = api.call('POST', f'/workspaces/{workspace["id"]}/cases', {'name':'Concurrent file save'})
    token = {'Authorization':'Bearer '+api.token}
    versions = {}

    def catalog_etag():
        from app.services.s3_live_store import HEAD

        return store.raw_client.head_object(Bucket=store.bucket,Key=prefix+'/'+HEAD)['ETag']

    def write_external(relative, data):
        if store is None:
            (case_root/relative).write_text(data,encoding='utf8')
            return
        from botocore.exceptions import ClientError

        match = {'IfMatch':versions[relative]} if relative in versions else {'IfNoneMatch':'*'}
        end = time.monotonic()+8
        while True:
            try:
                result = store.client.put_object(Bucket=store.bucket,Key=prefix+'/'+relative,Body=data.encode(),**match)
                versions[relative] = result['ETag']
                return
            except ClientError as error:
                if error.response['ResponseMetadata']['HTTPStatusCode']!=412 or time.monotonic()>=end:
                    raise
                time.sleep(.1)

    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=12)) as client:
        async with client.ws_connect(api.base+'/ws/events', headers={'Authorization': 'Bearer ' + api.token},
                                     params={'case_id': case['id'], 'after': 0}) as ws:
            assert (await ws.receive_json())['type'] == 'ready'
            initial = await observe_file_event(ws, lambda event: event['initial'])
            hold = root/'hold-watch'
            if observe_locked:
                (root/'require-locked-inventory').touch()
            hold.write_text(prefix, encoding='utf8')
            actual_change = 'Ref/changed-inventory-proof.txt'
            await asyncio.to_thread(write_external,actual_change,'actual external change')
            try:
                await poll(lambda: (root/'watch-reading').exists(), bool)
                report['checkpoint'] = json.loads((root/'watch-reading').read_text(encoding='utf8'))
                report['while_fs_inventory_paused'] = await asyncio.to_thread(writer_available, root)
                if not observe_locked:
                    async with client.put(api.base+f'/api/v1/cases/{peer["id"]}/file', headers=token,
                                      json={'path':'Ref/concurrent.txt','content':'actual concurrent save',
                                            'expected_sha256':None}) as save:
                        report['concurrent_save'] = {'status': save.status, 'result':await save.json()}
            finally:
                hold.unlink()
            if observe_locked:
                assert report['checkpoint']['inside_case_transaction'] is True
                assert report['while_fs_inventory_paused']['acquired'] is False
                change = await observe_file_event(ws, lambda event: 'Ref/changed-inventory-proof.txt' in event['paths'])
                report['external_change_published_after_scan'] = change['seq'] > initial['seq']
                assert report['external_change_published_after_scan']
                return
            assert report['while_fs_inventory_paused']['acquired'] and report['concurrent_save']['status'] == 200, report
            assert report['checkpoint']['inside_case_transaction'] is False
            changed = await observe_file_event(ws,lambda event:actual_change in event['paths'])
            assert changed['seq']>initial['seq'] and len(file_watch_events(root,case['id']))==2
            report['changed_inventory_published'] = True

            additional_api = share_database_and_case_files(api,store)
            relative = 'Ref/external-watch-proof.txt'
            hold = root/'hold-observation'
            hold.write_text(prefix,encoding='utf8')
            stale_etag = await asyncio.to_thread(catalog_etag) if store is not None else None
            await asyncio.to_thread(write_external,relative,'old')
            try:
                recorded = root/'observation-ready'
                await poll(recorded.exists,bool)
                older = json.loads(recorded.read_text(encoding='utf8'))
                assert json.loads(older[relative])[1]==3
                with additional_api as peer_api:
                    owner = api.call('GET','/auth/me')
                    peer_token = peer_api.call('POST','/auth/login',
                                              {'email':owner['email'],'password':'isolated-test-123'})['token']
                    current = 'newer external observation'
                    await asyncio.to_thread(write_external,relative,current)
                    async with client.ws_connect(peer_api.base+'/ws/events', params={
                            'case_id':case['id'], 'token':peer_token, 'after':changed['seq']}) as peer_ws:
                        assert (await peer_ws.receive_json())['type'] == 'ready'

                        def created(event):
                            return {'kind':'created','path':relative} in event['changes']

                        later, peer_changed = await asyncio.gather(observe_file_event(ws, created),
                                                                  observe_file_event(peer_ws, created))
                        assert later['seq'] > changed['seq'] and later['paths'] == [relative], later
                        assert later['event_id'] == peer_changed['event_id'] and later['seq'] == peer_changed['seq']
                        report['external_change'] = {'seq':later['seq'], 'paths':later['paths'],
                                                     'both_api_event_id':later['event_id']}
                        hold.unlink()
                        # Wait for two more polling intervals after the stale observer returns.
                        await asyncio.sleep(1.3)
                        assert len(file_watch_events(root,case['id']))==3
                        newest = observed_entries(root,case['id'])
                        assert json.loads(newest[relative])[1]==len(current)
                        report['older_observation_cannot_replace_newer_event'] = True
            finally:
                hold.unlink(missing_ok=True)
            peer_birth = json.loads((additional_api.root/'api-birth.json').read_text(encoding='utf8'))
            await poll(lambda:identity.probe(peer_birth), lambda state:state['state']=='exited')
            report['peer_api_owner_exit'] = True
            (additional_api.root/'file_watch_writer_fixture.py').unlink(missing_ok=True)

            if store is not None:
                from botocore.exceptions import ClientError

                current_etag = await asyncio.to_thread(catalog_etag)
                assert current_etag!=stale_etag
                try:
                    await asyncio.to_thread(store.client.fence,store.bucket,prefix,expected_etag=stale_etag)
                except ClientError as error:
                    assert error.response['ResponseMetadata']['HTTPStatusCode']==412
                else:
                    raise AssertionError('A stale external observation could fence a newer catalog')
                assert await asyncio.to_thread(catalog_etag)==current_etag
                (root/'watch-reading').unlink()
                hold = root/'hold-watch'
                hold.write_text(prefix,encoding='utf8')
                try:
                    await poll(lambda: (root/'watch-reading').exists(),bool)
                    assert await asyncio.to_thread(catalog_etag)==current_etag
                finally:
                    hold.unlink()
                await asyncio.sleep(1.1)
                assert await asyncio.to_thread(catalog_etag)==current_etag
                report.update(stale_catalog_fence_http=412,idle_watcher_preserved_catalog=True)
            else:
                async with client.post(api.base+f'/fixture/pending/{case["id"]}', headers=token) as pending:
                    assert pending.status == 200, await pending.text()
                    identifier = (await pending.json())['id']
                journal = case_root/'.vibeflow/file-transactions'
                resolved = journal/'history'/identifier/'resolution.json'
                await poll(resolved.exists, bool)
                receipt = json.loads(resolved.read_text(encoding='utf8'))
                assert receipt['decision'] == 'rolled_back' and not (journal/'pending').exists(), receipt
                assert len(file_watch_events(root, case['id'])) == 3
                report['unchanged_recovery'] = receipt['decision']

        def monitor_active():
            return api.call('GET', f'/fixture/monitor/{case["id"]}', api_prefix=False)['active']

        await poll(monitor_active, lambda active:not active)
        report['monitor_stopped'] = True


def main(observe_locked, store=None):
    server = IsolatedServer('case-file-watch-writer', app='file_watch_writer_fixture:app')
    root = server.root
    module = root/'file_watch_writer_fixture.py'
    module.write_text('import os\nos.environ.update('+repr(store.settings() if store else {})+')\n'+FIXTURE,
                      encoding='utf8')
    report = {'passed':False,'backend':'authenticated_seaweedfs' if store else 'fs',
              'model_calls':0,'mock_llm_calls':0}
    print(f'CASE_FILE_WATCH_WRITER_EVIDENCE {root}', flush=True)
    try:
        with server as api:
            asyncio.run(verify(api, root, report, observe_locked=observe_locked,store=store))
        birth = json.loads((root/'api-birth.json').read_text(encoding='utf8'))
        asyncio.run(poll(lambda:identity.probe(birth), lambda state:state['state']=='exited'))
        report.update(passed=True, api_owner_exit=True)
    except BaseException as error:
        report.update(error=type(error).__name__, detail=str(error))
        raise
    finally:
        module.unlink(missing_ok=True)
        (root/'case-file-watch-writer.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
    print('CASE_FILE_WATCH_WRITER_OK', flush=True)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe-changed-lock',action='store_true')
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    enforce([Path(__file__)])
    from s3testsupport import ObjectStore

    storage = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                          volume_max=8,authenticated=True) if args.s3 else nullcontext()
    with storage as store:
        main(args.observe_changed_lock,store)
