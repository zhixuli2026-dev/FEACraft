"""Replay genuine retained sandbox artifacts against an API on mounted storage."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path, PureWindowsPath
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid

import httpx

from cross_volume_constructor_acceptance import restart, set_limit
from cross_volume_file_acceptance import mounted_at
from testsupport import SERVER, IsolatedServer

LARGE = 'Ref/archive-large.bin'
EDIT = 'Ref/updated-by-sandbox.txt'
FIXTURE = r"""
import hashlib
import json
import os
from pathlib import Path
import time

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException

evidence = Path(__file__).parent
key = evidence/'vault.key'
if not key.exists():
    key.write_bytes(Fernet.generate_key())
os.environ['VIBEFLOW_VAULT_KEY_PATH'] = str(key)

from app import storage
from app.db import SessionLocal
from app.deps import get_current_user
from app.main import app
from app.routers.terminal import _authorize_terminal
from app.services import file_transactions as files, sandbox_export_publication, tree_transactions
from app.services.case_write import CaseWriteConflict, _writes
from app.services.sandbox_export_execution import SandboxExport

original_capture = files._capture
original_publish = files._publish_prepared
original_move = tree_transactions._move


def capture(path, destination=None, **options):
    result = original_capture(path, destination, **options)
    pause = evidence/'pause'
    if (path.name == 'archive-large.bin' and destination is not None
            and pause.exists() and not (evidence/'observed.json').exists()):
        claims = (storage._fs_root()/'.node-storage-budget').glob('*.json')
        observation = {'lock': _writes.get() is not None, 'device': destination.stat().st_dev,
                       'size': result['size'], 'claims': [json.loads(row.read_text()) for row in claims]}
        temporary = evidence/'observed.tmp'
        temporary.write_text(json.dumps(observation))
        temporary.replace(evidence/'observed.json')
        for _ in range(400):
            if not pause.exists():
                return result
            time.sleep(.025)
        raise TimeoutError('Concurrent save did not finish')
    return result


def move(source, destination, **options):
    result = original_move(source, destination, **options)
    if destination.parent.name == 'before' and (evidence/'exit-first-move').exists():
        os._exit(84)
    return result


def publish_journal(*args):
    if (evidence/'exit-before-journal').exists():
        os._exit(86)
    return original_publish(*args)


files._capture = capture
files._publish_prepared = publish_journal
tree_transactions._move = move
if (evidence/'legacy').exists():
    tree_transactions.staging_root = lambda prefix: None


@app.post('/api/v1/fixture/{case_id}/publish-export')
async def publish_export(case_id: int, user=Depends(get_current_user)):
    # The source artifacts and receipt came from a completed native export.
    metadata = json.loads((evidence/'export.json').read_text())
    exporter = SandboxExport(None)
    exporter.root = Path(metadata['root'])
    exporter.request_id = metadata['request_id']
    exporter.initial = json.loads((exporter.root/'initial.json').read_bytes())
    raw = (exporter.root/'manifest.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == metadata['manifest_sha256']
    exporter.manifest = json.loads(raw)
    async with SessionLocal() as db:
        case = await _authorize_terminal(db, case_id, user.id)
        prefix = case.s3_prefix

    async def authorize(db):
        return await _authorize_terminal(db, case_id, user.id)

    try:
        return await sandbox_export_publication.publish(exporter, metadata['session_id'], case_id, prefix, authorize)
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
"""


def read_receipt(path):
    proof = json.loads((path/'local-tree-proof.json').read_text(encoding='utf8'))
    assert proof['passed'] and not proof['actual_model_calls'] and not proof['mock_llm_calls']
    return proof


def load_fixture(api, native, recovery_source, export_source):
    database = api.root/'test.sqlite'
    with closing(sqlite3.connect((recovery_source/'test.sqlite').as_uri()+'?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(database)) as copied:
            original.backup(copied)
    with sqlite3.connect(database) as db:
        request_id, case_id, user_id, raw = db.execute(
            "SELECT id,case_id,created_by,profile_json FROM native_requests WHERE kind='terminal.recover' "
            "AND phase='succeeded'").fetchone()
        profile = json.loads(raw)
        [email] = db.execute('SELECT email FROM users WHERE id=?', (user_id,)).fetchone()
        workspace, prefix = db.execute('SELECT workspace_id,s3_prefix FROM cases WHERE id=?', (case_id,)).fetchone()
        export_case, export_prefix = db.execute(
            'SELECT id,s3_prefix FROM cases WHERE workspace_id=? AND id<>?', (workspace, case_id)).fetchone()
        opname = PureWindowsPath(profile['root']).name
        recovery = native/'.terminal-recovery'/opname
        shutil.copytree(recovery_source/'files'/'.terminal-recovery'/opname, recovery,
                        ignore=shutil.ignore_patterns('recovery.zip'))
        profile['root'] = str(recovery)
        profile['image_root'] = str(native/'.terminal-export'/PureWindowsPath(profile['image_root']).name)
        db.execute('UPDATE native_requests SET profile_json=? WHERE id=?', (json.dumps(profile), request_id))
    with closing(sqlite3.connect((export_source/'test.sqlite').as_uri()+'?mode=ro', uri=True)) as db:
        [raw] = db.execute("SELECT payload_json FROM durable_events WHERE type='terminal.export_published'").fetchone()
        published = json.loads(raw)
    [source] = (export_source/'files'/'.terminal-export').iterdir()
    exported = native/'.retained-test-export'
    exported.mkdir()
    shutil.copytree(source/'files', exported/'files')
    for name in ('initial.json', 'manifest.json', 'complete.json'):
        shutil.copyfile(source/name, exported/name)
    (api.root/'export.json').write_text(json.dumps(dict(published, root=str(exported))), encoding='utf8')
    return dict(email=email, workspace=workspace, case_id=case_id, case_root=native/prefix,
                export_case=export_case, export_case_root=native/export_prefix,
                recovery=recovery, exported=exported,
                endpoint=f"/terminal/sessions/{profile['session_id']}/recoveries/{request_id}")


def materialize(source, root, *, export=False):
    initial = json.loads((source/'initial.json').read_bytes())
    for path in sorted(initial['directories'], key=lambda p: p.count('/')):
        (root/path).mkdir()
    for path, expected in initial['files'].items():
        stage = source/'files'/path
        target = root/path
        if path == EDIT:
            target.write_bytes(b'before')
        elif export and path == '0-caseDict/caseDict':
            data = stage.read_bytes().removesuffix(b'\n// actual local tree export\n')
            target.write_bytes(data)
        else:
            shutil.copyfile(stage, target)
        with target.open('rb') as stream:
            assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected['sha256'], path
    for path, mode in initial['modes'].items():
        (root/path).chmod(mode)
    return initial['files'][LARGE]


def concurrent(api, operation, writer, scratch, device, expect_lock):
    probe = api.root/'observed.json'
    probe.unlink(missing_ok=True)
    pause = api.root/'pause'
    pause.touch()
    with ThreadPoolExecutor(max_workers=1) as threads:
        request = threads.submit(operation)
        try:
            until = time.monotonic()+25
            while not probe.exists() and not request.done() and time.monotonic() < until:
                time.sleep(.025)
            if not probe.exists():
                raise AssertionError(request.result().text if request.done() else 'Staging did not reach the copied file')
            observation = json.loads(probe.read_bytes())
            assert observation['device'] == device and observation['size'] == 40*1024*1024
            assert observation['lock'] is expect_lock, observation
            if expect_lock:
                with sqlite3.connect(api.root/'test.sqlite', timeout=.2) as db:
                    try:
                        db.execute('BEGIN IMMEDIATE')
                    except sqlite3.OperationalError as error:
                        assert 'locked' in str(error)
                    else:
                        raise AssertionError('Legacy staging did not hold the writer')
            else:
                assert any(row.get('target_root') == str(scratch) for row in observation['claims']), observation
                with sqlite3.connect(api.root/'test.sqlite', timeout=.5) as db:
                    db.execute('BEGIN IMMEDIATE')
                api.call('PUT', f'/cases/{writer}/file', {'path':'Ref/during.txt','content':'concurrent'})
                observation['independent_write'] = 200
        finally:
            pause.unlink()
        observation.pop('claims')
        return request.result(), observation


def terminate_during(api, marker, operation, code):
    marker.touch()
    try:
        response = operation()
        raise AssertionError(f'API returned {response.status_code} instead of exit {code}: {response.text}')
    except httpx.RemoteProtocolError:
        assert api.proc.wait(timeout=8) == code
    finally:
        marker.unlink()


def verify(native, sources, baseline):
    api = IsolatedServer('cross-volume-tree', app='tree_fixture:app')
    original, exported = sources
    proofs = [read_receipt(path) for path in sources]
    assert proofs[1]['verified_native_stop']
    item = load_fixture(api, native, original, exported)
    override = f'import os\nos.environ["VIBEFLOW_STORAGE_FS_ROOT"]={str(native)!r}\n'
    (api.root/'tree_fixture.py').write_text(override+FIXTURE, encoding='utf8')
    if baseline:
        (api.root/'legacy').touch()
    item['case_root'].parent.mkdir(parents=True)
    with mounted_at(item['case_root'], Path('/dev/shm')) as dev, mounted_at(item['export_case_root'], Path('/dev/shm')):
        expected = materialize(item['recovery'], item['case_root'])
        with api:
            api.token = api.call('POST', '/auth/login',
                                 {'email': item['email'], 'password': 'isolated-test-123'})['token']
            writer = api.call('POST', f"/workspaces/{item['workspace']}/cases", {'name':'Independent writer'})['id']
            route = item['endpoint']
            case = f"/cases/{item['case_id']}"
            selection = {'paths':[EDIT], 'semantics':'sandbox_tree_changes'}
            operation = dict(selection, confirm=True, request_id=str(uuid.uuid4()),
                             plan_sha256=api.call('POST', route+'/restore-plan', selection)['plan_sha256'])
            post = lambda path, **options: httpx.post(api.base+'/api/v1'+path,
                headers={'Authorization':'Bearer '+api.token}, timeout=65, trust_env=False, **options)
            scratch = item['case_root']/'.vibeflow-file-staging'
            report = dict(source_device=native.stat().st_dev, target_device=dev, readback=expected, baseline=baseline)
            if baseline:
                response, observation = concurrent(api, lambda: post(route+'/restore', json=operation),
                                                  writer, scratch, dev, True)
                assert response.status_code == 200, response.text
                return dict(report, restore=observation)

            nested = item['case_root']/'Ref'/'nested-mount'
            with mounted_at(nested, Path('/tmp')):
                guard = nested/'retain.txt'
                guard.write_text('mounted data')
                nested_plan = api.call('POST', route+'/restore-plan', selection)
                denied = post(route+'/restore', json=dict(operation, request_id=str(uuid.uuid4()),
                                                           plan_sha256=nested_plan['plan_sha256']))
                assert denied.status_code == 409 and denied.json()['detail']['code'] == 'tree_mount_boundary', denied.text
                assert guard.read_text() == 'mounted data'
                assert (item['case_root']/EDIT).read_bytes() == b'before'
            nested.rmdir()
            report['nested_mount_preserved'] = True

            set_limit(api.root/'test.sqlite', 98)
            def restore():
                return post(route+'/restore', json=operation)

            terminate_during(api, api.root/'exit-before-journal', restore, 86)
            [abandoned] = scratch.iterdir()
            assert (abandoned/'journal/after/0/archive-large.bin').stat().st_size == expected['size']
            restart(api)
            terminate_during(api, api.root/'exit-first-move', restore, 84)
            assert not abandoned.exists()
            pending = item['case_root']/'.vibeflow/tree-transactions/pending'
            transaction = json.loads((pending/'manifest.json').read_bytes())['plan']['id']
            assert not (item['case_root']/EDIT).exists()
            assert (pending/'before/0/archive-large.bin').stat().st_size == expected['size']
            restart(api)
            api.call('GET', route+'/restores/'+operation['request_id'], expect=404)
            api.call('POST', case+'/file-transactions:recover')
            [resolved] = (pending.parent/'history'/transaction).glob('resolution-*.json')
            result = json.loads(resolved.read_bytes())
            assert result['decision'] == 'rolled_back' and not pending.exists(), result
            assert api.call('GET', case+'/raw?path='+EDIT, raw=True) == b'before'
            # The retained original directory was moved away and back.
            set_limit(api.root/'test.sqlite', 138)
            operation['plan_sha256'] = api.call('POST', route+'/restore-plan', selection)['plan_sha256']
            response, observation = concurrent(api, restore, writer, scratch, dev, False)
            assert response.status_code == 200, response.text
            assert post(route+'/restore', json=operation).raise_for_status().json()['replayed'] is True
            assert api.call('GET', case+'/raw?path='+EDIT, raw=True) == b'after'
            assert not any(scratch.iterdir())
            report.update(restore=observation, rollback=result, replay=True, abandoned_stage_removed=True)

            export_case = f"/cases/{item['export_case']}"
            export_expected = materialize(item['exported'], item['export_case_root'], export=True)
            set_limit(api.root/'test.sqlite', 260)
            export_route = f"/fixture/{item['export_case']}/publish-export"
            export_scratch = item['export_case_root']/'.vibeflow-file-staging'
            response, observation = concurrent(api, lambda: post(export_route), writer, export_scratch, dev, False)
            assert response.status_code == 200, response.text
            assert post(export_route).raise_for_status().json() == response.json()
            assert api.call('GET', export_case+'/raw?path='+EDIT, raw=True) == b'after'
            dictionary = api.call('GET', export_case+'/caseDict')
            assert 'actual local tree export' in dictionary['content']
            assert (item['export_case_root']/'Ref/export-empty').is_dir()
            for root, contract in [(item['case_root'], expected), (item['export_case_root'], export_expected)]:
                with (root/LARGE).open('rb') as stream:
                    assert hashlib.file_digest(stream,'sha256').hexdigest() == contract['sha256']
            assert not any(export_scratch.iterdir())
            assert not list((native/'.node-storage-budget').glob('*.json'))
            return dict(report, export=observation, export_dictionary=True, export_empty_directory=True,
                        staging_and_claims_cleared=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--recovery-fixture', type=Path, required=True)
    parser.add_argument('--export-fixture', type=Path, required=True)
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    enforce([Path(__file__).resolve()])
    with tempfile.TemporaryDirectory(dir='/tmp', prefix='tree-test-') as directory:
        report = verify(Path(directory), (args.recovery_fixture.resolve(), args.export_fixture.resolve()), args.baseline)
    report.update(passed=True, real_model_calls=0, simulated_model_calls=0)
    args.report.write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
    print('CROSS_VOLUME_TREE_PASS', args.report, flush=True)
