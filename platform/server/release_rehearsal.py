"""Rehearse an internal release and cold rollback with disposable PostgreSQL and FS storage."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory

from testsupport import IsolatedServer, SERVER

sys.path.insert(0, str(SERVER.parent / 'tools'))
sys.path.insert(0, str(SERVER.parent / 'scripts'))
from deployment_snapshot import command, digest
from release_package import extract


@contextmanager
def postgres(image, identity):
    name = f'vf-j22-release-{identity}'
    password = uuid4().hex
    container = command(['docker', 'run', '--detach', '--rm', '--pull=never', '--name', name,
                         '--publish', '127.0.0.1::5432', '--env', f'POSTGRES_PASSWORD={password}', image]).strip()
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                command(['docker', 'exec', container, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres'])
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        binding = command(['docker', 'port', container, '5432/tcp']).strip()
        host, port = binding.rsplit(':', 1)
        if host != '127.0.0.1':
            raise RuntimeError('Rehearsal PostgreSQL must bind to local loopback')
        yield container, f'postgresql+asyncpg://postgres:{password}@{host}:{int(port)}'
    finally:
        command(['docker', 'rm', '--force', '--volumes', container])


def database_command(container, program, *arguments, **streams):
    return command(['docker', 'exec', '-i', container, program, '-U', 'postgres', *arguments], **streams)


def database_revision(container, name):
    return database_command(container, 'psql', '-d', name, '-tAc',
                            'SELECT version_num FROM alembic_version').strip()


def migrate(code, url):
    env = dict(os.environ, VIBEFLOW_DATABASE_URL=url, VIBEFLOW_STORAGE_BACKEND='fs')
    result = subprocess.run([sys.executable, '-B', '-m', 'alembic', 'upgrade', 'head'],
                            cwd=code, env=env, capture_output=True, encoding='utf8', timeout=120)
    if result.returncode:
        diagnostic = result.stderr.replace(url, '[disposable database]')[-4000:]
        raise RuntimeError(f'Alembic upgrade failed with exit {result.returncode}\n{diagnostic}')


def packaged_revision(code):
    config = Config(str(code / 'alembic.ini'))
    config.set_main_option('script_location', str(code / 'migrations'))
    return ScriptDirectory.from_config(config).get_current_head()


@contextmanager
def application(code, url, state, phase, jwt_secret, report):
    state.mkdir(exist_ok=True)
    api = IsolatedServer('release-' + phase, app='release_fixture:app', database_url=url)
    overrides = {f'VIBEFLOW_{setting}': str(state / field) for setting, field in (
        ('STORAGE_FS_ROOT', 'files'), ('INDEX_ROOT', 'index'), ('AUDIT_DIR', 'audit'),
        ('RUNTIME_CONFIG', 'runtime.json'), ('CODEX_STATE_ROOT', 'cli'),
        ('SKILLS_STORAGE_ROOT', 'skills'), ('VAULT_KEY_PATH', 'vault.key'),
    )}
    overrides['VIBEFLOW_JWT_SECRET'] = jwt_secret
    # Uvicorn runs the actual API from the verified package, in the disposable
    # working directory supplied by IsolatedServer.
    (api.root / 'release_fixture.py').write_text(
        'import os, sys\nfrom pathlib import Path\n'
        f'os.environ.update({overrides!r})\nsys.path.insert(0, {str(code)!r})\n'
        'from app.main import app\nimport app.main as main\n'
        f'assert Path(main.__file__).resolve().is_relative_to(Path({str(code)!r}))\n', encoding='utf8')
    record = {'phase': phase, 'log': str(api.root / 'api.log')}
    report['api'].append(record)
    try:
        with api:
            yield api
    finally:
        record['exited'] = api.proc is not None and api.proc.poll() is not None


def check_readback(api, credentials, case, file, content):
    api.token = api.call('POST', '/auth/login', credentials)['token']
    assert api.call('GET', '/auth/me')['email'] == credentials['email']
    assert api.call('GET', f'/cases/{case}/raw?path={file}', raw=True) == content.encode()


def rehearse(previous_archive, candidate_archive, image, output):
    identity = uuid4().hex
    source_db = 'tmp_vf_release_' + identity
    restored_db = 'tmp_vf_restore_' + identity
    login = {'email': 'release-owner@example.com', 'password': uuid4().hex}
    code = {}
    revisions = {}
    report = {'passed': False, 'checks': [], 'api': [], 'model_calls': 0}
    output.parent.mkdir(parents=True, exist_ok=True)
    report['postgres_image'] = command(['docker', 'image', 'inspect', '--format', '{{.Id}}', image]).strip()
    before = set(command(['docker', 'ps', '--no-trunc', '-q']).splitlines())
    marker = f'Version snapshot {identity}'
    original_file = 'Ref/original.txt'
    candidate_file = 'Ref/after-upgrade.txt'
    jwt_secret = uuid4().hex + uuid4().hex
    try:
        with TemporaryDirectory(prefix='release-work-', dir=output.parent) as directory:
            work = Path(directory).resolve()
            for label, archive in (('previous', previous_archive), ('candidate', candidate_archive)):
                destination = work / label
                extract(archive, destination)
                code[label] = destination / 'platform/server'
                revisions[label] = packaged_revision(code[label])
                report[label] = {'sha256': digest(archive), 'revision': revisions[label]}
            report['package_changed'] = report['previous']['sha256'] != report['candidate']['sha256']
            report['schema_changed'] = revisions['previous'] != revisions['candidate']
            state = work / 'source-state'
            snapshot = work / 'snapshot-state'
            backup = work / 'source.dump'
            subprocess.run([sys.executable, '-B', str(SERVER.parent / 'scripts/init-provider-vault.py'),
                            str(state / 'vault.key')], check=True, capture_output=True, timeout=20)
            with postgres(image, identity) as (container, base):
                report['postgres_container'] = container
                source_url = f'{base}/{source_db}'
                restored_url = f'{base}/{restored_db}'
                for name in (source_db, restored_db):
                    database_command(container, 'psql', '-v', 'ON_ERROR_STOP=1', '-d', 'postgres',
                                     '-c', f'CREATE DATABASE "{name}"')
                migrate(code['previous'], source_url)
                assert database_revision(container, source_db) == revisions['previous']
                with application(code['previous'], source_url, state, 'previous', jwt_secret, report) as api:
                    api.token = api.call('POST', '/auth/register', {**login, 'name': 'Release owner'})['token']
                    workspace = api.call('GET', '/workspaces')[0]['id']
                    case = api.call('POST', f'/workspaces/{workspace}/cases', {'name': 'Rollback case'})['id']
                    api.call('PUT', f'/cases/{case}/file', {'path': original_file, 'content': marker})
                    check_readback(api, login, case, original_file, marker)
                report['checks'].append('previous_package_started_and_persisted_file')

                with backup.open('xb') as stream:
                    database_command(container, 'pg_dump', '-d', source_db, '--format=custom',
                                     '--no-owner', '--no-acl', output=stream)
                shutil.copytree(state, snapshot)
                report['backup_sha256'] = digest(backup)
                assert digest(snapshot / 'vault.key') == digest(state / 'vault.key')
                report['checks'].append('cold_database_and_file_snapshot')

                migrate(code['candidate'], source_url)
                assert database_revision(container, source_db) == revisions['candidate']
                with application(code['candidate'], source_url, state, 'candidate', jwt_secret, report) as api:
                    check_readback(api, login, case, original_file, marker)
                    api.call('PUT', f'/cases/{case}/file', {'path': candidate_file, 'content': marker + '-new'})
                    new_workspace = api.call('POST', '/workspaces', {'name': 'Candidate-only workspace'})['id']
                    check_readback(api, login, case, candidate_file, marker + '-new')
                report['checks'].append('candidate_upgraded_and_wrote_new_state')

                with backup.open('rb') as stream:
                    database_command(container, 'pg_restore', '-d', restored_db, '--no-owner', '--no-acl',
                                     '--exit-on-error', input_file=stream)
                assert database_revision(container, restored_db) == revisions['previous']
                with application(code['previous'], restored_url, snapshot, 'restored', jwt_secret, report) as api:
                    check_readback(api, login, case, original_file, marker)
                    api.call('GET', f'/cases/{case}/raw?path={candidate_file}', expect=404)
                    assert new_workspace not in {row['id'] for row in api.call('GET', '/workspaces')}
                report['checks'].append('previous_database_and_file_snapshot_restored')

                with application(code['candidate'], source_url, state, 'candidate-recheck', jwt_secret, report) as api:
                    check_readback(api, login, case, candidate_file, marker + '-new')
                    assert new_workspace in {row['id'] for row in api.call('GET', '/workspaces')}
                assert database_revision(container, source_db) == revisions['candidate']
                report['checks'].append('candidate_state_survived_rollback')
        report['passed'] = all(api['exited'] for api in report['api'])
    finally:
        try:
            report['original_containers'] = before == set(command(['docker', 'ps', '--no-trunc', '-q']).splitlines())
        finally:
            output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    return report['passed'] and report['original_containers']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--postgres-image', default='postgres:16-alpine')
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    if args.receipt.exists():
        parser.error('Use a new receipt path')
    raise SystemExit(not rehearse(args.previous, args.candidate, args.postgres_image, args.receipt))
