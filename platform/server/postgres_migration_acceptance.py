"""N7-02: real disposable PostgreSQL, Alembic, HTTP and pg_dump/restore.

SQL fixtures represent legacy records only. No provider, model response or CLI
inference fixture is installed. The new shell job really runs through the API.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import asyncpg
import httpx
from sqlalchemy.engine import make_url

SERVER = Path(__file__).resolve().parent
ROOT = SERVER.parents[1]
BASE = '6fb6c18fb7ee'
PREVIOUS = 'e8c3f09624ab'
HEAD = 'f9d4a10735bc'
PG = Path(r'C:\Program Files\PostgreSQL\16\bin')
EMAIL = 'migration-acceptance@example.com'
PASSWORD = 'isolated-migration-123'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf8')


def files(root):
    assert not any(p.is_symlink() for p in root.rglob('*'))
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob('*')) if p.is_file()}


class API:
    def __init__(self, env, root, label, report):
        self.env, self.root, self.label, self.report = env, root, label, report
        self.process = None
        self.client = None
        self.log = None

    def __enter__(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.log = (self.root / (self.label + '-api.log')).open('w', encoding='utf8')
        self.process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app',
            '--host', '127.0.0.1', '--port', str(self.port)], cwd=SERVER, env=self.env,
            stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.port}', timeout=60, trust_env=False)
        self.observation = {'label': self.label, 'pid': self.process.pid, 'port': self.port,
                            'ready': False, 'exited': False}
        self.report['api_hosts'].append(self.observation)
        try:
            until = time.monotonic() + 60
            while time.monotonic() < until:
                if self.process.poll() is not None:
                    break
                try:
                    if self.client.get('/api/health').status_code == 200:
                        self.observation['ready'] = True
                        break
                except httpx.TransportError:
                    pass
                time.sleep(.2)
            if self.label == 'old-schema':
                assert not self.observation['ready'] and self.process.poll() is not None
                self.log.flush()
                assert 'Database requires a reviewed Alembic upgrade' in (self.root / (self.label + '-api.log')).read_text(encoding='utf8')
            else:
                assert self.observation['ready'], 'API failed to start: ' + self.label
                token = self.call('POST', '/auth/login', {'email': EMAIL, 'password': PASSWORD})['token']
                self.client.headers['Authorization'] = 'Bearer ' + token
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def call(self, method, path, body=None, *, headers=None):
        response = self.client.request(method, '/api/v1' + path, json=body, headers=headers)
        assert response.status_code == 200, (method, path, response.status_code, response.text[:500])
        return response.json()

    def __exit__(self, *_):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=30)
            self.observation['exited'] = self.process.poll() is not None
            self.observation['exit_code'] = self.process.returncode
        if self.client is not None:
            self.client.close()
        if self.log is not None:
            self.log.close()


async def main():
    # Apply the same source-pinned no-LLM gate as the regression executor.
    sys.path.insert(0, str(ROOT / 'platform/tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    from app.config import Settings
    from app.security import hash_password
    configured = make_url(Settings(_env_file=SERVER / '.env').database_url)
    assert configured.drivername == 'postgresql+asyncpg'
    assert configured.host in {'127.0.0.1', 'localhost', '::1'}
    assert all((PG / tool).is_file() for tool in ['pg_dump.exe', 'pg_restore.exe'])
    root = Path(tempfile.mkdtemp(prefix='postgres-migration-', dir=ROOT / 'tmp'))
    print('EVIDENCE ' + str(root), flush=True)
    result = {'passed': False, 'real_model_calls': 0, 'mock_llm_calls': 0,
              'api_hosts': [], 'migration_commands': [], 'databases_removed': [], 'checks': []}
    write(root / 'result.json', result)
    connect = dict(host=configured.host, port=configured.port or 5432,
                   user=configured.username, password=configured.password, timeout=10)
    admin = await asyncpg.connect(**connect, database='postgres')
    names = ['tmp_vf_n702_' + uuid.uuid4().hex for _ in range(3)]
    created = []
    storage = root / 'files'
    storage.mkdir()
    development_audit = files(SERVER / 'data/audit')
    secret = uuid.uuid4().hex + uuid.uuid4().hex

    def env_for(name, fs=storage):
        return dict(os.environ, VIBEFLOW_DATABASE_URL=configured.set(database=name).render_as_string(hide_password=False),
                    VIBEFLOW_STORAGE_BACKEND='fs', VIBEFLOW_STORAGE_FS_ROOT=str(fs),
                    VIBEFLOW_STORAGE_MAINTENANCE_ENABLED='false', VIBEFLOW_DISTRIBUTED_JOBS='false',
                    VIBEFLOW_JWT_SECRET=secret, VIBEFLOW_CODEX_STATE_ROOT=str(root / 'native'),
                    VIBEFLOW_AUDIT_DIR=str(root / 'audit'),
                    VIBEFLOW_INDEX_ROOT=str(root / 'index'), PYTHONIOENCODING='utf-8')

    def command(argv, env, label, *, succeeds=True):
        output = subprocess.run(argv, cwd=SERVER, env=env, capture_output=True,
                                encoding='utf8', errors='replace', timeout=180)
        message = output.stdout + output.stderr
        if configured.password:
            message = message.replace(configured.password, '[REDACTED]')
        (root / (label + '.log')).write_text(message, encoding='utf8')
        assert (output.returncode == 0) == succeeds, label + ' unexpected exit ' + str(output.returncode)
        return message

    async def migrate(name, *args, succeeds=True):
        label = 'migration-' + str(len(result['migration_commands']) + 1)
        text = await asyncio.to_thread(command, [sys.executable, '-m', 'alembic', *args], env_for(name), label, succeeds=succeeds)
        result['migration_commands'].append({'database': name, 'arguments': args, 'expected_success': succeeds, 'log': label + '.log'})
        return text

    async def snapshot(name, *, legacy=False):
        db = await asyncpg.connect(**connect, database=name)
        try:
            tables = ['jobs'] if legacy else [r['tablename'] for r in await db.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
            answer = {}
            for table in tables:
                assert re.fullmatch(r'[a-z_]+', table)
                rows = await db.fetch('SELECT row_to_json(t)::text AS value FROM "' + table + '" t')
                values = sorted(json.dumps(json.loads(r['value']), sort_keys=True, ensure_ascii=False) for r in rows)
                answer[table] = {'rows': len(values), 'sha256': hashlib.sha256('\n'.join(values).encode()).hexdigest()}
            return answer
        finally:
            await db.close()

    async def backup(name, label):
        env = dict(os.environ, PGPASSWORD=configured.password or '', PGCLIENTENCODING='UTF8')
        connection = ['--host', configured.host, '--port', str(configured.port or 5432), '--username', configured.username]
        path = root / (label + '.pgdump')
        await asyncio.to_thread(command, [str(PG / 'pg_dump.exe'), '--format=custom', '--no-owner', '--no-privileges',
            *connection, '--dbname', name, '--file', str(path)], env, label + '-dump')
        target = root / (label + '-files')
        shutil.copytree(storage, target)
        manifest = {'database': name, 'dump_sha256': sha(path), 'dump_bytes': path.stat().st_size,
                    'tables': await snapshot(name), 'files': files(target)}
        write(root / (label + '-manifest.json'), manifest)
        return manifest

    async def restore(name, label):
        env = dict(os.environ, PGPASSWORD=configured.password or '', PGCLIENTENCODING='UTF8')
        await asyncio.to_thread(command, [str(PG / 'pg_restore.exe'), '--no-owner', '--no-privileges', '--exit-on-error',
            '--host', configured.host, '--port', str(configured.port or 5432), '--username', configured.username,
            '--dbname', name, str(root / (label + '.pgdump'))], env, label + '-restore')
        target = root / (label + '-restored-files')
        shutil.copytree(root / (label + '-files'), target)
        manifest = json.loads((root / (label + '-manifest.json')).read_text(encoding='utf8'))
        assert sha(root / (label + '.pgdump')) == manifest['dump_sha256']
        assert await snapshot(name) == manifest['tables']
        assert files(target) == manifest['files']
        return target

    def legacy_http(api):
        rows = api.call('GET', '/cases/1/jobs')
        assert {r['id'] for r in rows} == set(range(1, 6))
        for identifier in range(1, 6):
            job = api.call('GET', '/jobs/' + str(identifier))
            log = api.call('GET', '/jobs/' + str(identifier) + '/log')
            assert job['attempt'] is None
            expected = 'LEGACY_SQL_FIXTURE_' + str(identifier) + '\n旧日志原字节\n'
            assert log['chunk'] == expected and log['offset'] == len(expected.encode())
            assert job['command'] == 'historical metadata only ' + str(identifier)
        return rows

    try:
        for name in names:
            assert re.fullmatch(r'tmp_vf_n702_[0-9a-f]{32}', name) and name != configured.database
            await admin.execute('CREATE DATABASE "' + name + '" TEMPLATE template0')
            created.append(name)
        target, restore_old, restore_current = names
        await migrate(target, 'upgrade', BASE)
        db = await asyncpg.connect(**connect, database=target)
        try:
            async with db.transaction():
                await db.execute("INSERT INTO users(email,name,password_hash,status,created_at) VALUES($1,'migration',$2,'active',now())", EMAIL, hash_password(PASSWORD))
                await db.execute("INSERT INTO workspaces(user_id,name,created_at) VALUES(1,'legacy',now())")
                await db.execute("INSERT INTO cases(workspace_id,name,s3_prefix,workflow_state,dict_revision,case_meta,parse_status,created_at,updated_at) VALUES(1,'legacy','workspaces/1/cases/1/','A',0,'{}','ok',now(),now())")
                for i, status in enumerate(['succeeded', 'failed', 'cancelled', 'queued', 'running'], 1):
                    await db.execute("INSERT INTO jobs(case_id,type,command,status,exit_code,log_s3_key,parsed_summary,created_at,ended_at) VALUES(1,'shell',$1,$2,$3,$4,$5,now(),$6)",
                        'historical metadata only ' + str(i), status, 0 if status == 'succeeded' else None,
                        'legacy-' + str(i) + '.log', '{broken historical JSON' if i == 2 else '{}', None)
                    path = storage / 'workspaces/1/cases/1' / ('legacy-' + str(i) + '.log')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(('LEGACY_SQL_FIXTURE_' + str(i) + '\n旧日志原字节\n').encode())
        finally:
            await db.close()
        original = await snapshot(target, legacy=True)
        original_files = files(storage)
        await backup(target, 'before-upgrade')
        with API(env_for(target), root, 'old-schema', result):
            pass
        assert await snapshot(target, legacy=True) == original
        result['checks'].append('old_schema_API_refuses_implicit_migration')
        await migrate(target, 'upgrade', 'head')
        assert await snapshot(target, legacy=True) == original and files(storage) == original_files
        await migrate(target, 'check')
        await migrate(target, 'downgrade', BASE)
        assert await snapshot(target, legacy=True) == original
        await migrate(target, 'upgrade', PREVIOUS)
        await migrate(target, 'upgrade', 'head')
        await migrate(target, 'upgrade', 'head')
        result['checks'].append('legacy_full_rollback_incremental_reupgrade_and_idempotence')
        with API(env_for(target), root, 'upgraded', result) as api:
            before_views = legacy_http(api)
            assert next(j for j in before_views if j['id'] == 2)['record_issues'][0]['code'] == 'record_unreadable'
            case = api.call('POST', '/workspaces/1/cases', {'name': 'actual post-migration execution'})
            new_job = api.call('POST', '/cases/' + str(case['id']) + '/jobs',
                {'type': 'shell', 'runtime': 'windows', 'command': 'echo N7_ACTUAL_POSTGRES_MIGRATION', 'timeout_seconds': 30},
                headers={'Idempotency-Key': 'n702-' + uuid.uuid4().hex})
            until = time.monotonic() + 90
            while time.monotonic() < until:
                actual = api.call('GET', '/jobs/' + str(new_job['id']))
                stop = (actual.get('attempt') or {}).get('process_stop') or {}
                if actual['status'] == 'succeeded' and stop.get('verified'):
                    break
                time.sleep(.2)
            else:
                api.call('POST', '/jobs/' + str(new_job['id']) + '/cancel')
                raise AssertionError('Actual post-migration shell did not finish with verified stop')
            assert 'N7_ACTUAL_POSTGRES_MIGRATION' in api.call('GET', '/jobs/' + str(new_job['id']) + '/log')['chunk']
            result['actual_shell_job'] = actual
            write(root / 'upgraded-legacy-api.json', before_views)
        assert await snapshot(target, legacy=True) != original  # One actual new Job now coexists.
        db = await asyncpg.connect(**connect, database=target)
        try:
            assert await db.fetchval('SELECT count(*) FROM job_attempts WHERE job_id<=5') == 0
            old_rows = await db.fetch('SELECT row_to_json(t)::text AS value FROM jobs t WHERE id<=5')
            digest = hashlib.sha256('\n'.join(sorted(json.dumps(json.loads(r['value']), sort_keys=True, ensure_ascii=False) for r in old_rows)).encode()).hexdigest()
            assert digest == original['jobs']['sha256']
        finally:
            await db.close()
        result['checks'].append('mixed_history_API_logs_and_actual_new_execution')
        settled = await snapshot(target)
        refused = await migrate(target, 'downgrade', BASE, succeeds=False)
        assert 'RuntimeError' in refused and ('history' in refused.lower() or 'evidence' in refused.lower())
        assert await snapshot(target) == settled
        result['checks'].append('populated_lossy_downgrade_refused_atomically')
        await backup(target, 'mixed-current')
        restored_old_files = await restore(restore_old, 'before-upgrade')
        await migrate(restore_old, 'upgrade', 'head')
        with API(env_for(restore_old, restored_old_files), root, 'restored-old-upgraded', result) as api:
            assert legacy_http(api) == before_views
        restored_current_files = await restore(restore_current, 'mixed-current')
        with API(env_for(restore_current, restored_current_files), root, 'restored-current', result) as api:
            assert legacy_http(api) == before_views
            restored_job = api.call('GET', '/jobs/' + str(new_job['id']))
            assert restored_job['status'] == 'succeeded'
            assert restored_job['attempt']['id'] == actual['attempt']['id']
            assert restored_job['attempt']['process_stop'] == actual['attempt']['process_stop']
            assert 'N7_ACTUAL_POSTGRES_MIGRATION' in api.call('GET', '/jobs/' + str(new_job['id']) + '/log')['chunk']
            write(root / 'restored-current-job.json', restored_job)
        result['checks'].append('old_and_mixed_backups_restore_exact_SQL_files_and_HTTP')
        await migrate(restore_current, 'check')
        result['legacy_jobs'] = 5
        result['backup_manifests'] = ['before-upgrade-manifest.json', 'mixed-current-manifest.json']
        assert files(SERVER / 'data/audit') == development_audit
        result['development_audit_unchanged'] = True
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'message': str(error).replace(configured.password or '\0', '[REDACTED]')}
        raise
    finally:
        try:
            assert await admin.fetchval('SELECT current_database()') == 'postgres'
            for name in reversed(created):
                assert re.fullmatch(r'tmp_vf_n702_[0-9a-f]{32}', name) and name != configured.database
                # No FORCE: an unclosed API must remain visible as a cleanup failure.
                await admin.execute('DROP DATABASE "' + name + '"')
                assert not await admin.fetchval('SELECT 1 FROM pg_database WHERE datname=$1', name)
                result['databases_removed'].append(name)
        except BaseException as error:
            result['passed'] = False
            result['cleanup_failure'] = type(error).__name__
            raise
        finally:
            await admin.close()
            if not all(host['exited'] for host in result['api_hosts']):
                result['passed'] = False
            if files(SERVER / 'data/audit') != development_audit:
                result['passed'] = False
                result['development_audit_unchanged'] = False
            write(root / 'result.json', result)
    print(json.dumps({'passed': result['passed'], 'evidence': str(root), 'checks': len(result['checks']),
                      'actual_shell_jobs': 1, 'real_model_calls': 0, 'mock_llm_calls': 0}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
