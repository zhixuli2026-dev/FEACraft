"""Real trash API and SQLite writer while the filesystem inventory holds the trash lock."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from fault_fixture import FileBarrier
from testsupport import IsolatedServer


FIXTURE = '''
from contextlib import contextmanager
from pathlib import Path
from fastapi import Depends
from app.deps import get_current_user
from app.main import app
from app.services import storage_usage, trash
from fault_fixture import FileBarrier

root = Path(__file__).parent
original_scan = storage_usage._scan_fs_tree
original_lock = trash._mutation_lock

def scan(settings, parts, counter, **kwargs):
    if parts[0] == 'workspaces' and (root / 'scan.armed').exists():
        FileBarrier(root, 'inventory-has-lock', timeout=15).wait()
    return original_scan(settings, parts, counter, **kwargs)

@contextmanager
def observed_lock(*args, **kwargs):
    if (root / 'delete.armed').exists():
        FileBarrier(root, 'delete-reached-lock').arrive()
    with original_lock(*args, **kwargs):
        yield

storage_usage._scan_fs_tree = scan
trash._mutation_lock = observed_lock

if hasattr(trash, 'wait_for_mutation'):
    original_wait = trash.wait_for_mutation

    async def wait_for_mutation():
        await original_wait()
        if (root / 'race.armed').exists():
            await FileBarrier(root, 'preflight-finished', timeout=10).wait_async()

    trash.wait_for_mutation = wait_for_mutation

@app.get('/api/v1/test/actual-storage-inventory')
async def inventory(user=Depends(get_current_user)):
    return await storage_usage.measure_user(user.id)
'''


def verify(baseline):
    api = IsolatedServer('trash-wait-budget', app='trash_wait_fixture:app')
    (api.root / 'trash_wait_fixture.py').write_text(FIXTURE, encoding='utf-8')
    result = {'passed': False, 'baseline': baseline, 'real_model_calls': 0, 'mock_llm_calls': 0}
    print('TRASH_WAIT_BUDGET', api.root, flush=True)

    def wait_for(barrier, work):
        deadline = time.monotonic() + 8
        while not barrier.ready.exists():
            if work.done():
                raise AssertionError((barrier.name, work.result()))
            if time.monotonic() >= deadline:
                raise TimeoutError(barrier.name)
            time.sleep(.02)

    database_uri = (api.root / 'test.sqlite').resolve().as_uri() + '?mode=rw'
    try:
        with api:
            api.create_case('Trash inventory deadline')
            api.put('Ref/original.txt', 'real contents before trash')
            with closing(sqlite3.connect(database_uri, uri=True)) as witness:
                witness.execute('CREATE TABLE trash_wait_witness (id INTEGER PRIMARY KEY)')
            (api.root / 'scan.armed').touch()
            locked = FileBarrier(api.root, 'inventory-has-lock')
            reached = FileBarrier(api.root, 'delete-reached-lock')
            with ThreadPoolExecutor(max_workers=2) as pool:
                measuring = pool.submit(api.call, 'GET', '/test/actual-storage-inventory')
                try:
                    wait_for(locked, measuring)
                    (api.root / 'delete.armed').touch()
                    deleting = pool.submit(api.call, 'DELETE', api.prefix)
                    wait_for(reached, deleting)
                    started = time.monotonic()
                    with closing(sqlite3.connect(database_uri, uri=True, timeout=.8)) as writer:
                        try:
                            writer.execute('INSERT INTO trash_wait_witness VALUES (1)')
                            writer.commit()
                            result['sqlite_write_during_inventory'] = True
                        except sqlite3.OperationalError as error:
                            if str(error) != 'database is locked':
                                raise
                            result['sqlite_write_during_inventory'] = False
                    result['sqlite_write_seconds'] = round(time.monotonic() - started, 3)
                    assert result['sqlite_write_during_inventory'] != baseline, result
                    assert not deleting.done(), 'Deletion moved before filesystem inventory released its lock'
                finally:
                    (api.root / 'scan.armed').unlink(missing_ok=True)
                    (api.root / 'delete.armed').unlink(missing_ok=True)
                    locked.release()
                assert measuring.result(timeout=15)['complete']
                assert deleting.result(timeout=15)['trashed']
            assert api.call('GET', '/trash')['cases'][0]['id'] == api.case_id
            assert api.call('POST', api.prefix + ':restore')['restored']
            restored = api.call('GET', api.prefix + '/file?path=Ref/original.txt')
            assert restored['content'] == 'real contents before trash'
            result['restore_preserved_original'] = True
            if not baseline:
                locked.ready.unlink()
                locked.released.unlink()
                (api.root / 'race.armed').touch()
                preflight = FileBarrier(api.root, 'preflight-finished')
                with ThreadPoolExecutor(max_workers=2) as pool:
                    deleting = pool.submit(api.call, 'DELETE', api.prefix, expect=409)
                    try:
                        wait_for(preflight, deleting)
                        (api.root / 'scan.armed').touch()
                        measuring = pool.submit(api.call, 'GET', '/test/actual-storage-inventory')
                        wait_for(locked, measuring)
                        preflight.release()
                        conflict = deleting.result(timeout=3)
                        assert '回收站正在处理其他操作' in conflict['detail']
                        with closing(sqlite3.connect(database_uri, uri=True, timeout=.8)) as writer:
                            writer.execute('INSERT INTO trash_wait_witness VALUES (2)')
                            writer.commit()
                        assert not measuring.done()
                        assert api.call('GET', '/trash')['cases'] == []
                    finally:
                        (api.root / 'scan.armed').unlink(missing_ok=True)
                        (api.root / 'race.armed').unlink(missing_ok=True)
                        preflight.release()
                        locked.release()
                    assert measuring.result(timeout=15)['complete']
                assert api.call('DELETE', api.prefix)['trashed']
                assert api.call('POST', api.prefix + ':restore')['restored']
                assert api.call('GET', api.prefix + '/file?path=Ref/original.txt')['content'] == restored['content']
                result['new_lock_after_preflight_returned_409_and_retried'] = True
        assert api.proc.poll() is not None
        result.update(passed=True, isolated_api_exited=True)
    finally:
        path = api.root / 'trash-wait-result.json'
        path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print('TRASH_WAIT_BUDGET_RESULT', path, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true')
    verify(parser.parse_args().baseline)
