"""Creation installation faults: claims, plans, data and committed receipts."""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import uuid

from testsupport import IsolatedServer, SERVER
from case_provision_smoke import crash

FIXTURE = '''
import os,time
from pathlib import Path
from app.main import app
ROOT=Path(__file__).parent
link=os.link
def hold():
 (ROOT/'ready').touch()
 while not (ROOT/'release').exists():time.sleep(.02)
def linking(source,target):
 result=link(source,target)
 mode=(ROOT/'mode').read_text() if (ROOT/'mode').exists() else ''
 path=Path(target).as_posix()
 if ((mode=='claim' and '/claims/' in path) or
     (mode=='plan' and '/pending/' in path) or
     (mode=='history' and '/history/' in path) or
     (mode in {'file','changed','third-link','automatic','foreign-target'} and path.endswith('/0-caseDict/caseDict'))):hold()
 return result
os.link=linking
'''


def boot(settings=None, database_url=None, fixture=True):
    api = IsolatedServer('provision-stage', app='stage_fixture:app', database_url=database_url)
    (api.root / 'stage_fixture.py').write_text('import os\nos.environ.update(' + repr(settings or {}) + ')\n' +
                                             (FIXTURE if fixture else 'from app.main import app\n'), encoding='utf8')
    return api


def run(modes=None, database_url=None):
    output = Path(tempfile.mkdtemp(prefix='provision-stage-evidence-', dir=SERVER.parents[1] / 'tmp'))
    checks = []
    for mode in modes or ['claim', 'plan', 'file', 'history', 'automatic', 'changed', 'third-link', 'foreign-target']:
        with boot(database_url=database_url) as first:
            source = first.create_case('Original ' + mode)
            first.call('POST', '/case-provisions:recover')
            ws = first.call('GET', '/workspaces')[0]['id']
            owner = first.call('GET', '/auth/me')['id']
            if mode == 'history':
                first.call('POST', f'/workspaces/{ws}/cases', {'name': 'Committed target'})
            (first.root / 'mode').write_text(mode)
            if mode == 'history':
                crash(first, lambda: first.call('POST', '/case-provisions:recover'))
            else:
                crash(first, lambda: first.call('POST', f'/workspaces/{ws}/cases', {'name': 'Interrupted target'}))
            root = Path(first.env['VIBEFLOW_STORAGE_FS_ROOT'])
            stage = root / f'workspaces/{owner}/.case-provisions/staging'
            intents = list(stage.glob('*.json'))
            assert len(intents) == 1, intents
            intent = intents[0]
            record = json.loads(intent.read_text(encoding='utf8'))
            assert record['schema'] == 'vibeflow.provision-stage.v2'
            part = intent.with_suffix('.part')
            installed = root / record['target']['prefix'] / record['target']['path']
            assert os.path.samefile(part, installed) and part.stat().st_nlink == 2
            if mode == 'changed':
                part.write_bytes(b'EXTERNAL EDIT')
            elif mode == 'third-link':
                os.link(part, output / ('third-link-' + record['id']))
            elif mode == 'foreign-target':
                record['target']['prefix'] = 'workspaces/999/cases/999'
                intent.write_text(json.dumps(record), encoding='utf8')
            snapshot = installed.read_bytes()
            shared = {key: first.env[key] for key in ('VIBEFLOW_DATABASE_URL', 'VIBEFLOW_STORAGE_FS_ROOT',
                                                       'VIBEFLOW_INDEX_ROOT', 'VIBEFLOW_JWT_SECRET')}
            if mode == 'automatic':
                shared.update(VIBEFLOW_STORAGE_MAINTENANCE_ENABLED='true', VIBEFLOW_STORAGE_MAINTENANCE_INTERVAL_SECONDS='1')
            with boot(shared, database_url, fixture=False) as peer:
                peer.token = first.token
                if mode == 'automatic':
                    deadline = time.monotonic() + 20
                    while intent.exists() and time.monotonic() < deadline:
                        time.sleep(.1)
                    assert not intent.exists()
                result = peer.call('POST', '/case-provisions:recover')
                protected = mode in {'changed', 'third-link', 'foreign-target'}
                if protected:
                    assert result['recovery_required'] >= 1, result
                    assert intent.exists() and part.exists() and installed.read_bytes() == snapshot
                else:
                    assert result['recovery_required'] == 0, result
                    assert not intent.exists() and not part.exists()
                    if mode in {'claim', 'history'}:
                        assert installed.read_bytes() == snapshot and installed.stat().st_nlink == 1
                    else:
                        assert not installed.exists()
                    assert peer.call('POST', '/case-provisions:recover')['results'] == []
                cases = peer.call('GET', f'/workspaces/{ws}/cases')
                assert len(cases) == (2 if mode == 'history' else 1), cases
                assert any(c['id'] == source['id'] for c in cases)
                if not protected:
                    made = peer.call('POST', f'/workspaces/{ws}/cases', {'name': 'After recovery'})
                    assert peer.call('GET', f'/cases/{made["id"]}/caseDict')['dict_revision'] == 0
                checks.append({'mode': mode, 'api': str(first.root), 'peer': str(peer.root)})
            (output / ('third-link-' + record['id'])).unlink(missing_ok=True)
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
    parser.add_argument('--compatibility', action='store_true')
    args = parser.parse_args()
    if args.compatibility:
        compatibility()
        return
    if not args.postgres:
        await asyncio.to_thread(run, args.modes)
        return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    url = make_url(Settings(_env_file=SERVER / '.env').database_url)
    assert url.host in {'127.0.0.1', 'localhost', '::1'} and url.drivername == 'postgresql+asyncpg'
    name = 'tmp_vf_stage_' + uuid.uuid4().hex
    admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
                                 password=url.password, database='postgres', timeout=10)
    created = False
    try:
        await admin.execute('CREATE DATABASE "' + name + '"')
        created = True
        temporary = url.set(database=name).render_as_string(hide_password=False)
        logs = Path(tempfile.mkdtemp(prefix='stage-migration-', dir=SERVER.parents[1] / 'tmp'))
        await asyncio.to_thread(alembic, temporary, logs, 'upgrade', 'head')
        output, result = await asyncio.to_thread(run, args.modes, temporary)
    finally:
        if created:
            await admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1', name)
            await admin.execute('DROP DATABASE "' + name + '"')
        await admin.close()
    result['temporary_database_removed'] = True
    (output / 'checks.json').write_text(json.dumps(result, indent=2), encoding='utf8')


def compatibility():
    """Old retained stages and v2 pre-installation records on an isolated root."""
    from app.config import get_settings
    from app.services import case_provision as provision, storage_maintenance as maintenance
    from app.services.process_identity_native import observe
    output = Path(tempfile.mkdtemp(prefix='stage-compat-evidence-', dir=SERVER.parents[1] / 'tmp'))
    os.environ.update(VIBEFLOW_STORAGE_FS_ROOT=str(output / 'files'), VIBEFLOW_STORAGE_BACKEND='fs')
    get_settings.cache_clear()
    script = 'import os,json; from app.services.process_identity_native import observe; print(json.dumps(observe(os.getpid())))'
    dead = json.loads(subprocess.check_output([sys.executable, '-X', 'utf8', '-B', '-c', script], cwd=SERVER, text=True))
    owner = provision.owner_prefix(1)
    stages = output / 'files' / owner / 'staging'
    stages.mkdir(parents=True)
    checks = []
    modes = ['intent-only', 'complete-data', 'root-file-linked', 'partial', 'live', 'foreign-process',
             'v1-aged', 'v1-recent', 'v1-linked']
    for mode in modes:
        identifier = uuid.uuid4().hex
        intent, part = stages / (identifier + '.json'), stages / (identifier + '.part')
        target = output / 'files/workspaces/1/cases/1/root.txt'
        data = b'ORIGINAL'
        created = time.time() - 2 * 86400
        record = {'schema': 'vibeflow.provision-stage.v2', 'id': identifier, 'owner': owner,
                  'binding': provision.binding(owner), 'created_at': created, 'file': provision.digest(data),
                  'process': dead, 'target': {'prefix': 'workspaces/1/cases/1', 'path': 'root.txt'}}
        if mode == 'live':
            record['process'] = observe(os.getpid())
        elif mode == 'foreign-process':
            record['process'] = dict(dead, host_id='0' * 64)
        elif mode.startswith('v1-'):
            record['schema'] = 'vibeflow.provision-stage.v1'
            del record['process'], record['target']
            if mode == 'v1-recent':
                created = record['created_at'] = time.time()
        intent.write_text(json.dumps(record), encoding='utf8')
        os.utime(intent, (created, created))
        if mode != 'intent-only':
            part.write_bytes(b'PARTIAL' if mode == 'partial' else data)
        if mode in {'root-file-linked', 'v1-linked'}:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(part, target)
        result = maintenance.clean_staging(1, time.time())
        expected = ('active_writer' if mode == 'live' else 'unverified_writer_retained' if mode == 'foreign-process'
                    else 'retained_recent' if mode == 'v1-recent' else 'recovery_required' if mode in {'partial', 'v1-linked'} else 'cleaned')
        assert len(result) == 1 and result[0]['status'] == expected, (mode, result)
        if expected == 'cleaned':
            assert not intent.exists() and not part.exists()
        else:
            assert intent.exists() and part.exists()
        if target.exists():
            assert target.read_bytes() == data
            target.unlink()
        intent.unlink(missing_ok=True); part.unlink(missing_ok=True)
        checks.append(mode)
    legacy = stages / uuid.uuid4().hex
    legacy.write_bytes(b'UNOWNED')
    assert maintenance.clean_staging(1, time.time())[0]['status'] == 'unowned_staging_retained'
    assert legacy.read_bytes() == b'UNOWNED'
    checks.append('unowned-old-stage-retained')
    result = {'passed': True, 'count': len(checks), 'checks': checks}
    (output / 'checks.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps({'passed': True, 'count': len(checks), 'evidence': str(output)}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
