"""Public geometry save, SQLite admission exhaustion, and a newer concurrent save."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from testsupport import IsolatedServer, SERVER


FIXTURE = '''import asyncio
from contextvars import ContextVar
import json
from pathlib import Path
import sqlite3
import time

from app.main import app
from app.services import file_transactions, storage_admission

root = Path(__file__).parent
original = file_transactions.finish_committed_edit
original_bounds = storage_admission.bounds
forced_wait = ContextVar('fixture_quota_wait', default=None)
workers = set()


def bounds(*, settlement=False):
    if not settlement and (root / 'observe-pre-save').exists():
        (root / 'pre-save-owner-admission').touch()
    end, parent = original_bounds(settlement=settlement)
    limit = forced_wait.get()
    return (min(end, time.monotonic() + limit) if limit is not None and not settlement else end), parent


def hold_writer(loop, ready, hold):
    connection = sqlite3.connect(root / 'test.sqlite', timeout=8)
    try:
        connection.execute('BEGIN IMMEDIATE')
        (root / 'writer-started').touch()
        loop.call_soon_threadsafe(ready.set)
        time.sleep(hold)
        connection.rollback()
        (root / 'writer-released').touch()
    finally:
        connection.close()


async def after_commit(db, case, owner_id):
    await original(db, case, owner_id)
    trigger = root / 'hold-after-edit'
    if trigger.exists():
        options = json.loads(trigger.read_text(encoding='utf8'))
        trigger.unlink()
        if options.get('pause'):
            (root / 'old-save-waiting').touch()
            done = asyncio.get_running_loop().time() + 15
            while not (root / 'allow-old-preview').exists():
                if asyncio.get_running_loop().time() >= done:
                    raise TimeoutError('old save handoff was not released')
                await asyncio.sleep(.02)
            return
        ready = asyncio.Event()
        task = asyncio.create_task(asyncio.to_thread(hold_writer, asyncio.get_running_loop(), ready, options['hold']))
        workers.add(task)
        task.add_done_callback(workers.discard)
        await asyncio.wait_for(ready.wait(), timeout=10)
        forced_wait.set(options.get('timeout'))


file_transactions.finish_committed_edit = after_commit
storage_admission.bounds = bounds
'''


def saved_source(name):
    return f"import cadquery as cq\nresult = {{{name!r}: cq.Workplane('XY').box(2, 3, 4)}}\n"


def wait_for_marker(path):
    limit = time.monotonic() + 12
    while not path.is_file():
        assert time.monotonic() < limit, path.name
        time.sleep(.02)


def exercise(api, report):
    api.create_case('Geometry admission after commit')
    path = 'A-geo/geometry.py'
    url = api.prefix + '/file?path=' + path
    def save(source, before):
        return api.call('PUT', api.prefix + '/file', {
            'path': path, 'content': source,
            'expected_sha256': before['content_sha256'], 'expected_revision': before['dict_revision'],
        })

    def sha(source):
        return hashlib.sha256(source.encode('utf8')).hexdigest()

    def counts():
        with sqlite3.connect((api.root / 'test.sqlite').resolve().as_uri() + '?mode=ro', uri=True) as db:
            return {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                    for table in ('jobs', 'job_attempts', 'artifact_publications')}

    trigger = api.root / 'hold-after-edit'
    source = saved_source('real box')
    digest = sha(source)
    trigger.write_text(json.dumps({'hold': .45}), encoding='utf8')
    started = time.monotonic()
    written = save(source, api.call('GET', url))
    elapsed = time.monotonic() - started
    assert elapsed >= .45 and (api.root / 'writer-released').is_file()
    assert written['ok'] and written['content_sha256'] == digest
    job_id = written['auto_preview_job']
    assert job_id is not None
    job = api.wait_job(job_id)
    assert job['exit_code'] == 0
    scene = api.call('GET', api.prefix + '/geometry/scene')
    assert scene['tolerance'] == .1 and [item['name'] for item in scene['objects']] == ['real box']
    assert api.call('GET', url)['content_sha256'] == digest
    original_counts = counts()
    assert set(original_counts.values()) == {1}, original_counts
    report['short_lock'] = dict(saved_sha256=digest, real_writer_hold_seconds=.45, elapsed_seconds=elapsed,
        job=job_id, actual_exit_code=job['exit_code'], counts=original_counts,
        publication=scene['publication']['id'], tolerance=scene['tolerance'])

    (api.root / 'writer-released').unlink()
    source = saved_source('saved without a preview')
    trigger.write_text(json.dumps({'hold': .65, 'timeout': .25}), encoding='utf8')
    exhausted = save(source, api.call('GET', url))
    wait_for_marker(api.root / 'writer-released')
    assert exhausted['ok'] and exhausted['content_sha256'] == sha(source)
    assert exhausted['auto_preview_job'] is None and exhausted['auto_preview_error']['code'] == 'storage_quota_busy'
    assert api.call('GET', url)['content_sha256'] == sha(source)
    assert counts() == original_counts and api.call('GET', api.prefix + '/geometry/scene?publication_id=' +
        scene['publication']['id'])['objects'][0]['name'] == 'real box'
    report['exhausted_wait'] = {'receipt': exhausted, 'counts': counts(), 'old_scene_preserved': True}

    first, latest = saved_source('overtaken source'), saved_source('latest source')
    trigger.write_text(json.dumps({'pause': True}), encoding='utf8')
    before = api.call('GET', url)
    with ThreadPoolExecutor(max_workers=1) as worker:
        ongoing = worker.submit(save, first, before)
        try:
            wait_for_marker(api.root / 'old-save-waiting')
            current = api.call('GET', url)
            assert current['content_sha256'] == sha(first)
            newer = save(latest, current)
        finally:
            (api.root / 'allow-old-preview').touch()
        older = ongoing.result(timeout=15)
    assert older['ok'] and older['content_sha256'] == sha(first) and older['auto_preview_job'] is None
    assert older['auto_preview_error']['code'] == 'stale_case_files'
    assert newer['ok'] and newer['content_sha256'] == sha(latest)
    latest_job = api.wait_job(newer['auto_preview_job'])
    assert latest_job['exit_code'] == 0 and set(counts().values()) == {2}
    assert api.call('GET', url)['content_sha256'] == sha(latest)
    assert api.call('GET', api.prefix + '/geometry/scene')['objects'][0]['name'] == 'latest source'
    report['overtaken_save'] = {'old_receipt': older, 'new_receipt': newer, 'counts': counts(),
                               'newer_native_exit': latest_job['exit_code']}

    before = api.call('GET', url)
    source = saved_source('pre-save lock released')
    (api.root / 'observe-pre-save').touch()
    with ThreadPoolExecutor(max_workers=1) as worker, closing(sqlite3.connect(api.root / 'test.sqlite')) as holder:
        holder.execute('BEGIN IMMEDIATE')
        ongoing = worker.submit(save, source, before)
        try:
            wait_for_marker(api.root / 'pre-save-owner-admission')
            time.sleep(.45)
            assert not ongoing.done(), 'save ended before the independent writer released admission'
        finally:
            holder.rollback()
        released = ongoing.result(timeout=15)
    assert released['ok'] and released['content_sha256'] == sha(source)
    final_job = api.wait_job(released['auto_preview_job'])
    assert final_job['exit_code'] == 0 and set(counts().values()) == {3}
    assert api.call('GET', api.prefix + '/geometry/scene')['objects'][0]['name'] == 'pre-save lock released'
    report['pre_save_lock'] = {'receipt': released, 'writer_hold_after_admission_seconds': .45,
                              'counts': counts(), 'actual_exit_code': final_job['exit_code']}


def main():
    api = IsolatedServer('geometry-preview-admission', app='preview_admission_fixture:app', access_log=True)
    (api.root / 'preview_admission_fixture.py').write_text(FIXTURE, encoding='utf8')
    print('GEOMETRY_PREVIEW_ADMISSION_EVIDENCE', api.root, flush=True)
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}
    try:
        with api:
            exercise(api, report)
        assert api.proc.poll() is not None
        report['passed'] = True
    except Exception as error:
        report.update(error_type=type(error).__name__, failure=str(error)[:1200])
        raise
    finally:
        (api.root / 'preview-admission.json').write_text(json.dumps(report, indent=2), encoding='utf8')


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    main()
