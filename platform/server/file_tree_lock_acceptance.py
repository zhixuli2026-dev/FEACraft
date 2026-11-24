"""Observe SQLite writer admission during a live full file-tree scan."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
import sys
import time

from testsupport import IsolatedServer, SERVER


FIXTURE = '''
from contextvars import ContextVar
import json
from pathlib import Path
import time

from app.main import app
from app import storage
from app.services import case_tree, case_write, local_file_edits, s3_live_store

root = Path(__file__).parent
original = storage._fs_entries
original_tree = case_tree.full_entries
original_selected = case_tree.selected_entries
original_remote = s3_live_store.Client._raw_pages
original_publish = local_file_edits.PreparedEdit.publish_files
reading_tree = ContextVar('reading_fixture_tree', default=False)

def pause(name, data):
    witness = root/name
    temporary = witness.with_suffix('.tmp')
    temporary.write_text(json.dumps(data),encoding='utf8')
    temporary.replace(witness)
    until = time.monotonic()+25
    while not (root/('release-'+name)).is_file():
        if time.monotonic()>=until:
            raise TimeoutError('Tree fixture did not release '+name)
        time.sleep(.025)

def collected(name, prefix, rows):
    armed = root/('arm-'+name)
    if armed.exists() and armed.read_text()==prefix:
        armed.unlink()
        pause(name,{'paths':[row['path'] for row in rows or []],
                    'inside_case_transaction':case_write._writes.get() is not None})
    return rows

def full_entries(prefix):
    token = reading_tree.set(True)
    try:
        return collected('full-result',prefix,original_tree(prefix))
    finally:
        reading_tree.reset(token)

def selected_entries(prefix, paths):
    return collected('patch-result',prefix,original_selected(prefix,paths))

def first(prefix, path):
    armed = root/'observe-tree'
    if reading_tree.get() and armed.exists() and armed.read_text()==prefix.rstrip('/'):
        armed.unlink()
        pause('first-entry',{'path':path,'inside_case_transaction':case_write._writes.get() is not None})

def measured(prefix, subdir='', *, budget):
    for entry in original(prefix,subdir,budget=budget):
        first(prefix,entry[0])
        yield entry

def remote(self, bucket, prefix, **kwargs):
    for row in original_remote(self,bucket,prefix,**kwargs):
        first(prefix,row['Key'])
        yield row

def publish(edit, case, pending):
    result = original_publish(edit,case,pending)
    if (root/'arm-install').exists() and any(row['path']=='Ref/transient.txt'
                                            for row in result.document['plan']['files']):
        (root/'arm-install').unlink()
        pause('installed',{'published_before_sql':True})
        raise case_write.CaseWriteConflict('file_conflict','Fixture rolled back the pending edit')
    return result

storage._fs_entries = measured
case_tree.full_entries = full_entries
case_tree.selected_entries = selected_entries
s3_live_store.Client._raw_pages = remote
local_file_edits.PreparedEdit.publish_files = publish
'''


def wait_for(api, name, pending):
    target = api.root/name
    until = time.monotonic()+18
    while not target.exists():
        if pending.done():
            raise AssertionError((f'Request ended before {name}',pending.result()))
        if time.monotonic()>=until:
            raise TimeoutError('No actual tree observation for '+name)
        time.sleep(.02)
    return json.loads(target.read_text(encoding='utf8'))


def arm(api, name, prefix):
    for path in (name,'release-'+name):
        (api.root/path).unlink(missing_ok=True)
    (api.root/('arm-'+name)).write_text(prefix)


def release(api, name):
    (api.root/('release-'+name)).touch()


async def postgres_query(api, statement, *args):
    import asyncpg

    dsn = api.database_url.replace('postgresql+asyncpg:', 'postgresql:', 1)
    connection = await asyncpg.connect(dsn,timeout=8)
    try:
        return await connection.fetchval(statement,*args)
    finally:
        await connection.close()


def writer_available(api, case_id):
    if api.database_url:
        from asyncpg import LockNotAvailableError

        try:
            result = asyncio.run(postgres_query(api,'SELECT next_seq FROM case_event_counters '
                                                'WHERE case_id=$1 FOR UPDATE NOWAIT',case_id))
            assert result is not None
            return True
        except LockNotAvailableError:
            return False

    connection = sqlite3.connect(api.root/'test.sqlite',timeout=.25)
    try:
        connection.execute('BEGIN IMMEDIATE')
        return True
    except sqlite3.OperationalError as error:
        assert 'locked' in str(error).lower(),error
        return False
    finally:
        connection.close()


def set_user_status(api, user_id, status):
    if api.database_url:
        changed = asyncio.run(postgres_query(api,'UPDATE users SET status=$1 WHERE id=$2 RETURNING id',status,user_id))
        assert changed==user_id
    else:
        with sqlite3.connect(api.root/'test.sqlite') as db:
            changed = db.execute('UPDATE users SET status=? WHERE id=?',(status,user_id)).rowcount
            assert changed==1


def verify_races(api, prefix, initial_watermark, result, *, remote):
    tree_route = api.prefix+'/tree'
    path = 'Ref/same-case.txt'
    with ThreadPoolExecutor(max_workers=2) as pool:
        arm(api,'full-result',prefix)
        future = pool.submit(api.call,'GET',tree_route)
        try:
            old = wait_for(api,'full-result',future)
            assert path not in old['paths'] and not old['inside_case_transaction']
            api.put(path,'active tree edit')
        finally:
            release(api,'full-result')
        new = future.result(35)
        assert new['watermark']>initial_watermark
        assert any(row['path']==path for row in new['entries'])
        result['same_case_write_stale_tree_retried'] = True

        arm(api,'patch-result',prefix)
        future = pool.submit(api.call,'POST',tree_route+':patch',{'paths':[path]})
        try:
            old = wait_for(api,'patch-result',future)
            assert old['paths']==[path] and not old['inside_case_transaction']
            api.put(path,'new and longer tree entry')
        finally:
            release(api,'patch-result')
        updated, = future.result(35)['entries']
        assert updated['path']==path and updated['size']==len('new and longer tree entry')
        result['same_case_patch_read_current_size'] = True

        user_id = api.call('GET','/auth/me')['id']
        arm(api,'full-result',prefix)
        future = pool.submit(api.call,'GET',tree_route,expect=403)
        try:
            wait_for(api,'full-result',future)
            set_user_status(api,user_id,'disabled')
        finally:
            release(api,'full-result')
        future.result(35)
        set_user_status(api,user_id,'active')
        result['revoked_user_http'] = 403

        if remote:
            return
        old_watermark = api.call('GET',tree_route)['watermark']
        arm(api,'full-result',prefix)
        (api.root/'first-entry').unlink()
        (api.root/'release-first-entry').unlink()
        (api.root/'observe-tree').write_text(prefix)
        future = pool.submit(api.call,'GET',tree_route)
        try:
            wait_for(api,'first-entry',future)
            (api.root/'arm-install').touch()
            changing = pool.submit(api.call,'PUT',api.prefix+'/file',
                                   {'path':'Ref/transient.txt','content':'will roll back'},expect=409)
            wait_for(api,'installed',changing)
            release(api,'first-entry')
            visible = wait_for(api,'full-result',future)
            assert 'Ref/transient.txt' in visible['paths']
            release(api,'installed')
            assert changing.result(25)['detail']['code']=='file_conflict'
            api.call('POST',api.prefix+'/file-transactions:recover')
        finally:
            for name in ('first-entry','installed','full-result'):
                release(api,name)
        settled = future.result(35)
        assert settled['watermark']==old_watermark
        assert not any(row['path']=='Ref/transient.txt' for row in settled['entries'])
        result['precommit_rollback_excluded_transient_without_advancing_watermark'] = True


def verify(observe_locked, store=None, database_url=None):
    api = IsolatedServer('file-tree-lock',app='tree_fixture:app',database_url=database_url)
    module = api.root/'tree_fixture.py'
    module.write_text('import os\nos.environ.update('+repr(store.settings() if store else {})+')\n'+FIXTURE,
                      encoding='utf8')
    result = {'passed':False,'model_calls':0,'simulated_model_calls':0,
              'backend':'authenticated_seaweedfs' if store else 'fs',
              'database':'postgresql' if database_url else 'sqlite'}
    print('FILE_TREE_LOCK_EVIDENCE',api.root,flush=True)
    try:
        with api:
            case_id = api.create_case('Full file tree scan')['id']
            prefix = api.call('GET',api.prefix)['storage_prefix']
            owner = api.call('GET','/workspaces')[0]['id']
            sibling = api.call('POST',f'/workspaces/{owner}/cases',{'name':'Sibling SQLite writer'})['id']
            if store:
                expected = {f'Ref/large-tree/entry-{index:02}.txt' for index in range(8)}
                for relative in sorted(expected):
                    api.put(relative,'actual object')
            else:
                expected = {f'Ref/large-tree/{page:02}/file-{index:02}.txt'
                            for page in range(16) for index in range(64)}
                base = api.root/'files'/prefix
                for relative in sorted(expected):
                    path = base/relative
                    path.parent.mkdir(parents=True,exist_ok=True)
                    path.write_bytes(b'tree entry')
            (api.root/'observe-tree').write_text(prefix)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(api.call,'GET',api.prefix+'/tree')
                try:
                    checkpoint = wait_for(api,'first-entry',pending)
                    available = writer_available(api,case_id)
                    result['postgres_case_lock_available' if database_url else 'sqlite_writer_available'] = available
                    assert checkpoint['inside_case_transaction'] is observe_locked,checkpoint
                    result.update(checkpoint)
                    result['concurrent_case_saved'] = False
                    if available:
                        route = f'/cases/{sibling}'
                        api.call('PUT',route+'/file',{'path':'Ref/parallel-tree.txt','content':'during listing'})
                        assert api.call('GET',route+'/raw?path=Ref/parallel-tree.txt',raw=True)==b'during listing'
                        result['concurrent_case_saved'] = True
                finally:
                    release(api,'first-entry')
                tree = pending.result(timeout=35)
            paths = {entry['path'] for entry in tree['entries']}
            assert expected <= paths
            assert (checkpoint['path'].startswith(prefix+'/') if store else checkpoint['path'] in paths)
            assert type(tree['watermark']) is int
            assert available is not observe_locked,result
            result.update(actual_seed_files=len(expected),tree_rows=len(paths),watermark=tree['watermark'])
            if not observe_locked:
                verify_races(api,prefix,tree['watermark'],result,remote=bool(store))
            result['passed'] = True
    finally:
        module.unlink(missing_ok=True)
        (api.root/'file-tree-lock.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')


async def main(args):
    from live_llm_terminal_agent_acceptance import disposable_database
    from s3testsupport import ObjectStore

    storage = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                          volume_max=8,authenticated=True) if args.s3 else nullcontext()
    async with disposable_database(args.postgres) as database_url:
        with storage as store:
            await asyncio.to_thread(verify,args.observe_locked,store,database_url)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe-locked',action='store_true')
    parser.add_argument('--s3',action='store_true')
    parser.add_argument('--postgres',action='store_true')
    args = parser.parse_args()
    enforce([Path(__file__)])
    asyncio.run(main(args))
