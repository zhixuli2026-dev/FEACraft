"""Actual cross-process index publication, rollback and API readback."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put


FIXTURE = '''
from fastapi import Depends
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import durable_events, indexsvc
import time

original_delivery = durable_events.deliver_pending
async def dispatch(*args, **kwargs):
    if (root/'pause-index-delivery').exists():
        (root/'index-paused').touch()
        return 0
    return await original_delivery(*args, **kwargs)
durable_events.deliver_pending = dispatch

original_sync = indexsvc._sync_index
def pause_api_build(*args, **kwargs):
    result = original_sync(*args, **kwargs)
    flag = root/'pause-api-rebuild'
    if flag.exists():
        (root/'api-rebuild-before-commit').touch()
        deadline = time.monotonic()+20
        while flag.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Test did not release the API rebuild')
            time.sleep(.02)
    return result
indexsvc._sync_index = pause_api_build

@app.get('/api/v1/testing/index-root/{case_id}')
async def index_root(case_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    case = await get_owned_case(case_id, user, db)
    return {'prefix': case.s3_prefix}
'''


BUILDER = '''
from pathlib import Path
import sys
import time
from app.services import indexsvc

case_id, prefix, ready = sys.argv[1:]
sync = indexsvc._sync_index
def paused(*args, **kwargs):
    result = sync(*args, **kwargs)
    Path(ready).touch()
    time.sleep(120)
    raise TimeoutError('Index builder was not stopped by the test')
indexsvc._sync_index = paused
indexsvc.rebuild(int(case_id), prefix)
'''


def scenario(api, *, store=None, **_):
    base = api.prefix
    prefix = api.call('GET', f'/testing/index-root/{api.case_id}')['prefix']
    path = 'Ref/rebuild.txt'
    old, new = ('marker'+uuid.uuid4().hex for _ in range(2))
    (api.root/'pause-index-delivery').touch()
    wait_for((api.root/'index-paused').exists, 'Index event dispatch did not pause')
    put(api, path, old)
    index_path = Path(api.env['VIBEFLOW_INDEX_ROOT'])/f'case-{api.case_id}.sqlite'
    builder_env = {**api.env, **(store.settings() if store is not None else {})}
    evidence = {}

    def search(marker, **kwargs):
        return api.call('GET', base+'/search?q='+marker, **kwargs)

    def interrupt(phase, observed):
        ready = api.root/f'index-{phase}-before-commit'
        with (api.root/f'index-{phase}-process.log').open('w', encoding='utf-8') as log:
            proc = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', '-c', BUILDER,
                str(api.case_id), prefix, str(ready)], env=builder_env,
                stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                wait_for(lambda: ready.exists() or proc.poll() is not None, 'Index builder did not reach commit', 40)
                assert ready.exists() and proc.poll() is None, f'{phase} builder did not hold an open transaction'
                observed()
            finally:
                if proc.poll() is None:
                    proc.kill()
                evidence[phase+'_builder_exit'] = proc.wait(timeout=15)
                (api.root/'index-rebuild.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        assert evidence[phase+'_builder_exit'] != 0

    def assert_unpublished():
        result = api.call('GET', base+'/index/status', expect=(200,500), raw=True)
        expected = {'indexed': False}
        reply = json.loads(result) if result.startswith(b'{') else result.decode('utf-8', 'replace')
        evidence['unpublished_status'] = reply
        assert reply == expected, reply
        assert search(old) == {'ready': False, 'hits': []}

    try:
        assert_unpublished()
        interrupt('first', assert_unpublished)
        assert_unpublished()
        first = api.call('POST', base+'/index:rebuild', {})['building_uuid']
        assert [hit['path'] for hit in search(old)['hits']] == [path]

        if store is not None:
            store.client.put_object(Bucket=store.bucket, Key=prefix+'/'+path, Body=new.encode())
        else:
            target = Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/prefix/path
            target.write_text(new, encoding='utf-8')

        def assert_previous_build():
            started = time.monotonic()
            response = api.call('POST', base+'/index:rebuild', {}, expect=(409,500), raw=True)
            try:
                conflict = json.loads(response)['detail']
            except (ValueError, KeyError):
                conflict = response.decode('utf-8', 'replace')
            evidence['concurrent_rebuild_reply'] = conflict
            evidence['concurrent_rebuild_seconds'] = time.monotonic()-started
            assert isinstance(conflict, dict) and conflict['code'] == 'index_build_busy', conflict
            assert evidence['concurrent_rebuild_seconds'] < 5
            assert api.call('GET', base+'/index/status')['building_uuid'] == first
            assert [hit['path'] for hit in search(old)['hits']] == [path]
            assert not search(new)['hits']

        interrupt('replacement', assert_previous_build)
        with closing(sqlite3.connect(index_path.resolve().as_uri()+'?mode=ro', uri=True)) as conn:
            assert conn.execute("SELECT value FROM meta WHERE key='building_uuid'").fetchone() == (first,)
            assert conn.execute('SELECT digest FROM indexed_content WHERE path=?', (path,)).fetchone() == (
                hashlib.sha256(old.encode()).hexdigest(),)
        legacy = index_path.with_suffix('.building')
        assert not legacy.exists()
        legacy.write_text('earlier-version-build', encoding='ascii')
        refusal = api.call('POST', base+'/index:rebuild', {}, expect=409)['detail']
        assert refusal['code'] == 'index_build_busy'
        stale = time.time()-601
        os.utime(legacy, (stale, stale))
        second = api.call('POST', base+'/index:rebuild', {})['building_uuid']
        assert second != first and [hit['path'] for hit in search(new)['hits']] == [path]
        assert not search(old)['hits']
        assert not legacy.exists()
        assert (index_path.parent/f'{legacy.stem}.stale-{second}').read_text(encoding='ascii') == 'earlier-version-build'

        concurrent_path = 'Ref/concurrent.txt'
        concurrent = 'marker'+uuid.uuid4().hex
        paused = api.root/'pause-api-rebuild'
        paused.touch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            rebuilding = pool.submit(api.call, 'POST', base+'/index:rebuild', {}, expect=409)
            try:
                wait_for(lambda: (api.root/'api-rebuild-before-commit').exists() or rebuilding.done(),
                         'API rebuild did not reach commit', 25)
                assert not rebuilding.done()
                refusal = api.call('POST', base+'/index:rebuild', {}, expect=409)['detail']
                assert refusal['code'] == 'index_build_busy'
                assert api.call('GET', base+'/index/status')['building_uuid'] == second
                assert [hit['path'] for hit in search(new)['hits']] == [path]
                assert put(api, concurrent_path, concurrent)['ok']
                assert not search(concurrent)['hits']
            finally:
                paused.unlink(missing_ok=True)
            refusal = rebuilding.result(timeout=35)['detail']
        assert refusal['code'] == 'index_source_changed'
        with closing(sqlite3.connect(index_path.resolve().as_uri()+'?mode=ro', uri=True)) as conn:
            assert conn.execute("SELECT value FROM meta WHERE key='building_uuid'").fetchone() == (second,)
            assert conn.execute('SELECT 1 FROM files WHERE path=?', (concurrent_path,)).fetchone() is None
        third = api.call('POST', base+'/index:rebuild', {})['building_uuid']
        assert third != second and [hit['path'] for hit in search(concurrent)['hits']] == [concurrent_path]

        stranger = api.call('POST', '/auth/register', {'name':'Foreign rebuild test',
            'email':uuid.uuid4().hex+'@example.com', 'password':'isolated-test-123'})['token']
        headers = {'Authorization': 'Bearer '+stranger}
        api.call('POST', base+'/index:rebuild', {}, expect=404, extra_headers=headers)
        api.call('GET', base+'/index/status', expect=404, extra_headers=headers)
        search(new, expect=404, extra_headers=headers)
        evidence.update(first_build=first, replacement_build=second, final_build=third, rollback_preserved_digest=True,
                        legacy_marker_preserved=True, api_build_aborted_on_source_change=True, case_write_during_build=True,
                        permission_denials=3, backend='s3' if store else 'fs')
    finally:
        (api.root/'pause-index-delivery').unlink(missing_ok=True)
        (api.root/'index-rebuild.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    return evidence


if __name__ == '__main__':
    import asyncio
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE))
