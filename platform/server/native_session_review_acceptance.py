"""Actual API/PTY, isolated old-schema migration and explicitly synthetic metadata faults.

No provider/LLM reply, model tool choice, Agent turn or CLI inference is simulated.
The malformed CLI/viz rows below represent unreadable history only, not execution.
"""
import argparse, hashlib, json, os, shutil, sqlite3, subprocess, sys, traceback, uuid
from pathlib import Path

import httpx
from websockets.sync.client import connect
from testsupport import IsolatedServer, SERVER

ROOT = SERVER.parents[1]
APP = '''from app.main import app
import os,uuid
from app.db import SessionLocal
from app.models import NativeSession,NativeCliSession,NativeVizSession
from app.services.durable_events import now
from app.services.process_identity_native import observe
@app.get('/api/v1/test/review-process')
async def process(): return observe(os.getpid())
@app.post('/api/v1/test/review-exit')
async def exit(): os._exit(0)
@app.post('/api/v1/test/review-metadata')
async def seed():
    # These are malformed metadata fixtures only, with no CLI/viz launch, turn,
    # RPC inference result, model reply, or physical-stop assertion.
    from datetime import timedelta
    rows=[]
    async with SessionLocal() as db:
        for kind,model in [('terminal',NativeSession),('cli',NativeCliSession),('viz',NativeVizSession)]:
            fields=dict(case_id=None if kind=='terminal' else 1,created_by=1,
                owner_id='MetadataFaultOnly',owner_token_sha256='0'*64,
                owner_identity_json='{}',launch_nonce=uuid.uuid4().hex,
                profile_json='{unreadable-history',phase='failed',ended_at=now(),
                heartbeat_at=now(),lease_expires_at=now()-timedelta(seconds=60),
                error='MetadataFaultOnly')
            if kind=='terminal': fields.update(id=str(uuid.uuid4()),shell='cmd')
            elif kind=='cli': fields.update(id=str(uuid.uuid4()),runtime='windows')
            else: fields.update(last_activity_at=now())
            row=model(**fields);db.add(row);await db.flush();rows.append({'kind':kind,'id':str(row.id)})
        await db.commit()
    return rows
'''


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def session_rows(path):
    with sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True) as db:
        return {table: hashlib.sha256(repr(db.execute('SELECT * FROM ' + table + ' ORDER BY id').fetchall()).encode()).hexdigest()
            for table in ('native_sessions', 'native_cli_sessions', 'native_viz_sessions')}


def run():
    from app.services.windows_process_scope import ProcessHandle
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256'] == hashlib.sha256(APP.encode('utf8')).hexdigest()
    for name, digest in review['source_hashes'].items(): assert sha(ROOT / name) == digest
    source = ROOT / 'tmp/job-recovery-UI-7lxkzlsd'
    previous = json.loads((source / 'UI-result.json').read_text(encoding='utf8'))
    assert previous['passed'] and len(previous['API_exit_witnesses']) == 2
    assert all(item['state'] == 'exited' for item in previous['API_exit_witnesses'])
    source_sha = sha(source / 'test.sqlite')
    api = IsolatedServer('native-session-review', app='review_fixture:app')
    assert api.root.parent == source.parent and api.root != source
    with sqlite3.connect('file:' + (source / 'test.sqlite').as_posix() + '?mode=ro', uri=True) as old:
        with sqlite3.connect(api.root / 'test.sqlite') as copy: old.backup(copy)
    shutil.copytree(source / 'files', api.root / 'files')
    (api.root / 'review_fixture.py').write_text(APP, encoding='utf8')
    report = {'passed': False, 'new_real_model_calls': 0, 'new_mock_llm_calls': 0,
        'metadata_faults_are_execution_evidence': False, 'developer_database_modified': False,
        'source_review_sha256': sha(args.review), 'API_exit_witnesses': []}
    handle = None; entered = False; term_id = None
    def save(): (api.root / 'review-result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    print('NATIVE_SESSION_REVIEW ' + str(api.root), flush=True)
    try:
        env = dict(os.environ, VIBEFLOW_DATABASE_URL='sqlite+aiosqlite:///' + (api.root / 'test.sqlite').as_posix())
        before = session_rows(api.root / 'test.sqlite')
        with (api.root / 'migration.log').open('w', encoding='utf8') as log:
            for command in [('stamp', 'a08c65df2197'), ('upgrade', 'head'), ('downgrade', 'a08c65df2197'), ('upgrade', 'head')]:
                subprocess.run([sys.executable, '-m', 'alembic', *command], cwd=SERVER, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        assert session_rows(api.root / 'test.sqlite') == before
        report['actual_migration_upgrade_downgrade_upgrade_preserved_old_sessions'] = True; save()
        api.__enter__(); entered = True
        birth = api.call('GET', '/test/review-process'); handle = ProcessHandle.query(birth)
        report['API_birth'] = birth
        email = json.loads((source / 'browser-setup.json').read_text(encoding='utf8'))['email']
        api.token = api.call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
        initial = api.call('GET', '/native-sessions/terminal')
        assert initial['data'] and all(row['kind'] == 'terminal' for row in initial['data'])
        closed = next(row for row in initial['data'] if row['cleanup_verified'] is True)
        plan = api.call('GET', '/native-sessions/terminal/' + closed['id'])
        assert plan['available_actions'] == [] and plan['review'] is None
        report['actual_closed_terminal_history_visible'] = True
        with connect(api.base.replace('http:', 'ws:') + '/ws/terminal?case=1&shell=cmd&token=' + api.token, proxy=None) as ws:
            while True:
                frame = ws.recv(timeout=30)
                if not isinstance(frame, str): continue
                value = json.loads(frame)
                assert value.get('type') != 'error', 'Actual PTY refused startup'
                if value.get('type') == 'ready': term_id = value['session']; break
        live = api.call('GET', '/native-sessions/terminal/' + term_id)
        assert live['owner_available'] is True and 'retain_for_review' not in live['available_actions']
        api.call('POST', '/native-sessions/terminal/' + term_id + ':retain-for-review',
            {'action': 'retain_for_review', 'snapshot_sha256': live['snapshot_sha256']}, expect=409)
        api.call('DELETE', '/terminal/sessions/' + term_id)
        stopped = api.call('GET', '/terminal/sessions/' + term_id)
        assert stopped['cleanup_verified'] is True
        report['actual_new_PTY_created_and_stopped'] = {'id': term_id, 'stop': stopped['stop']}; term_id = None
        faults = api.call('POST', '/test/review-metadata', {})
        frozen = session_rows(api.root / 'test.sqlite'); receipts = []
        for fault in faults:
            prefix = '/native-sessions/' + fault['kind'] + '/' + fault['id']
            plan = api.call('GET', prefix)
            assert plan['phase'] == 'evidence_invalid' and plan['alive'] is None and plan['cleanup_verified'] is None
            assert plan['available_actions'] == ['retain_for_review']
            body = {'action': 'retain_for_review', 'snapshot_sha256': plan['snapshot_sha256'], 'note': 'Metadata fault only; retain original evidence'}
            receipt = api.call('POST', prefix + ':retain-for-review', body)
            replay = api.call('POST', prefix + ':retain-for-review', body)
            assert receipt == replay and receipt['stop_evidence_created'] is False and receipt['execution_authorized'] is False
            api.call('POST', prefix + ':retain-for-review', {**body, 'note': 'different note'}, expect=409)
            api.call('POST', prefix + ':retain-for-review', {**body, 'snapshot_sha256': 'f' * 64}, expect=409)
            assert api.call('GET', prefix)['review']['id'] == receipt['id']
            receipts.append({'kind': fault['kind'], 'id': fault['id'], 'review_id': receipt['id'], 'case_id': plan['case_id']})
        assert session_rows(api.root / 'test.sqlite') == frozen
        assert next(item for item in receipts if item['kind'] == 'terminal')['case_id'] is None
        report['metadata_fault_retention_receipts'] = receipts
        report['original_session_rows_unchanged_by_review'] = True
        page = api.call('GET', '/native-sessions/terminal?limit=1')
        assert page['next_cursor'] is not None
        second = api.call('GET', '/native-sessions/terminal?limit=1&cursor=' + page['next_cursor'])
        assert second['data'] and second['data'][0]['id'] != page['data'][0]['id']
        api.call('GET', '/native-sessions/cli?cursor=' + page['next_cursor'], expect=400)
        original_token = api.token
        other = api.call('POST', '/auth/register', {'email': uuid.uuid4().hex + '@example.com', 'password': 'isolated-test-123', 'name': 'Other metadata reader'})
        api.token = other['token']
        for item in faults: api.call('GET', '/native-sessions/' + item['kind'] + '/' + item['id'], expect=404)
        api.token = original_token
        report['keyset_pagination_and_cross_user_rejection'] = True
        assert sha(source / 'test.sqlite') == source_sha
        report['original_actual_history_database_unchanged'] = True
        report['functional_passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'frames': [{'file': Path(v.filename).name, 'line': v.lineno, 'function': v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                if term_id: api.call('DELETE', '/terminal/sessions/' + term_id)
                try: api.call('POST', '/test/review-exit')
                except (OSError, httpx.TransportError): pass
                handle.wait_exit(10)
                report['API_exit_witnesses'].append({'birth': report['API_birth'], 'state': 'exited', 'reason': 'retained_creation_handle'})
            except BaseException as error: report['cleanup_failure'] = type(error).__name__
            api.__exit__(RuntimeError if 'failure' in report else None, None, None)
        if handle is not None: handle.close()
        report['passed'] = bool(report.get('functional_passed') and report['API_exit_witnesses'] and 'cleanup_failure' not in report)
        save()
    print('NATIVE_SESSION_REVIEW_RESULT ' + str(api.root / 'review-result.json'), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__': raise SystemExit(run())
