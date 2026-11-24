"""Inspect the production UI against a copy of actual terminal job history; no inference."""
import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import traceback
from pathlib import Path

import httpx
from testsupport import SERVER, IsolatedServer

ROOT = SERVER.parents[1]
APP = '''from app.main import app
import os
from app.services.process_identity_native import observe
@app.get('/api/v1/test/ui-process')
async def ui_process(): return observe(os.getpid())
@app.post('/api/v1/test/ui-exit')
async def ui_exit(): os._exit(0)
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def job_history(path):
    with sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True) as connection:
        jobs = connection.execute('SELECT id,status FROM jobs ORDER BY id').fetchall()
        assert jobs and all(status in {'succeeded', 'failed', 'cancelled', 'interrupted'} for _, status in jobs)
        names = {'jobs', 'job_attempts', 'job_submission_requests', 'job_execution_heads',
                 'artifact_publications', 'artifact_publication_heads', 'publication_storage_reservations'}
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        hashes = {}
        for name in sorted(names & present):
            rows = connection.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
            hashes[name] = hashlib.sha256(repr(rows).encode('utf8')).hexdigest()
        return {'jobs': jobs, 'table_hashes': hashes}


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-history', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--native-history', action='store_true', help='Copy terminal native-session review acceptance history; seed metadata-only UI cases')
    args = parser.parse_args()
    source = args.from_history.resolve()
    assert source.parent == (ROOT / 'tmp').resolve() and source.name.startswith('native-session-review-' if args.native_history else 'job-recovery-UI-')
    prior_path = source / ('review-result.json' if args.native_history else 'UI-result.json')
    prior = json.loads(prior_path.read_text(encoding='utf8'))
    assert prior['passed'] and len(prior['API_exit_witnesses']) == (1 if args.native_history else 2)
    if not args.native_history: assert prior['preview_exited']
    assert all(row['state'] == 'exited' for row in prior['API_exit_witnesses'])
    review = json.loads(args.review.read_text(encoding='utf8'))
    app_source = APP
    if args.native_history:
        from native_session_review_acceptance import APP as native_app
        app_source = native_app
    assert review['fixture_sha256'] == hashlib.sha256(app_source.encode('utf8')).hexdigest()
    for base, hashes in review['sources'].items():
        for name, digest in hashes.items(): assert sha(ROOT / base / name) == digest
    original_db = source / 'test.sqlite'
    original_hash = sha(original_db)
    before = job_history(original_db)
    api = IsolatedServer('job-recovery-history-UI', app='history_ui_fixture:app')
    assert api.root.parent == source.parent and api.root != source
    assert not any(path.is_symlink() for path in (source / 'files').rglob('*'))
    with sqlite3.connect('file:' + original_db.as_posix() + '?mode=ro', uri=True) as original:
        with sqlite3.connect(api.root / 'test.sqlite') as copy: original.backup(copy)
    shutil.copytree(source / 'files', api.root / 'files')
    (api.root / 'history_ui_fixture.py').write_text(app_source, encoding='utf8')
    report = {'passed': False, 'source': source.relative_to(ROOT).as_posix(),
        'source_report_sha256': sha(prior_path), 'source_database_sha256': original_hash,
        'before': before, 'new_real_model_calls': 0, 'new_mock_llm_calls': 0,
        'new_native_business_jobs': 0, 'review_sha256': sha(args.review)}
    preview = preview_log = api_handle = preview_handle = None
    entered = False

    def save():
        (api.root / 'UI-result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')

    try:
        api.__enter__(); entered = True
        api_birth = api.call('GET', '/test/review-process' if args.native_history else '/test/ui-process'); api_handle = ProcessHandle.query(api_birth)
        report['API_birth'] = api_birth
        if args.native_history:
            with sqlite3.connect('file:' + (api.root / 'test.sqlite').as_posix() + '?mode=ro', uri=True) as database:
                email = database.execute('SELECT email FROM users WHERE id=1').fetchone()[0]
        else: email = json.loads((source / 'browser-setup.json').read_text(encoding='utf8'))['email']
        api.token = api.call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
        metadata_faults = api.call('POST', '/test/review-metadata', {}) if args.native_history else []
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        assert port not in {8000, api.port}
        web = ROOT / 'platform/web'
        preview_log = (api.root / 'web-preview.log').open('w', encoding='utf8')
        preview = subprocess.Popen([shutil.which('node.exe') or 'node', str(web / 'node_modules/vite/bin/vite.js'), 'preview', '--host', '127.0.0.1', '--port', str(port), '--strictPort'],
            cwd=web, env=dict(os.environ, VITE_API_TARGET=api.base), stdout=preview_log,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        preview_birth = identity.observe(preview.pid); preview_handle = ProcessHandle.query(preview_birth)
        report['preview_birth'] = preview_birth
        web_url = 'http://127.0.0.1:' + str(port)
        until = time.monotonic() + 30
        with httpx.Client(trust_env=False, timeout=1) as client:
            while True:
                assert preview.poll() is None
                try:
                    if client.get(web_url).status_code == 200: break
                except httpx.HTTPError: pass
                assert time.monotonic() < until
                time.sleep(.1)
        setup = {'root': str(api.root), 'web': web_url, 'email': email, 'case_id': 1, 'job_id': 2}
        if args.native_history: setup['metadata_faults_not_execution_evidence'] = metadata_faults
        (api.root / 'browser-setup.json').write_text(json.dumps(setup, indent=2), encoding='utf8')
        save(); print('JOB_HISTORY_UI_READY ' + str(api.root) + ' ' + web_url, flush=True)
        done = api.root / 'browser-finished.json'; until = time.monotonic() + 900
        while not done.exists():
            assert preview.poll() is None and time.monotonic() < until, 'Browser inspection stopped or timed out'
            time.sleep(.2)
        report['browser'] = json.loads(done.read_text(encoding='utf8')); assert report['browser']['passed']
        report['after'] = job_history(api.root / 'test.sqlite')
        assert report['after'] == before
        assert sha(original_db) == original_hash and job_history(original_db) == before
        report['original_history_unchanged'] = True; report['functional_passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'frames': [{'file': Path(v.filename).name, 'line': v.lineno, 'function': v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                for session in api.call('GET', '/terminal/sessions?include_closed=true')['sessions']:
                    if session.get('alive'): api.call('DELETE', '/terminal/sessions/' + session['id'])
                report['owned_terminal_sessions_closed'] = True
                try: api.call('POST', '/test/review-exit' if args.native_history else '/test/ui-exit')
                except (OSError, httpx.TransportError): pass
                api_handle.wait_exit(10); report['actual_API_exited'] = True
            except BaseException as error: report['cleanup_failure'] = type(error).__name__
            api.__exit__(RuntimeError if 'failure' in report else None, None, None)
        if preview is not None:
            if preview.poll() is None: preview.terminate()
            preview.wait(timeout=10)
        if preview_handle is not None:
            preview_handle.wait_exit(10); report['actual_preview_exited'] = True; preview_handle.close()
        if preview_log is not None: preview_log.close()
        if api_handle is not None: api_handle.close()
        report['passed'] = bool(report.get('functional_passed') and report.get('actual_API_exited') and report.get('actual_preview_exited') and 'cleanup_failure' not in report)
        save()
    print('JOB_HISTORY_UI_RESULT ' + str(api.root / 'UI-result.json'), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__': raise SystemExit(run())
