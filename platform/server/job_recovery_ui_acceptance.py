"""Hold an actual stopped Pandoc job and a production web preview for browser UI acceptance."""
import hashlib, json, os, shutil, socket, subprocess, sys, time, traceback
from pathlib import Path
import httpx
from testsupport import SERVER, IsolatedServer
from attempt_output_recovery_acceptance import FIXTURE
from attempt_professional_finalizing_acceptance import EXTRA
from early_finalizing_acceptance import EARLY

ROOT = SERVER.parents[1]
REVIEW = ROOT / 'tmp/N5-job-recovery-UI-review-v1-20261110.json'


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity
    api = IsolatedServer('job-recovery-UI', app='output_fixture:app')
    for name, value in [('normal-mode', 'Actual UI acceptance'), ('native-handler', 'report'), ('early-boundary', 'before')]:
        (api.root / name).write_text(value, encoding='utf8')
    (api.root / 'output_fixture.py').write_text(FIXTURE + '\n' + EXTRA + '\n' + EARLY, encoding='utf8')
    report = {'passed': False, 'model_calls': 0, 'mock_llm_calls': 0, 'developer_database_modified': False, 'API_births': [], 'API_exit_witnesses': []}
    review = json.loads(REVIEW.read_text(encoding='utf8'))
    for base, hashes in review['sources'].items():
        for name, digest in hashes.items():
            assert hashlib.sha256((ROOT / base / name).read_bytes()).hexdigest() == digest
    report['source_review_sha256'] = hashlib.sha256(REVIEW.read_bytes()).hexdigest()
    handles = {}; preview = None; preview_log = None; preview_handle = None; entered = False

    def save():
        (api.root / 'UI-result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')

    def remember():
        birth = api.call('GET', '/test/output-process')
        handles[json.dumps(birth, sort_keys=True)] = ProcessHandle.query(birth)
        report['API_births'].append(birth); save(); return birth

    def exited(birth):
        handle = handles[json.dumps(birth, sort_keys=True)]
        handle.wait_exit(10); assert handle.identity(allow_exited=True) == birth
        if not any(row['birth'] == birth for row in report['API_exit_witnesses']):
            report['API_exit_witnesses'].append({'birth': birth, 'state': 'exited', 'reason': 'retained_creation_handle'}); save()

    def stop_api():
        birth = api.call('GET', '/test/output-process')
        try: api.call('POST', '/test/output-exit')
        except (OSError, httpx.TransportError): pass
        exited(birth)

    print('JOB_RECOVERY_UI ' + str(api.root), flush=True)
    try:
        api.__enter__(); entered = True
        api.create_case('Job recovery browser acceptance'); user = api.call('GET', '/auth/me'); remember()
        api.put('E-report/report.md', '# Original recovery document\n\nActual Pandoc for browser recovery.\n')
        job = api.call('POST', api.prefix + '/report:build', {})
        report['job_id'] = job['job_id']; save()
        until = time.monotonic() + 120
        marker = api.root / 'actual-early-boundary.json'
        while not marker.exists():
            assert time.monotonic() < until, 'Actual Pandoc did not reach stopped early stage'
            time.sleep(.1)
        report['before_browser'] = api.call('GET', '/test/output-state/' + str(job['job_id'])); save()
        stop_api(); api.__exit__(RuntimeError, None, None); entered = False
        api.__enter__(); entered = True
        api.token = api.call('POST', '/auth/login', {'email': user['email'], 'password': 'isolated-test-123'})['token']; remember()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        assert port not in {8000, api.port}
        web = ROOT / 'platform/web'
        env = dict(os.environ, VITE_API_TARGET=api.base)
        preview_log = (api.root / 'web-preview.log').open('w', encoding='utf8')
        preview = subprocess.Popen([shutil.which('node.exe') or 'node', str(web / 'node_modules/vite/bin/vite.js'), 'preview', '--host', '127.0.0.1', '--port', str(port), '--strictPort'], cwd=web, env=env, stdout=preview_log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        web_url = 'http://127.0.0.1:' + str(port)
        until = time.monotonic() + 30
        with httpx.Client(trust_env=False, timeout=1) as client:
            while True:
                assert preview.poll() is None, 'Web preview exited'
                try:
                    if client.get(web_url).status_code == 200: break
                except httpx.HTTPError: pass
                assert time.monotonic() < until, 'Web preview startup timeout'
                time.sleep(.1)
        preview_birth = identity.observe(preview.pid); preview_handle = ProcessHandle.query(preview_birth)
        report['preview_birth'] = preview_birth
        setup = {'root': str(api.root), 'web': web_url, 'email': user['email'], 'case_id': api.case_id, 'job_id': job['job_id']}
        (api.root / 'browser-setup.json').write_text(json.dumps(setup, indent=2), encoding='utf8')
        report['browser_setup'] = {'web': web_url, 'case_id': api.case_id, 'job_id': job['job_id']}; save()
        print('JOB_RECOVERY_UI_READY ' + web_url, flush=True)
        until = time.monotonic() + 1200
        done = api.root / 'browser-finished.json'
        while not done.exists():
            assert preview.poll() is None and time.monotonic() < until, 'Browser acceptance stopped or timed out'
            time.sleep(.2)
        report['browser'] = json.loads(done.read_text(encoding='utf8'))
        report['after_browser'] = api.call('GET', '/test/output-state/' + str(job['job_id']))
        assert report['browser']['passed']
        before, after = report['before_browser'], report['after_browser']
        assert after['phase'] == after['job_status'] == 'succeeded'
        assert after['attempt_id'] == before['attempt_id'] and after['input'] == before['input'] and after['original_deadline_unix'] == before['original_deadline_unix']
        assert after['write_checkpoint']['stop'] == before['actual_in_memory_stop']
        releases = [json.loads(line) for line in (api.root / 'actual-root-releases.jsonl').read_text().splitlines()]
        assert sum(row['job_id'] == job['job_id'] for row in releases) == 1
        report['actual_root_releases'] = releases; report['functional_passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'frames': [{'file': Path(v.filename).name, 'line': v.lineno, 'function': v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                sessions = api.call('GET', '/terminal/sessions?include_closed=true')['sessions']
                for session in sessions:
                    if session.get('alive'): api.call('DELETE', '/terminal/sessions/' + session['id'])
                report['owned_terminal_sessions_closed'] = True
                stop_api()
            except BaseException as error: report['cleanup_failure'] = type(error).__name__
            api.__exit__(RuntimeError if 'failure' in report else None, None, None)
        if preview is not None:
            if preview.poll() is None: preview.terminate()
            preview.wait(timeout=10)
        if preview_handle is not None:
            preview_handle.wait_exit(10); report['preview_exited'] = True; preview_handle.close()
        if preview_log is not None: preview_log.close()
        for birth in report['API_births']:
            try: exited(birth)
            except BaseException as error: report['cleanup_failure'] = type(error).__name__
        for handle in handles.values(): handle.close()
        report['passed'] = bool(report.get('functional_passed') and 'cleanup_failure' not in report and report.get('preview_exited'))
        save()
    print('JOB_RECOVERY_UI_RESULT ' + str(api.root / 'UI-result.json'), flush=True)
    return 0 if report['passed'] else 1

if __name__ == '__main__': raise SystemExit(run())
