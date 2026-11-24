"""J13: adopt an unversioned legacy clone and build the current PostgreSQL schema."""
from __future__ import annotations

import argparse
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
from sqlalchemy.engine import make_url

from postgres_migration_acceptance import API, BASE, PG, ROOT, SERVER, files, sha, write


HEAD = '228635dfe9a1'
LEGACY_DIR = ROOT / 'tmp/postgres-migration-kcgvlllj'
DUMP_SHA = '9c643676f45a6016878b9570c62609cadf6fcf8e1eff6256455b004b67d255e6'
TEMP_NAME = re.compile(r'tmp_vf_j13_[0-9a-f]{32}')
SQL_IDENTIFIER = re.compile(r'[a-z_][a-z0-9_]*')


class SchemaMismatch(Exception):
    pass


async def main(startup):
    sys.path.insert(0, str(ROOT / 'platform/tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])

    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from app.config import Settings

    cfg = Config(str(SERVER / 'alembic.ini'))
    cfg.set_main_option('script_location', str(SERVER / 'migrations'))
    assert ScriptDirectory.from_config(cfg).get_current_head() == HEAD
    configured = make_url(Settings(_env_file=SERVER / '.env').database_url)
    assert configured.drivername == 'postgresql+asyncpg'
    assert configured.host in {'127.0.0.1', 'localhost', '::1'}
    assert configured.database and configured.database != 'postgres'
    legacy_dump = LEGACY_DIR / 'before-upgrade.pgdump'
    legacy_manifest = json.loads((LEGACY_DIR / 'before-upgrade-manifest.json').read_text(encoding='utf8'))
    assert sha(legacy_dump) == DUMP_SHA == legacy_manifest['dump_sha256']
    legacy_files = LEGACY_DIR / 'before-upgrade-files'
    assert files(legacy_files) == legacy_manifest['files']

    root = Path(tempfile.mkdtemp(prefix='j13-postgres-adoption-', dir=ROOT / 'tmp'))
    print('J13_EVIDENCE', root, flush=True)
    data_root = root / 'files'
    shutil.copytree(legacy_files, data_root)
    development_audit = files(SERVER / 'data/audit')
    report = {'passed': False, 'base': BASE, 'head': HEAD, 'startup_contract': startup, 'checks': [], 'api_hosts': [],
              'databases_removed': [], 'real_model_calls': 0, 'mock_llm_calls': 0}
    connect = dict(host=configured.host, port=configured.port or 5432,
                   user=configured.username, password=configured.password, timeout=10)
    cli_connect = ['--host', configured.host, '--port', str(configured.port or 5432),
                   '--username', configured.username]
    cli_env = dict(os.environ, PGPASSWORD=configured.password or '', PGCLIENTENCODING='UTF8')
    jwt_secret = uuid.uuid4().hex + uuid.uuid4().hex
    created: set[str] = set()

    def checked_name(name):
        assert TEMP_NAME.fullmatch(name) and name in created and name != configured.database
        return name

    def env_for(name):
        target = configured.set(database=checked_name(name))
        return dict(os.environ, VIBEFLOW_DATABASE_URL=target.render_as_string(hide_password=False),
                    VIBEFLOW_STORAGE_BACKEND='fs', VIBEFLOW_STORAGE_FS_ROOT=str(data_root),
                    VIBEFLOW_STORAGE_MAINTENANCE_ENABLED='false', VIBEFLOW_DISTRIBUTED_JOBS='false',
                    VIBEFLOW_JWT_SECRET=jwt_secret, VIBEFLOW_CODEX_STATE_ROOT=str(root / 'native'),
                    VIBEFLOW_AUDIT_DIR=str(root / 'audit'), VIBEFLOW_INDEX_ROOT=str(root / 'index'),
                    VIBEFLOW_RUNTIME_CONFIG=str(root / 'runtime.json'), VIBEFLOW_SKILLS_STORAGE_ROOT=str(root / 'skills'),
                    VIBEFLOW_VAULT_KEY_PATH='', PYTHONIOENCODING='utf-8')

    def sanitized(message):
        for value in (configured.password, configured.username):
            if value:
                message = message.replace(value, '[REDACTED]')
        return message

    async def command(argv, env, label):
        def invoke():
            return subprocess.run(argv, cwd=SERVER, env=env, capture_output=True,
                                  encoding='utf8', errors='replace', timeout=180)
        result = await asyncio.to_thread(invoke)
        (root / (label + '.log')).write_text(sanitized(result.stderr), encoding='utf8')
        assert result.returncode == 0, (label, result.returncode)
        return result.stdout

    async def migration(name, label, *args):
        return await command([sys.executable, '-m', 'alembic', *args], env_for(name), label)

    async def schema(name, label):
        text = await command([str(PG / 'pg_dump.exe'), '--schema-only', '--no-owner', '--no-privileges',
                              '--no-comments', '--exclude-table=public.alembic_version', *cli_connect,
                              '--dbname', checked_name(name)], cli_env, label)
        # PostgreSQL emits a random psql restrict token in each text dump.
        stable = '\n'.join(line for line in text.splitlines() if not re.fullmatch(r'\\(?:un)?restrict\s+\S+', line))
        assert stable and 'CREATE TABLE public.jobs' in stable
        return stable

    async def connection(name):
        db = await asyncpg.connect(**connect, database=checked_name(name))
        if await db.fetchval('SELECT current_database()') != name:
            await db.close()
            raise RuntimeError('Unexpected isolation database')
        return db

    async def revision(name):
        db = await connection(name)
        try:
            if await db.fetchval("SELECT to_regclass('public.alembic_version') IS NULL"):
                return None
            return [row['version_num'] for row in await db.fetch('SELECT version_num FROM alembic_version')]
        finally:
            await db.close()

    async def old_rows(name, columns=None):
        db = await connection(name)
        try:
            if columns is None:
                column_map = {}
                for row in await db.fetch("SELECT table_name,column_name FROM information_schema.columns "
                                          "WHERE table_schema='public' AND table_name<>'alembic_version' "
                                          "ORDER BY table_name,ordinal_position"):
                    column_map.setdefault(row['table_name'], []).append(row['column_name'])
            else:
                column_map = columns
            snapshot = {}
            for table, names in column_map.items():
                assert SQL_IDENTIFIER.fullmatch(table) and all(SQL_IDENTIFIER.fullmatch(col) for col in names)
                select = ','.join('"' + col + '"' for col in names)
                rows = await db.fetch('SELECT row_to_json(t)::text AS value FROM (SELECT ' + select +
                                      ' FROM public."' + table + '") t')
                ordered = sorted(json.dumps(json.loads(row['value']), sort_keys=True, ensure_ascii=False) for row in rows)
                snapshot[table] = {'rows': len(ordered), 'sha256': hashlib.sha256('\n'.join(ordered).encode()).hexdigest()}
            return column_map, snapshot
        finally:
            await db.close()

    async def stamp_legacy(name, expected, label):
        assert await revision(name) is None
        actual = await schema(name, label + '-schema')
        if actual != expected:
            raise SchemaMismatch('Legacy structure differs from exact unsigned baseline')
        await migration(name, label + '-stamp', 'stamp', BASE)
        assert await revision(name) == [BASE]

    try:
        admin = await asyncpg.connect(**connect, database='postgres')
    except Exception as error:
        raise RuntimeError(sanitized(str(error))) from None
    try:
        assert await admin.fetchval('SELECT current_database()') == 'postgres'
        reference, adopted, fresh = ('tmp_vf_j13_' + uuid.uuid4().hex for _ in range(3))
        for name in (reference, adopted, fresh):
            assert TEMP_NAME.fullmatch(name) and name != configured.database
            await admin.execute('CREATE DATABASE "' + name + '" TEMPLATE template0')
            created.add(name)

        await migration(reference, 'reference-base', 'upgrade', BASE)
        expected = await schema(reference, 'reference-schema')
        await command([str(PG / 'pg_restore.exe'), '--no-owner', '--no-privileges', '--exit-on-error',
                       *cli_connect, '--dbname', checked_name(adopted), str(legacy_dump)], cli_env, 'restore-old')
        assert await revision(adopted) == [BASE]
        columns, original_rows = await old_rows(adopted)
        assert original_rows == {name: rows for name, rows in legacy_manifest['tables'].items() if name != 'alembic_version'}

        db = await connection(adopted)
        try:
            await db.execute('DROP TABLE public.alembic_version')
            await db.execute('ALTER INDEX public.ix_jobs_case_id RENAME TO j13_fixture_wrong_index')
        finally:
            await db.close()
        try:
            await stamp_legacy(adopted, expected, 'wrong-structure')
        except SchemaMismatch as e:
            report['wrong_structure'] = str(e)
        else:
            raise AssertionError('Wrong structure was stamped')
        assert await revision(adopted) is None and (await old_rows(adopted, columns))[1] == original_rows
        report['checks'].append('mismatched_unversioned_clone_rejected_without_stamping')

        db = await connection(adopted)
        try:
            await db.execute('ALTER INDEX public.j13_fixture_wrong_index RENAME TO ix_jobs_case_id')
        finally:
            await db.close()
        await stamp_legacy(adopted, expected, 'matched-legacy')
        report['checks'].append('matching_unversioned_clone_diff_zero_and_explicit_base_stamp')
        await migration(adopted, 'adopted-upgrade-head', 'upgrade', HEAD)
        await migration(adopted, 'adopted-head-idempotent', 'upgrade', 'head')
        await migration(fresh, 'fresh-head', 'upgrade', HEAD)
        assert await revision(adopted) == await revision(fresh) == [HEAD]
        assert await schema(adopted, 'adopted-head') == await schema(fresh, 'fresh-head-schema')
        await migration(adopted, 'adopted-check', 'check')
        await migration(fresh, 'fresh-check', 'check')
        assert (await old_rows(adopted, columns))[1] == original_rows
        report['checks'].append('direct_current_build_matches_adopted_current_schema_and_original_rows')

        def verify_http(label, rejected=False):
            with API(env_for(adopted), root, label, report) as api:
                if not rejected:
                    cases = api.call('GET', '/cases/1/jobs')
                    assert {row['id'] for row in cases} == set(range(1, 6))
                    for identifier in range(1, 6):
                        job = api.call('GET', '/jobs/' + str(identifier))
                        log = api.call('GET', '/jobs/' + str(identifier) + '/log')
                        expected_log = (data_root / 'workspaces/1/cases/1' / f'legacy-{identifier}.log').read_bytes()
                        assert job['attempt'] is None and log['chunk'].encode() == expected_log
                        assert log['offset'] == len(expected_log)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                with socket.socket() as probe:
                    probe.settimeout(.4)
                    if probe.connect_ex(('127.0.0.1', api.port)) != 0:
                        return
                time.sleep(.2)
            raise AssertionError('Isolated API port still open')

        if startup:
            db = await connection(adopted)
            try:
                await db.execute('DROP TABLE public.alembic_version')
            finally:
                await db.close()
            assert await revision(adopted) is None
            if startup == 'observe-before-fix':
                await asyncio.to_thread(verify_http, 'j13-unversioned-before-fix')
                report['checks'].append('previous_api_served_current_shape_with_no_version_table')
            else:
                await asyncio.to_thread(verify_http, 'old-schema', True)
                error_log = (root / 'old-schema-api.log').read_text(encoding='utf8')
                assert 'has no Alembic revision. Verify and stamp the matching revision' in error_log
                assert (await old_rows(adopted, columns))[1] == original_rows
                report['checks'].append('current_api_refuses_unversioned_complete_schema_without_mutating_business_rows')
            assert await revision(adopted) is None
            assert await schema(adopted, 'unversioned-shape') == await schema(fresh, 'fresh-shape-before-restamp')
            await migration(adopted, 'stamp-matched-current', 'stamp', HEAD)
            assert await revision(adopted) == [HEAD]

        await asyncio.to_thread(verify_http, 'j13-adopted-current')
        assert all((data_root / name).is_file() and sha(data_root / name) == digest
                   for name, digest in legacy_manifest['files'].items())
        assert sha(legacy_dump) == DUMP_SHA and files(legacy_files) == legacy_manifest['files']
        assert files(SERVER / 'data/audit') == development_audit
        report['checks'].append('current_api_reads_all_five_legacy_jobs_and_exact_utf8_logs')
        report['current_legacy_tables'] = len(original_rows)
        report['legacy_rows_manifest'] = original_rows
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__,'message': sanitized(str(error))[:750]}
        raise RuntimeError(report['failure']['message']) from None
    finally:
        try:
            assert await admin.fetchval('SELECT current_database()') == 'postgres'
            for name in sorted(created):
                await admin.execute('DROP DATABASE "' + checked_name(name) + '"')
                assert not await admin.fetchval('SELECT 1 FROM pg_database WHERE datname=$1', name)
                report['databases_removed'].append(name)
        except BaseException as error:
            report['passed'] = False
            report['cleanup_failure'] = type(error).__name__
            raise RuntimeError(sanitized(str(error))) from None
        finally:
            await admin.close()
            write(root / 'result.json', report)
    print('J13_PASS', root / 'result.json', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--startup', choices=('observe-before-fix', 'require-reviewed-revision'))
    asyncio.run(main(parser.parse_args().startup))
