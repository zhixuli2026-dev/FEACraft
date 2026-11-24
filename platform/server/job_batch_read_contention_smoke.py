"""Exercise the real batch-list API against a separate SQLite writer."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

import httpx

from app.services import process_identity_native as identity
from testsupport import IsolatedServer


FIXTURE = r'''
import asyncio
import json
import os
from pathlib import Path

os.environ['VIBEFLOW_STORAGE_MEASUREMENT_TIMEOUT_SECONDS'] = '3'

from sqlalchemy import event
from app.db import engine
from app.main import app
from app.routers import jobs
from app.services import process_identity_native

root = Path(__file__).parent
(root/'api-birth.json').write_text(json.dumps(process_identity_native.observe(os.getpid())), encoding='utf8')

@event.listens_for(engine.sync_engine, 'connect')
def short_fixture_writer_timeout(connection, _record):
    cursor = connection.cursor()
    try:
        cursor.execute('PRAGMA busy_timeout = 100')
    finally:
        cursor.close()

original = jobs._batch_out

async def projection(db, manifest, rows):
    waiting = root/'hold-projection'
    if waiting.exists():
        (root/'projection-entered').touch()
        end = asyncio.get_running_loop().time() + 8
        while waiting.exists():
            if asyncio.get_running_loop().time() >= end:
                raise TimeoutError('The test did not release the projection')
            await asyncio.sleep(.025)
    return await original(db, manifest, rows)

jobs._batch_out = projection
'''


@contextmanager
def writer(root):
    with closing(sqlite3.connect(root/'test.sqlite', isolation_level=None, timeout=.5)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            yield connection
        finally:
            connection.rollback()


def poll(fetch, predicate, seconds=12):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = fetch()
        if predicate(value):
            return value
        time.sleep(.05)
    raise TimeoutError(f'Batch read fixture did not reach its fence: {value}')


def verify():
    harness = IsolatedServer('job-batch-read-contention', app='batch_read_fixture:app')
    root = harness.root
    (root/'batch_read_fixture.py').write_text(FIXTURE, encoding='utf8')
    checks = {}
    source = Path(__file__).parent/'app/routers/jobs.py'
    result = {'passed': False, 'checks': checks, 'model_calls': 0, 'mock_llm_calls': 0,
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    print(f'JOB_BATCH_READ_CONTENTION_EVIDENCE {root}', flush=True)
    try:
        with harness as api, ThreadPoolExecutor(max_workers=1) as pool:
            api.create_case('SQLite batch read contention')
            path = api.prefix+'/job-batches'

            def read(endpoint=path, token=None):
                return httpx.get(api.base+'/api/v1'+endpoint, timeout=10, trust_env=False,
                                 headers={'Authorization': 'Bearer '+(token or api.token)})

            def read_after_writer(endpoint, check):
                started = time.monotonic()
                with writer(root):
                    request = pool.submit(read, endpoint)
                    time.sleep(.6)
                    finished_under_lock = request.done()
                response = request.result(timeout=12)
                checks[check] = {'status': response.status_code, 'elapsed': time.monotonic()-started,
                                 'sqlite_busy_timeout_ms': 100, 'responded_under_lock': finished_under_lock}
                return response

            response = read_after_writer(path, 'recovering_writer')
            assert response.status_code == 200 and response.json() == [], response.text

            with writer(root):
                started = time.monotonic()
                timed_out = read()
                elapsed = time.monotonic()-started
            checks['persistent_writer'] = {'status': timed_out.status_code, 'elapsed': elapsed}
            assert timed_out.status_code == 504, timed_out.text
            assert timed_out.json()['detail']['code'] == 'job_batches_read_timeout', timed_out.text
            assert 2.8 <= elapsed < 8, elapsed
            assert read().raise_for_status().json() == []

            sentinel = 'BATCH_VIEW_'+uuid.uuid4().hex
            batch = api.call('POST', api.prefix+'/jobs:batch', {'commands': ['echo '+sentinel],
                            'runtime': 'windows', 'max_concurrent': 1}, expect=202)
            job_id = batch['jobs'][0]['id']
            job = api.wait_job(job_id, timeout=45)
            assert sentinel in job['log_backlog'], job
            batch_id = batch['id']
            completed = read().raise_for_status().json()
            assert len(completed) == 1 and completed[0]['id'] == batch_id, completed
            assert completed[0]['counts']['succeeded'] == 1 and completed[0]['jobs'][0]['id'] == job_id, completed
            detail_path = path+'/'+batch_id
            assert read_after_writer(detail_path, 'detail_recovering_writer').raise_for_status().json() == completed[0]
            checks['committed_batch'] = {'id': batch_id, 'job_id': job_id, 'status': completed[0]['status'],
                                         'actual_command_output': True}

            fence = root/'hold-projection'
            fence.touch()
            try:
                request = pool.submit(read)
                poll(lambda: (root/'projection-entered').exists(), bool)
                with writer(root) as connection:
                    assert connection.in_transaction and not request.done()
                checks['projection_allows_writer'] = True
            finally:
                fence.unlink()
            response = request.result(timeout=12)
            assert response.raise_for_status().json() == completed

            other = api.call('POST', '/auth/register', {'email': uuid.uuid4().hex+'@example.com',
                             'password': 'batch-view-123', 'name': 'Other member'})['token']
            assert read(token=other).status_code == 404 and read(detail_path, token=other).status_code == 404
            checks['cross_user_list_and_detail'] = 404
        birth = json.loads((root/'api-birth.json').read_text(encoding='utf8'))
        poll(lambda: identity.probe(birth), lambda observed: observed['state'] == 'exited')
        checks['api_owner_exit'] = True
        result['passed'] = True
    except BaseException as error:
        result['error'] = type(error).__name__
        result['detail'] = str(error)
        raise
    finally:
        (root/'job-batch-read-contention.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf8')
    print(f'JOB_BATCH_READ_CONTENTION_OK {root}', flush=True)


if __name__ == '__main__':
    verify()
