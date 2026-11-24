"""Verify the four local case constructor routes on real separate filesystems."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

import httpx

from cross_volume_file_acceptance import mounted_at
from testsupport import SERVER, IsolatedServer


BODY = 'Ref/large.bin'
SIZE = 12 * 1024 * 1024
FIXTURE = """
import json
import os
from pathlib import Path
import time

from cryptography.fernet import Fernet

evidence = Path(__file__).parent
key = evidence / 'vault.key'
if not key.exists():
    key.write_bytes(Fernet.generate_key())
os.environ['VIBEFLOW_VAULT_KEY_PATH'] = str(key)

from app import storage
from app.main import app
from app.services import backup_restore, case_copy_local, case_import, case_provision


def observed_capture(original):
    def capture(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.name == 'large.bin' and '.vibeflow-file-staging' in path.parts and (evidence/'pause').exists():
            claims = [json.loads(p.read_text()) for p in
                      (storage._fs_root()/'.node-storage-budget').glob('*.json')]
            record = dict(device=path.stat().st_dev, claims=[
                {'bytes': row['bytes'], 'target_root': row.get('target_root')} for row in claims])
            marker = evidence/'staged.json'
            if not marker.exists():
                temporary = marker.with_suffix('.writing')
                temporary.write_text(json.dumps(record))
                temporary.replace(marker)
                for _ in range(200):
                    if not (evidence/'pause').exists():
                        return result
                    time.sleep(.05)
                raise RuntimeError('The independent writer did not finish during staging')
        return result
    return capture


for module in (backup_restore, case_copy_local, case_import):
    module._capture = observed_capture(module._capture)

original_install = case_copy_local.LocalCopy.install
def install(self, plan, rendered):
    body = self.directory/'Ref'/'large.bin'
    if not body.exists():
        return original_install(self, plan, rendered)
    before = body.stat()
    result = original_install(self, plan, rendered)
    target = storage._fs_path(plan['binding']['prefix'], 'Ref/large.bin')
    after = target.stat()
    assert before.st_dev == after.st_dev and before.st_ino == after.st_ino
    with (evidence/'installs.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(kind=plan['kind'], prefix=plan['binding']['prefix'],
                                    device=after.st_dev, inode=after.st_ino))+'\\n')
    return result

case_copy_local.LocalCopy.install = install

original_witness = case_provision.record_witness
async def witness(db, case, plan):
    if (evidence/'exit-before-witness').exists():
        os._exit(83)
    return await original_witness(db, case, plan)

case_provision.record_witness = witness
"""


def archives(root, body, digest, case_dict):
    standard = root / 'case.zip'
    with ZipFile(standard, 'w') as bundle:
        bundle.writestr('0-caseDict/caseDict', case_dict)
        bundle.writestr(BODY, body)
    workspace = root / 'workspace.zip'
    manifest = dict(schema='vibeflow.workspace-backup.v1', workspace={'name': 'Restored'},
                    cases=[dict(id=10, name='Member', workflow_state='A', dict_revision=1,
                                case_meta='{}', parse_status='ok')],
                    files=[dict(path=path, case_id=10, data=base64.b64encode(content).decode(), sha256=sha)
                           for path, content, sha in (
                               (BODY, body, digest),
                               ('0-caseDict/caseDict', case_dict, hashlib.sha256(case_dict).hexdigest()))])
    with ZipFile(workspace, 'w') as bundle:
        bundle.writestr('manifest.json', json.dumps(manifest))
    return standard, workspace


def restart(api):
    api.proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', api.app, '--app-dir', str(SERVER),
                                 '--host', '127.0.0.1', '--port', str(api.port), '--no-access-log'],
                                cwd=api.root, env=api.env, stdout=api.log, stderr=subprocess.STDOUT)
    for _ in range(160):
        if api.proc.poll() is not None:
            break
        try:
            if api.call('GET', '/api/health', api_prefix=False)['ok']:
                return
        except OSError:
            time.sleep(.1)
    raise RuntimeError(f'Could not restart API. See {api.root}/api.log')


def set_limit(database, limit):
    with sqlite3.connect(database, timeout=1) as db:
        db.execute('BEGIN IMMEDIATE')
        [settings] = db.execute('SELECT quotas FROM plans WHERE code=?', ('free',)).fetchone()
        updated = dict(json.loads(settings), storage_mb=limit)
        db.execute('UPDATE plans SET quotas=? WHERE code=?', (json.dumps(updated), 'free'))


def observe_staging(api, invoke, device, target, token, writer, limit):
    """Hold a materialized body and verify another API write can reach SQLite."""
    database = api.root / 'test.sqlite'
    set_limit(database, limit)
    pause = api.root / 'pause'
    staged = api.root / 'staged.json'
    staged.unlink(missing_ok=True)
    pause.touch()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(invoke)
        try:
            until = time.monotonic() + 20
            while not staged.exists() and not future.done() and time.monotonic() < until:
                time.sleep(.025)
            if not staged.exists():
                response = future.result() if future.done() else None
                raise AssertionError(response.text if response is not None else 'No staging observation')
            observation = json.loads(staged.read_text())
            assert observation['device'] == device, observation
            # Import releases its expanded-body claim once the verified files
            # are materialized. Its mounted owned-stage marker remains reserved.
            assert any(row['target_root'] == str(target) for row in observation['claims']), observation
            set_limit(database, limit)
            api.token = writer['token']
            api.call('PUT', f"/cases/{writer['id']}/file", {'path': 'Ref/writer.txt', 'content': str(limit)})
            observation['independent_write'] = 200
            observation['limit_mib'] = limit
        finally:
            api.token = token
            pause.unlink()
        return future.result(), observation


def verify(native):
    api = IsolatedServer('cross-volume-constructor', app='constructor_fixture:app')
    override = f'import os\nos.environ["VIBEFLOW_STORAGE_FS_ROOT"] = {str(native)!r}\n'
    (api.root / 'constructor_fixture.py').write_text(override + FIXTURE, encoding='utf8')
    body = random.Random(20261121).randbytes(SIZE)
    digest = hashlib.sha256(body).hexdigest()
    with api:
        writer = api.create_case('Writer')
        writer['token'] = api.token
        case_dict = api.call('GET', f"/cases/{writer['id']}/raw?path=0-caseDict%2FcaseDict", raw=True)
        standard, workspace_zip = archives(api.root, body, digest, case_dict)
        token = api.call('POST', '/auth/register', {'email': 'mount@example.com',
                         'password': 'isolated-test-123', 'name': 'Mounted owner'})['token']
        api.token = token
        workspace = api.call('GET', '/workspaces')[0]['id']
        with sqlite3.connect(api.root / 'test.sqlite') as db:
            [owner] = db.execute('SELECT user_id FROM workspaces WHERE id=?', (workspace,)).fetchone()
        cases_root = native / 'workspaces' / str(owner) / 'cases'
        cases_root.parent.mkdir(parents=True, exist_ok=True)
        with mounted_at(cases_root, Path('/dev/shm')) as device, httpx.Client(
                base_url=api.base+'/api/v1', headers={'Authorization': 'Bearer '+token},
                trust_env=False, timeout=45) as client:
            scratch = cases_root / '.vibeflow-file-staging'
            report = {'root': str(api.root), 'source_device': native.stat().st_dev,
                      'target_device': device, 'bytes': SIZE, 'sha256': digest}

            def post(route, *, archive=None, fields=None):
                if archive is not None:
                    with archive.open('rb') as stream:
                        return client.post(route, files={'file': (archive.name, stream, 'application/zip')}, data=fields)
                return client.post(route, json=fields)

            def take(route, key, limit, **options):
                response, observation = observe_staging(
                    api, lambda: post(route, **options), device, scratch, token, writer, limit)
                assert response.status_code == 200, response.text[:1500]
                report[key] = observation
                return response.json()

            def assert_body(case_id):
                response = client.get(f'/cases/{case_id}/raw', params={'path': BODY})
                assert response.status_code == 200
                assert len(response.content) == SIZE and hashlib.sha256(response.content).hexdigest() == digest

            imported = take(f'/workspaces/{workspace}/cases:import', 'case_import', 13,
                            archive=standard, fields={'name': 'Import'})
            assert_body(imported['id'])
            case_copy = f"/cases/{imported['id']}:copy"
            copied = take(case_copy, 'case_copy', 25, fields={'name': 'Copy'})
            assert_body(copied['id'])
            cloned = take(f'/workspaces/{workspace}:copy', 'workspace_copy', 49, fields={'name': 'Clone'})
            members = api.call('GET', f"/workspaces/{cloned['id']}/cases")
            assert len(members) == 2
            for case in members:
                assert_body(case['id'])
            restored = take('/workspaces:restore', 'workspace_restore', 61, archive=workspace_zip)
            [member] = api.call('GET', f"/workspaces/{restored['workspace_id']}/cases")
            assert_body(member['id'])
            rejected = post(f'/workspaces/{workspace}/cases:import', archive=standard, fields={'name': 'Too large'})
            assert rejected.status_code == 429, rejected.text[:1000]
            assert sorted(case['name'] for case in api.call('GET', f'/workspaces/{workspace}/cases')) == ['Copy', 'Import']
            assert not list(scratch.iterdir())
            report['quota_rejection'] = rejected.status_code

            set_limit(api.root / 'test.sqlite', 73)
            crash = api.root / 'exit-before-witness'
            crash.touch()
            try:
                post(case_copy, fields={'name': 'Interrupted'})
                raise AssertionError('A reply survived the intentional API exit')
            except httpx.RemoteProtocolError:
                assert api.proc.wait(timeout=8) == 83
            finally:
                crash.unlink()
            installed = [json.loads(line) for line in (api.root/'installs.jsonl').read_text().splitlines()]
            unpublished = native / installed[-1]['prefix']
            assert unpublished.exists() and (unpublished/BODY).read_bytes() == body
            restart(api)
            with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization': 'Bearer '+token},
                              trust_env=False, timeout=45) as recovered:
                result = recovered.post('/case-provisions:recover').raise_for_status().json()
                assert result['recovery_required'] == 0 and not unpublished.exists(), result
                assert len(api.call('GET', f'/workspaces/{workspace}/cases')) == 2
                retried = recovered.post(case_copy, json={'name': 'Interrupted'}).raise_for_status().json()
                assert_body(retried['id'])
            assert not list(scratch.iterdir())
            assert not list((native/'.node-storage-budget').glob('*.json'))
            report.update(crash_recovery=result, installed=installed, staging_and_claims_cleared=True)
            return report


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    enforce([Path(__file__).resolve()])
    with tempfile.TemporaryDirectory(dir='/tmp', prefix='constructor-test-') as root:
        evidence = verify(Path(root))
    evidence.update(passed=True, actual_model_calls=0, simulated_model_calls=0)
    args.report.write_text(json.dumps(evidence, indent=2)+'\n', encoding='utf8')
    print('CROSS_VOLUME_CONSTRUCTORS_PASS', args.report, flush=True)
