"""Actual HTTP/manual retention on copies of retained real LaTeX fault databases.

No historical regression suite is imported or run, and no new inference/native
worker is started. The original two real histories are copied read-only.
"""
import argparse
import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid

import httpx
from testsupport import SERVER, IsolatedServer

ROOT = SERVER.parents[1]
FIXTURE = '''from app.main import app
import os
from app.services import process_identity_native as identity
@app.get('/api/v1/test/disposition-process')
async def actual_process():return identity.observe(os.getpid())
@app.post('/api/v1/test/disposition-exit')
async def actual_exit():os._exit(79)
'''
CORE_TABLES = ('cases', 'jobs', 'job_attempts', 'job_execution_heads', 'artifact_publications',
               'artifact_publication_heads', 'native_requests')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connect(path):
    value = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    value.execute('PRAGMA query_only=ON')
    return value


def core(path):
    with connect(path) as db:
        return {table: hashlib.sha256(json.dumps(sorted(db.execute('SELECT * FROM ' + table).fetchall(), key=repr), default=str).encode()).hexdigest() for table in CORE_TABLES}


async def import_history(path, url):
    """Copy actual retained rows into an owned empty PG fixture, not a new run."""
    from datetime import datetime, timezone
    from sqlalchemy import Boolean, DateTime, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.db import Base
    from app import models
    engine = create_async_engine(url)
    try:
        with connect(path) as source:
            source.row_factory = sqlite3.Row
            names = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            async with engine.begin() as db:
                for table in Base.metadata.sorted_tables:
                    if table.name not in names:
                        continue
                    rows = []
                    for original in source.execute('SELECT * FROM ' + table.name):
                        value = {key: original[key] for key in original.keys() if key in table.columns}
                        for column in table.columns:
                            if value.get(column.name) is None:
                                continue
                            if isinstance(column.type, DateTime):
                                stamp = datetime.fromisoformat(value[column.name])
                                value[column.name] = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
                            elif isinstance(column.type, Boolean):
                                value[column.name] = bool(value[column.name])
                        rows.append(value)
                    if rows:
                        await db.execute(table.insert(), rows)
                        if 'id' in table.columns and table.columns.id.type.python_type is int:
                            sequence = await db.scalar(text('SELECT pg_get_serial_sequence(:name, :column)'), {'name': table.name, 'column': 'id'})
                            if sequence:
                                maximum = max(row['id'] for row in rows)
                                await db.execute(text('SELECT setval(CAST(:sequence AS regclass), :maximum, true)'), {'sequence': sequence, 'maximum': maximum})
    finally:
        await engine.dispose()


async def inspect_pg(url):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(url)
    try:
        async with engine.connect() as db:
            hashes = {}
            for table in CORE_TABLES:
                rows = [tuple(row) for row in await db.execute(text('SELECT * FROM ' + table))]
                hashes[table] = hashlib.sha256(json.dumps(sorted(rows, key=repr), default=str).encode()).hexdigest()
            events = [tuple(row) for row in await db.execute(text("SELECT id,payload_json FROM durable_events WHERE type='job.recovery_retained_for_review'"))]
            outbox = [row[0] for row in await db.execute(text("SELECT event_id FROM event_outbox WHERE event_id IN (SELECT id FROM durable_events WHERE type='job.recovery_retained_for_review')"))]
            return hashes, events, outbox
    finally:
        await engine.dispose()


def run(source_folder, label, *, no_checkpoint=True, database_url=None):
    from app.services import process_identity_native as identity
    from app.services.windows_process_scope import ProcessHandle
    source = ROOT / 'tmp' / source_folder / 'test.sqlite'
    original_sha = sha(source)
    api = IsolatedServer('recovery-disposition', app='disposition_fixture:app', database_url=database_url)
    (api.root / 'disposition_fixture.py').write_text(FIXTURE, encoding='utf8')
    target = api.root / 'test.sqlite'
    with connect(source) as old, sqlite3.connect(target) as copy:
        old.backup(copy)
    # These retained create_all test databases predate Alembic stamping. This
    # is an isolated schema projection, not a migration rehearsal or upgrade
    # of the original. Existing business/process columns stay byte-identical.
    from sqlalchemy import create_engine
    from app.db import Base
    from app import models
    engine = create_engine('sqlite:///' + target.as_posix())
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(target) as db:
        for table in ('native_requests', 'native_sessions'):
            columns = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
            if table == 'native_requests' and 'result_json' not in columns:
                db.execute("ALTER TABLE native_requests ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'")
            if 'retirement_event_id' not in columns:
                db.execute('ALTER TABLE ' + table + ' ADD COLUMN retirement_event_id VARCHAR(36) REFERENCES durable_events(id)')
    if database_url:
        asyncio.run(import_history(target, database_url))
    report = {'passed': False, 'label': label, 'original_database': str(source.relative_to(ROOT)),
              'original_database_sha256': original_sha, 'model_calls': 0, 'mock_llm_calls': 0,
              'new_native_worker_calls': 0, 'developer_database_modified': False, 'API_births': [], 'API_exit_witnesses': []}
    report['database'] = 'postgresql' if database_url else 'sqlite'
    review = json.loads((ROOT / 'tmp/N5-manual-disposition-review-v4-20261110.json').read_text())
    report['source_hashes'] = {name: sha(SERVER / name) for name in review['source_hashes']}
    assert report['source_hashes'] == review['source_hashes']
    handles = {}

    def current_core():
        return asyncio.run(inspect_pg(database_url))[0] if database_url else core(target)

    def save():
        (api.root / 'disposition-result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')

    def call(method, path, body=None, *, base=None, token=None, expect=200):
        selected = token or api.token
        headers = {'Authorization': 'Bearer ' + selected} if selected else {}
        with httpx.Client(base_url=(base or api.base) + '/api/v1', headers=headers, trust_env=False, timeout=30) as client:
            response = client.request(method, path, json=body)
            assert response.status_code == expect, ('Unexpected HTTP status', method, path, response.status_code)
            return response.json()

    def remember(base=None):
        birth = call('GET', '/test/disposition-process', base=base)
        handles[json.dumps(birth, sort_keys=True)] = ProcessHandle.query(birth)
        report['API_births'].append(birth)
        save()
        return birth

    def exit_api(birth, base=None):
        try:
            call('POST', '/test/disposition-exit', base=base)
        except httpx.TransportError:
            pass
        handle = handles[json.dumps(birth, sort_keys=True)]
        handle.wait_exit(10)
        assert handle.identity(allow_exited=True) == birth
        if not any(row['birth'] == birth for row in report['API_exit_witnesses']):
            report['API_exit_witnesses'].append({'birth': birth, 'state': 'exited', 'reason': 'retained_creation_handle'})
            save()

    @contextlib.contextmanager
    def peer():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        assert port != 8000
        base = 'http://127.0.0.1:' + str(port)
        with (api.root / 'peer.log').open('w', encoding='utf8') as log:
            proc = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', 'disposition_fixture:app', '--app-dir', str(SERVER), '--host', '127.0.0.1', '--port', str(port), '--no-access-log'], cwd=api.root, env=dict(api.env), stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            birth = None
            try:
                until = time.monotonic() + 30
                while True:
                    assert proc.poll() is None, 'Actual peer launcher exited during startup'
                    try:
                        with httpx.Client(trust_env=False, timeout=1) as client:
                            if client.get(base + '/api/health').json().get('ok'):
                                break
                    except (httpx.HTTPError, ValueError):
                        pass
                    assert time.monotonic() < until, 'Actual peer startup timeout'
                    time.sleep(.1)
                birth = remember(base)
                yield base
            finally:
                try:
                    if birth is not None:
                        exit_api(birth, base)
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)

    try:
        with connect(target) as db:
            row = db.execute('SELECT j.id,j.case_id,u.email FROM jobs j JOIN cases c ON c.id=j.case_id JOIN workspaces w ON w.id=c.workspace_id JOIN users u ON u.id=w.user_id ORDER BY j.id LIMIT 1').fetchone()
        job_id, case_id, email = row
        baseline = current_core()
        with api:
            birth = remember()
            api.token = call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
            assert current_core() == baseline, 'Startup changed retained business or process history'
            path = '/jobs/' + str(job_id)
            plan = call('GET', path + '/recovery-disposition')
            report['diagnosis'] = plan
            body = {'attempt_id': plan['attempt_id'], 'snapshot_sha256': plan['snapshot_sha256'], 'action': 'retain_for_review'}
            if no_checkpoint:
                assert plan['available_actions'] == ['retain_for_review']
                assert 'child_stop_unverified' in plan['issues']
                assert 'content_drain_unverified' in plan['issues']
                stale = dict(body, snapshot_sha256='0' * 64)
                assert call('POST', path + ':retain-for-review', stale, expect=409)['detail']['code'] == 'state_conflict'
                call('POST', path + ':retain-for-review', dict(body, action='mark_drained'), expect=422)
                call('POST', path + ':retain-for-review', dict(body, unknown=True), expect=422)
                with peer() as other:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        futures = [pool.submit(call, 'POST', path + ':retain-for-review', body, base=base) for base in (api.base, other)]
                        results = [future.result() for future in futures]
                    assert results[0] == results[1]
                    result = results[0]
                    assert result['state'] == 'retained_for_manual_review'
                    assert result['content_drained'] is False and result['execution_authorized'] is False
                    report['retention'] = result
                assert call('GET', path)['recovery_disposition']['disposition'] == result
                assert call('GET', path + '/recovery-disposition')['disposition'] == result
                assert current_core() == baseline
                outsider = call('POST', '/auth/register', {'email': uuid.uuid4().hex + '@example.com', 'password': 'isolated-test-123', 'name': 'Unrelated review user'})['token']
                call('GET', path + '/recovery-disposition', token=outsider, expect=404)
                call('POST', path + ':retain-for-review', body, token=outsider, expect=404)
                assert current_core() == baseline
                if database_url:
                    _, events, outbox = asyncio.run(inspect_pg(database_url))
                else:
                    with connect(target) as db:
                        events = db.execute("SELECT id,payload_json FROM durable_events WHERE type='job.recovery_retained_for_review'").fetchall()
                        outbox = [row[0] for row in db.execute('SELECT event_id FROM event_outbox')]
                assert len(events) == 1 and events[0][0] == result['event_id']
                assert outbox.count(result['event_id']) == 1
                report['actual_retention_events'] = len(events)
                exit_api(birth)
            else:
                assert plan['available_actions'] == []
                call('POST', path + ':retain-for-review', body, expect=409)
                assert call('GET', path)['recovery_disposition'] is None
                assert current_core() == baseline
                exit_api(birth)
        if no_checkpoint:
            with api:
                birth = remember()
                api.token = call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
                assert call('POST', path + ':retain-for-review', body) == result
                assert current_core() == baseline
                exit_api(birth)
        assert sha(source) == original_sha
        report['core_business_and_process_history_unchanged'] = current_core() == baseline
        report['original_database_bytes_unchanged'] = True
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)]}
    finally:
        try:
            current = call('GET', '/test/disposition-process')
            if current in report['API_births']:
                exit_api(current)
        except (httpx.HTTPError, AssertionError):
            pass
        for birth in report['API_births']:
            handle = handles[json.dumps(birth, sort_keys=True)]
            try:
                handle.wait_exit(10)
                assert handle.identity(allow_exited=True) == birth
                if not any(row['birth'] == birth for row in report['API_exit_witnesses']):
                    report['API_exit_witnesses'].append({'birth': birth, 'state': 'exited', 'reason': 'retained_creation_handle'})
            except BaseException:
                report['passed'] = False
            handle.close()
        report['original_database_bytes_unchanged'] = sha(source) == original_sha
        assert report['original_database_bytes_unchanged']
        save()
    print('RECOVERY_DISPOSITION_RESULT ' + str(api.root / 'disposition-result.json'), flush=True)
    return report['passed']


async def postgres():
    import asyncpg
    import re
    import tempfile
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    u = make_url(Settings(_env_file=SERVER / '.env').database_url)
    assert u.host in {'localhost', '127.0.0.1', '::1'}
    name = 'tmp_vf_repeat_' + uuid.uuid4().hex
    assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}', name)
    root = Path(tempfile.mkdtemp(prefix='recovery-disposition-postgres-', dir=ROOT / 'tmp'))
    print('RECOVERY_DISPOSITION_POSTGRES ' + str(root), flush=True)
    result = {'passed': False, 'database': name, 'developer_database_modified': False}
    connection = await asyncpg.connect(host=u.host, port=u.port or 5432, user=u.username, password=u.password, database='postgres', timeout=10)
    created = False
    try:
        await connection.execute('CREATE DATABASE "' + name + '"')
        created = True
        url = u.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic, url, root, 'upgrade', 'head')
        result['passed'] = await asyncio.to_thread(run, 'native-execution-gza2q2i0', 'actual_LaTeX_history_on_temporary_PG', database_url=url)
        return 0 if result['passed'] else 1
    finally:
        try:
            if created:
                await connection.execute('DROP DATABASE "' + name + '" WITH (FORCE)')
                result['temporary_database_dropped'] = True
        finally:
            await connection.close()
            (root / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    if args.postgres:
        return asyncio.run(postgres())
    results = [run('native-execution-gza2q2i0', 'original_actual_LaTeX_receipt_failure'),
               run('native-execution-ea57lhjl', 'original_actual_LaTeX_owner_death'),
               run('early-finalizing-axzw4htt', 'completed_current_checkpoint_compatibility', no_checkpoint=False)]
    return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
