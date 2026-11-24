"""Actual CasePanel recovery, concurrency and permission checks on accepted history.

Only isolated SQL/files/HTTP/browser/native processes are used by default. An
explicit live_llm entry can supply an actual upstream evidence observer. Browser phase results must
name their actual scope; this harness does not declare the whole N6-05 complete.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import time
import traceback

from testsupport import SERVER, IsolatedServer

ROOT = SERVER.parents[1]
APP = '''import os,json
from pathlib import Path
from fastapi import Depends
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Job,JobAttempt
from app.routers.workspaces import get_owned_case
from app.main import app
from app.services.process_identity_native import observe
@app.get('/api/v1/test/controls-birth')
async def birth():return observe(os.getpid())

@app.get('/api/v1/test/controls-recovered-drain/{job_id}')
async def recovered_drain(job_id:int,user=Depends(get_current_user)):
    from sqlalchemy import select
    from app.services.job_write_checkpoint import validate
    async with SessionLocal() as db:
        job=await db.get(Job,job_id)
        assert job is not None
        case=await get_owned_case(job.case_id,user,db,write=False)
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
        value=json.loads(attempt.write_checkpoint_json)
        if value.get('schema')!='vibeflow.job-write-checkpoint.v4':
            return {'available':False,'job_id':job.id,'attempt_id':attempt.id}
        checked=validate(case,job,attempt)
        return {'available':True,'job_id':job.id,'attempt_id':attempt.id,'state':checked['state'],
            'original_stop':checked['stop'],'worker_exit':checked['recovery']['worker_exit'],
            'original_root_stop_missing':checked['recovery']['original_root_stop_missing'],
            'guardian_scopes':len(checked['recovery']['guardians']),'validated_by_production_certificate':True}

@app.post('/api/v1/test/controls-ownership-fault/{case_id}')
async def ownership_fault(case_id:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True)
        job=await db.get(Job,7)
        assert case.id==1 and job.case_id==case.id and job.status=='succeeded'
        from sqlalchemy import select
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
        assert attempt.phase=='succeeded' and attempt.claim_token is not None
        assert json.loads(attempt.worker_identity_json)!={}
        marker=Path.cwd()/'ownership-fault-original.json';assert not marker.exists()
        marker.write_text(json.dumps({'attempt_id':attempt.id,'claim_token':attempt.claim_token,'restored':False}),encoding='utf8')
        from sqlalchemy import text
        changed=await db.execute(text('UPDATE job_attempts SET claim_token=NULL WHERE id=:identifier AND claim_token=:original'),
            {'identifier':attempt.id,'original':attempt.claim_token})
        assert changed.rowcount==1
        await db.commit()
        return {'case_id':case.id,'job_id':job.id,'fault':'explicit_missing_claim_with_retained_native_metadata','no_status_or_stop_receipt_changed':True}

@app.post('/api/v1/test/controls-ownership-restore/{case_id}')
async def ownership_restore(case_id:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True);assert case.id==1
        marker=Path.cwd()/'ownership-fault-original.json'
        record=json.loads(marker.read_text(encoding='utf8'))
        attempt=await db.get(JobAttempt,record['attempt_id']);job=await db.get(Job,attempt.job_id)
        assert job.case_id==case.id and job.id==7 and attempt.phase=='succeeded'
        assert attempt.claim_token==(record['claim_token'] if record['restored'] else None)
        if not record['restored']:
            from sqlalchemy import text
            changed=await db.execute(text('UPDATE job_attempts SET claim_token=:original WHERE id=:identifier AND claim_token IS NULL'),
                {'identifier':attempt.id,'original':record['claim_token']})
            assert changed.rowcount==1
        await db.commit()
        record['restored']=True;marker.write_text(json.dumps(record),encoding='utf8')
        return {'restored':True,'no_status_or_stop_receipt_changed':True}
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def history(path, limits=None):
    names = ['workflow_runs', 'workflow_steps', 'jobs', 'job_attempts']
    with sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True) as db:
        if limits is None:
            limits = {name: db.execute('SELECT coalesce(max(rowid),0) FROM ' + name).fetchone()[0] for name in names}
        return limits, {name: sha_rows(db, name, limits[name]) for name in names}


def sha_rows(db, name, limit):
    rows = db.execute('SELECT * FROM ' + name + ' WHERE rowid<=? ORDER BY rowid', (limit,)).fetchall()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def run(*, model_observer=None):
    from app.services.windows_process_scope import ProcessHandle
    from app.services.process_identity_native import observe
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--copy-index', action='store_true', help='Preserve stopped-source index databases through SQLite backup for restart acceptance')
    parser.add_argument('--restart-on-request', action='store_true', help='Allow one explicitly requested idle or new-running-job API restart on the same storage and environment')
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf-8'))
    assert review['fixture_sha256'] == hashlib.sha256(APP.encode()).hexdigest()
    for name, digest in review['source_hashes'].items():
        assert sha(ROOT / name) == digest, name
    source = (args.source_root or ROOT / 'tmp/case-workflow-chain-jg4a48bl').resolve()
    assert source.is_dir() and source.is_relative_to((ROOT / 'tmp').resolve()) and not source.is_symlink()
    source_result = source / ('controls-result.json' if (source / 'controls-result.json').exists() else 'chain-result.json')
    accepted = json.loads(source_result.read_text(encoding='utf-8'))
    assert accepted['passed'] and all(accepted[k] for k in ['all_actual_native_stops_verified', 'API_exit_verified', 'preview_exit_verified'])
    original = source / 'test.sqlite'
    original_sha = sha(original)
    limits, old_history = history(original)
    with sqlite3.connect('file:' + original.as_posix() + '?mode=ro', uri=True) as db:
        original_requests = {r[0] for r in db.execute('SELECT id FROM native_requests')}
    api = IsolatedServer('case-workflow-controls', app='case_controls_fixture:app')
    with sqlite3.connect('file:' + original.as_posix() + '?mode=ro', uri=True) as a, sqlite3.connect(api.root / 'test.sqlite') as b:
        a.backup(b)
    assert not any(p.is_symlink() for p in (source / 'files').rglob('*'))
    shutil.copytree(source / 'files', api.root / 'files')
    copied_indexes = {}
    if args.copy_index:
        assert (source / 'index').is_dir() and not (source / 'index').is_symlink()
        (api.root / 'index').mkdir()
        for index in sorted((source / 'index').glob('case-*.sqlite')):
            assert index.is_file() and not index.is_symlink() and index.resolve().parent == (source / 'index').resolve()
            copied_indexes[index.name] = sha(index)
            with sqlite3.connect('file:' + index.as_posix() + '?mode=ro', uri=True) as original_index, sqlite3.connect(api.root / 'index' / index.name) as target_index:
                original_index.backup(target_index)
    (api.root / 'case_controls_fixture.py').write_text(APP, encoding='utf-8')
    report = {'passed': False, 'real_model_calls': 0, 'mock_llm_calls': 0, 'actual_native_jobs': [], 'review_sha256': sha(args.review)}
    report['preserved_index_databases'] = copied_indexes
    entered = False
    api_handle = preview = preview_handle = preview_log = None

    def inventory():
        with sqlite3.connect('file:' + (api.root / 'test.sqlite').as_posix() + '?mode=ro', uri=True) as db:
            report['actual_native_jobs'] = [r[0] for r in db.execute('SELECT id FROM jobs WHERE rowid>? ORDER BY id', (limits['jobs'],))]
            return [(r[0], r[1]) for r in db.execute('SELECT id,status FROM workflow_runs')]

    def stopped(identifier):
        until = time.monotonic() + 180
        while time.monotonic() < until:
            job = api.call('GET', '/jobs/' + str(identifier))
            attempt = job.get('attempt') or {}
            if job['status'] in {'succeeded', 'failed', 'cancelled', 'interrupted'} and attempt.get('phase') == job['status'] and (attempt.get('process_stop') or {}).get('verified') is True:
                report.setdefault('native_stops', {})[str(identifier)] = {'status': job['status'], 'stop': attempt['process_stop']}
                return
            if job['status'] == attempt.get('phase') == 'interrupted':
                recovered = api.call('GET', '/test/controls-recovered-drain/' + str(identifier))
                if recovered.get('available') and recovered.get('validated_by_production_certificate'):
                    assert recovered['job_id'] == identifier and recovered['attempt_id'] == attempt['id']
                    assert recovered['state'] == 'recovered_stopped' and recovered['original_stop'] is None
                    report.setdefault('native_stops', {})[str(identifier)] = {'status': job['status'], 'stop': None, 'independent_scope_drain': recovered}
                    return
            time.sleep(.15)
        raise TimeoutError('Actual native stop not verified for job ' + str(identifier))

    def drain_requests():
        # A cold refresh starts real capability probes. Their lifecycle is
        # independent of jobs and must finish before this harness kills API.
        until = time.monotonic() + 120
        while True:
            observations = []
            with sqlite3.connect('file:' + (api.root / 'test.sqlite').as_posix() + '?mode=ro', uri=True) as db:
                rows = db.execute('SELECT id,kind,phase,children_json FROM native_requests').fetchall()
            for identifier, kind, phase, raw in rows:
                if identifier in original_requests:
                    continue
                children = list(json.loads(raw).get('children', {}).values())
                verified = phase in {'succeeded', 'failed', 'cancelled'} and all(
                    child['phase'] in {'stopped', 'failed'} and (child.get('stop') or {}).get('verified') is True
                    for child in children)
                observations.append({'id': identifier, 'kind': kind, 'phase': phase,
                                     'children': len(children), 'actual_stops_verified': verified})
            report['native_request_stops'] = observations
            report['all_actual_request_stops_verified'] = all(v['actual_stops_verified'] for v in observations)
            if report['all_actual_request_stops_verified']:
                return
            if time.monotonic() >= until:
                raise TimeoutError('Actual non-job native requests have not drained')
            time.sleep(.2)

    try:
        api.__enter__()
        entered = True
        report['API_birth'] = api.call('GET', '/test/controls-birth')
        api_handle = ProcessHandle.query(report['API_birth'])
        with sqlite3.connect(api.root / 'test.sqlite') as db:
            email, workspace_id = db.execute('SELECT email,(SELECT workspace_id FROM cases WHERE id=1) FROM users WHERE id=1').fetchone()
        api.token = api.call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
        existing_cases = api.call('GET', '/workspaces/' + str(workspace_id) + '/cases')
        neighbor = next((item for item in existing_cases if item['id'] != 1), None)
        if neighbor is None:
            neighbor = api.call('POST', '/workspaces/' + str(workspace_id) + '/cases', {'name': 'CasePanel ownership boundary'})
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        web = ROOT / 'platform/web'
        preview_log = (api.root / 'web-preview.log').open('w', encoding='utf-8')
        preview = subprocess.Popen([shutil.which('node.exe') or 'node', str(web / 'node_modules/vite/bin/vite.js'), 'preview', '--host', '127.0.0.1', '--port', str(port), '--strictPort'],
            cwd=web, env=dict(os.environ, VITE_API_TARGET=api.base), stdout=preview_log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        report['preview_birth'] = observe(preview.pid)
        preview_handle = ProcessHandle.query(report['preview_birth'])
        with sqlite3.connect(api.root / 'test.sqlite') as db:
            selected_run = (accepted.get('recomputation') or {}).get('run_id') or db.execute("SELECT id FROM workflow_runs WHERE case_id=1 AND target='report' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        setup = {'root': str(api.root), 'web': 'http://127.0.0.1:' + str(port), 'email': email, 'user_id': 1, 'case_id': 1,
            'workspace_id': workspace_id, 'neighbor_case_id': neighbor['id'], 'run_id': selected_run}
        (api.root / 'browser-setup.json').write_text(json.dumps(setup, indent=2), encoding='utf-8')
        print('CASE_CONTROLS_READY ' + str(api.root), flush=True)
        until = time.monotonic() + 2400
        while not (api.root / 'browser-finished.json').exists():
            assert time.monotonic() < until and api.proc.poll() is None and preview.poll() is None
            inventory()
            restart_request = api.root / 'api-restart-request.json'
            if args.restart_on_request and restart_request.exists() and not report.get('service_restart'):
                request = json.loads(restart_request.read_text(encoding='utf-8'))
                assert request.get('scope') in {'idle_same_storage_and_credentials', 'running_job_same_storage_and_credentials'}
                assert all(status in {'succeeded', 'failed', 'cancelled', 'interrupted'} for _, status in inventory())
                with sqlite3.connect(api.root / 'test.sqlite') as db:
                    active_jobs = [r[0] for r in db.execute("SELECT id FROM jobs WHERE status IN ('queued','running') ORDER BY id")]
                if request['scope'] == 'idle_same_storage_and_credentials':
                    assert active_jobs == []
                else:
                    identifier = request['job_id']
                    assert type(identifier) is int and identifier in report['actual_native_jobs'] and active_jobs == [identifier]
                    running = api.call('GET', '/jobs/' + str(identifier))
                    assert running['status'] == running['attempt']['phase'] == 'running'
                    assert running['attempt']['process_identity'] is not None
                    (api.root / 'running-job-before-restart.json').write_text(json.dumps(running, ensure_ascii=False, indent=2), encoding='utf-8')
                assert all(row.get('cleanup_verified') is True for row in api.call('GET', '/terminal/sessions?include_closed=true')['sessions'])
                drain_requests()
                for name, digest in review['source_hashes'].items():
                    assert sha(ROOT / name) == digest, name
                before_history = history(api.root / 'test.sqlite', limits)[1]
                restart = {'scope': request['scope'], 'before': report['API_birth'],
                           'same_database_storage_vault_and_environment': True}
                if request['scope'] == 'running_job_same_storage_and_credentials':
                    restart['job_id'] = identifier
                    restart['attempt_id'] = running['attempt']['id']
                command = list(api.proc.args)
                api.proc.terminate()
                api.proc.wait(timeout=20)
                api_handle.wait_exit(20)
                api_handle.close()
                api_handle = None
                restart['original_process_exit_verified'] = True
                report['service_restart'] = restart
                (api.root / 'api-restart-progress.json').write_text(json.dumps(restart, indent=2), encoding='utf-8')
                api.proc = subprocess.Popen(command, cwd=api.root, env=api.env, stdout=api.log,
                                            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                restart['after'] = observe(api.proc.pid)
                api_handle = ProcessHandle.query(restart['after'])
                ready_until = time.monotonic() + 30
                while True:
                    assert api.proc.poll() is None, 'Restarted API exited before readiness'
                    try:
                        if api.call('GET', '/api/health', api_prefix=False).get('ok'):
                            break
                    except (OSError, ValueError):
                        pass
                    if time.monotonic() >= ready_until:
                        raise TimeoutError('Restarted API did not become ready')
                    time.sleep(.15)
                assert restart['before'] != restart['after']
                restart['same_auth_token_accepted'] = isinstance(api.call('GET', '/workspaces'), list)
                restart['old_history_unchanged'] = history(api.root / 'test.sqlite', limits)[1] == before_history
                assert restart['same_auth_token_accepted'] and restart['old_history_unchanged']
                restart['ready'] = True
                (api.root / 'api-restart-ready.json').write_text(json.dumps(restart, indent=2), encoding='utf-8')
                print('CASE_CONTROLS_RESTARTED ' + str(api.root), flush=True)
            (api.root / 'controls-progress.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            time.sleep(.5)
        report['browser'] = json.loads((api.root / 'browser-finished.json').read_text(encoding='utf-8'))
        assert report['browser']['passed'] is True
        inventory()
        for identifier in report['actual_native_jobs']:
            stopped(identifier)
        drain_requests()
        assert history(api.root / 'test.sqlite', limits)[1] == old_history
        report['old_workflow_step_job_attempt_rows_unchanged'] = True
        report['functional_passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:1400], 'frames': [{'file': Path(row.filename).name, 'line': row.lineno} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                if (api.root / 'ownership-fault-original.json').exists():
                    report['ownership_restore_cleanup'] = api.call('POST', '/test/controls-ownership-restore/1', {})
                for identifier, status in inventory():
                    if status not in {'succeeded', 'failed', 'cancelled', 'interrupted'}:
                        value = api.call('GET', '/workflow-runs/' + identifier)
                        api.call('POST', '/workflow-runs/' + identifier + ':cancel', {'expected_version': value['version']})
                inventory()
                for identifier in report['actual_native_jobs']:
                    job = api.call('GET', '/jobs/' + str(identifier))
                    if job['status'] in {'queued', 'running'}:
                        api.call('POST', '/jobs/' + str(identifier) + ':cancel', {})
                    stopped(identifier)
                for row in api.call('GET', '/terminal/sessions?include_closed=true')['sessions']:
                    if row.get('cleanup_verified') is not True:
                        api.call('DELETE', '/terminal/sessions/' + row['id'])
                report['terminal_cleanup'] = api.call('GET', '/terminal/sessions?include_closed=true')['sessions']
                assert all(row.get('cleanup_verified') is True for row in report['terminal_cleanup'])
                drain_requests()
            except BaseException as error:
                report['cleanup_failure'] = type(error).__name__ + ': ' + str(error)[:700]
        if preview:
            if preview.poll() is None:
                preview.terminate()
            preview.wait(timeout=15)
        if preview_handle:
            preview_handle.wait_exit(15)
            preview_handle.close()
            report['preview_exit_verified'] = True
        if preview_log:
            preview_log.close()
        if entered:
            api.__exit__(None if report.get('functional_passed') else RuntimeError, None, None)
        if api_handle:
            api_handle.wait_exit(15)
            api_handle.close()
            report['API_exit_verified'] = True
        report['original_database_unchanged'] = sha(original) == original_sha
        report['source_indexes_unchanged'] = all(sha(source / 'index' / name) == digest for name, digest in copied_indexes.items())
        report['all_actual_native_stops_verified'] = all(str(i) in report.get('native_stops', {}) for i in report['actual_native_jobs'])
        if model_observer is not None:
            try:
                report.update(model_observer(api.root))
            except Exception as error:
                report['real_model_calls'] = None
                report['model_evidence_failure'] = type(error).__name__ + ': ' + str(error)[:700]
        report['passed'] = bool(report.get('functional_passed') and report.get('API_exit_verified') and report.get('preview_exit_verified') and report['original_database_unchanged'] and report['source_indexes_unchanged'] and report['all_actual_native_stops_verified'] and report.get('all_actual_request_stops_verified') and 'cleanup_failure' not in report and 'model_evidence_failure' not in report)
        (api.root / 'controls-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('CASE_CONTROLS_RESULT ' + str(api.root / 'controls-result.json'), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(run())
