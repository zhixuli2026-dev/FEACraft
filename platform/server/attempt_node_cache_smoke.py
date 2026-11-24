"""Fresh API rollback followed by original-node restart against real S3."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from attempt_seal_recovery_smoke import boot
from case_constructor_recovery_smoke import contents, put_foreign, read_plans
from case_provision_smoke import crash
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME
from testsupport import SERVER


def verify(store, database_url=None, modes=None):
    output = Path(tempfile.mkdtemp(prefix='node-cache-evidence-', dir=SERVER.parents[1] / 'tmp'))
    settings = store.settings()
    if not database_url:
        settings['VIBEFLOW_DATABASE_URL'] = 'sqlite+aiosqlite:///' + (output / 'test.sqlite').as_posix()
    checks = []
    for mode in modes or ['normal', 'automatic', 'changed', 'foreign-file', 'foreign-directory', 'hardlink', 'corrupt-receipt', 'remote-returned', 'committed']:
        with boot(settings, database_url) as first:
            case = first.create_case('Node cache ' + mode)
            owner = first.call('GET', '/auth/me')['id']
            prefix = read_plans(first, store, owner)[0]['binding']['prefix']
            first.put('Ref/input.txt', 'CACHE INPUT')
            (first.root / 'mode').write_text('after-commit' if mode == 'committed' else 'after-copy')
            crash(first, lambda: first.call('POST', first.prefix + '/jobs', {
                'command': 'echo CACHE_RECOVERY', 'input_paths': ['Ref/input.txt']}))
            remote = contents(first, store, prefix)
            plan = next(json.loads(raw) for path, raw in remote.items() if path.startswith('.vibeflow/attempt-provisions/pending/'))
            identifier = plan['attempt_id']
            local = first.root / 'files' / prefix / '.vibeflow/attempts' / identifier
            before = {p.relative_to(local).as_posix(): p.read_bytes() for p in local.rglob('*') if p.is_file()}
            assert before and before['inputs/Ref/input.txt'] == b'CACHE INPUT'
            shared = {**settings, 'VIBEFLOW_DATABASE_URL': database_url or settings['VIBEFLOW_DATABASE_URL'],
                      'VIBEFLOW_JWT_SECRET': first.env['VIBEFLOW_JWT_SECRET']}
            endpoint = f'/cases/{case["id"]}/attempt-preparations'
            with boot(shared, database_url, fixture=False) as peer:
                peer.token = first.token
                results = peer.call('POST', endpoint + ':recover')['results']
                assert [row['status'] for row in results] == ['committed' if mode == 'committed' else 'rolled_back'], results
                assert {p.relative_to(local).as_posix(): p.read_bytes() for p in local.rglob('*') if p.is_file()} == before
            if mode == 'changed':
                (local / 'run/Ref/input.txt').write_bytes(b'USER CHANGE')
            elif mode == 'foreign-file':
                (local / 'keep.txt').write_bytes(b'FOREIGN')
            elif mode == 'foreign-directory':
                (local / 'keep-empty').mkdir()
            elif mode == 'hardlink':
                os.link(local / 'run/Ref/input.txt', output / ('link-' + identifier))
            elif mode == 'corrupt-receipt':
                path = '.vibeflow/attempt-provisions/history/' + identifier + '.json'
                receipt = json.loads(contents(first, store, prefix)[path])
                receipt['plan']['binding']['prefix'] = 'foreign/case'
                put_foreign(first, store, prefix, path, json.dumps(receipt).encode())
            elif mode == 'remote-returned':
                put_foreign(first, store, prefix, plan['manifest']['inputs_path'] + '/Ref/input.txt', b'FOREIGN REMOTE')
            protected = {p.relative_to(local).as_posix(): p.read_bytes() for p in local.rglob('*') if p.is_file()}
            remote_before = contents(first, store, prefix)
            restart = {**shared, 'VIBEFLOW_STORAGE_FS_ROOT': first.env['VIBEFLOW_STORAGE_FS_ROOT']}
            if mode == 'automatic':
                restart.update(VIBEFLOW_STORAGE_MAINTENANCE_ENABLED='true', VIBEFLOW_STORAGE_MAINTENANCE_INTERVAL_SECONDS='1')
            with boot(restart, database_url, fixture=False) as old_node:
                old_node.token = first.token
                if mode == 'automatic':
                    deadline = time.monotonic() + 20
                    while local.exists() and time.monotonic() < deadline:
                        time.sleep(.1)
                    assert not local.exists()
                elif mode == 'normal':
                    diagnostic = old_node.call('GET', endpoint)['results']
                    assert [r['status'] for r in diagnostic] == ['node_cache_pending'], diagnostic
                    assert local.exists()
                    result = old_node.call('POST', endpoint + ':recover')['results']
                    assert [r['status'] for r in result] == ['node_cache_removed'], result
                    assert not local.exists()
                else:
                    result = old_node.call('POST', endpoint + ':recover')['results']
                    if mode == 'committed':
                        assert result == [], result
                    else:
                        assert [r['status'] for r in result] == ['recovery_required'], result
                    assert {p.relative_to(local).as_posix(): p.read_bytes() for p in local.rglob('*') if p.is_file()} == protected
                    if mode == 'foreign-directory':
                        assert (local / 'keep-empty').is_dir()
                assert contents(first, store, prefix) == remote_before
                if mode in {'normal', 'automatic'}:
                    assert old_node.call('POST', endpoint + ':recover')['results'] == []
            (output / ('link-' + identifier)).unlink(missing_ok=True)
            checks.append({'mode': mode, 'api': str(first.root), 'peer': str(peer.root), 'restart': str(old_node.root)})
        print('PASS ' + mode, flush=True)
    result = {'passed': True, 'count': len(checks), 'checks': checks,
              'database': 'postgresql' if database_url else 'sqlite'}
    (output / 'checks.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps({'passed': True, 'evidence': str(output)}), flush=True)
    return output, result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--modes', nargs='+')
    args = parser.parse_args()
    with ObjectStore(executable=RUNTIME, volume_max=12) as store:
        if not args.postgres:
            await asyncio.to_thread(verify, store, None, args.modes)
            return
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url = make_url(Settings(_env_file=SERVER / '.env').database_url)
        assert url.host in {'127.0.0.1', 'localhost', '::1'} and url.drivername == 'postgresql+asyncpg'
        name = 'tmp_vf_nodecache_' + uuid.uuid4().hex
        admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
                                      password=url.password, database='postgres', timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "' + name + '"')
            created = True
            temporary = url.set(database=name).render_as_string(hide_password=False)
            logs = Path(tempfile.mkdtemp(prefix='node-cache-migration-', dir=SERVER.parents[1] / 'tmp'))
            await asyncio.to_thread(alembic, temporary, logs, 'upgrade', 'head')
            output, result = await asyncio.to_thread(verify, store, temporary, args.modes)
        finally:
            if created:
                await admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1', name)
                await admin.execute('DROP DATABASE "' + name + '"')
            await admin.close()
        result['temporary_database_removed'] = True
        (output / 'checks.json').write_text(json.dumps(result, indent=2), encoding='utf8')


if __name__ == '__main__':
    asyncio.run(main())
