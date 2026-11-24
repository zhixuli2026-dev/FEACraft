"""Actual API checks against a disposable copy of earlier native-run evidence.

No provider, model turn, native launch, or inference substitute is used here.
The original evidence database is read-only and must remain byte-identical.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import traceback
import uuid

from testsupport import IsolatedServer, SERVER
from workflow_input_recovery_acceptance import APP

ROOT = SERVER.parents[1]
TABLES = ('cases', 'workflow_runs', 'workflow_steps', 'jobs', 'job_attempts', 'durable_events')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connect(path):
    db = sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def snapshot(path):
    with connect(path) as db:
        return {table: [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY id')]
                for table in TABLES}


def run():
    from app.services.windows_process_scope import ProcessHandle
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256'] == hashlib.sha256(APP.encode()).hexdigest()
    required = {str(path.relative_to(ROOT)).replace('\\', '/') for path in (SERVER / 'app').rglob('*.py')}
    required.update('platform/server/' + name for name in (
        'workflow_recovery_binding_acceptance.py', 'workflow_input_recovery_acceptance.py', 'testsupport.py'))
    assert required <= set(review['source_hashes'])
    for name, digest in review['source_hashes'].items():
        assert sha(ROOT / name) == digest, name
    original = args.evidence.resolve()
    original_report = json.loads((original / 'workflow-result.json').read_text(encoding='utf8'))
    assert original_report['passed'] is True
    source_db = original / 'test.sqlite'
    source_hash = sha(source_db)
    api = IsolatedServer('workflow-backlink', app='workflow_backlink_fixture:app')
    (api.root / 'workflow_backlink_fixture.py').write_text(APP, encoding='utf8')
    with connect(source_db) as source, sqlite3.connect(api.root / 'test.sqlite') as target:
        source.backup(target)
    shutil.copytree(original / 'files', api.root / 'files')
    report = {'passed': False, 'real_model_calls': 0, 'mock_llm_calls': 0,
              'native_jobs_launched': 0, 'review_sha256': sha(args.review),
              'original_evidence': str(original), 'checks': []}
    entered = False
    handle = None
    try:
        api.__enter__()
        entered = True
        birth = api.call('GET', '/test/workflow-birth')
        handle = ProcessHandle.query(birth)
        report['API_birth'] = birth
        with connect(api.root / 'test.sqlite') as db:
            email = db.execute('SELECT email FROM users WHERE id=1').fetchone()[0]
        api.token = api.call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
        before = snapshot(api.root / 'test.sqlite')
        for check in original_report['checks']:
            if check['kind'] not in ('recompute', 'migrate'):
                continue
            source = check['source']
            context = api.call('GET', '/workflow-runs/' + source['run_id'] + '/recovery')
            assert context['action'] == check['kind'], context
            assert context['successors']['items'][0]['id'] == check['target_run']
            assert api.call('GET', '/workflow-runs/' + check['target_run'] + '/recovery')['source']['id'] == source['run_id']
            report['checks'].append({'kind': 'existing_actual_' + check['kind'] + '_history_readable',
                                     'source_run': source['run_id'], 'target_run': check['target_run']})
        damaged = next(row['source'] for row in original_report['checks']
                       if row['kind'] == 'unclaimed_unknown_writer_refused')
        prefix = '/workflow-runs/' + damaged['run_id']
        context = api.call('GET', prefix + '/recovery')
        assert context['action'] is None and context['reason']['code'] == 'evidence_invalid', context
        body = {'expected_version': context['run']['version'], 'expected_revision': context['current_revision'],
                'confirmed_reset': True, 'target': 'geometry', 'run_dir': 'C-run/case-1',
                'parameters': {'geometry_mode': 'export', 'report_mode': 'compose', 'solve_mode': 'serial'},
                'budget': {'timeout_seconds': 180, 'max_log_bytes': 1048576}}
        rejected = api.call('POST', prefix + ':recompute', body,
                            extra_headers={'Idempotency-Key': str(uuid.uuid4())}, expect=409)
        assert rejected['detail']['code'] == 'evidence_invalid', rejected
        assert api.call('GET', '/jobs/' + str(damaged['job_id']) + '/input-recovery')['workflow'] is None
        assert snapshot(api.root / 'test.sqlite') == before
        report['checks'].append({'kind': 'original_dangling_active_attempt_refused',
                                 'source_run': damaged['run_id'], 'context': context['reason'],
                                 'recompute_status': 409, 'recompute_error': rejected['detail'],
                                 'all_original_business_rows_unchanged': True})
        report['functional_passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:1800],
                             'frames': [{'file': Path(row.filename).name, 'line': row.lineno}
                                        for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            api.__exit__(None if report.get('functional_passed') else RuntimeError, None, None)
        if handle:
            handle.wait_exit(15)
            report['API_exit_verified'] = True
            handle.close()
        report['original_database_unchanged'] = sha(source_db) == source_hash
        report['passed'] = bool(report.get('functional_passed') and report.get('API_exit_verified')
                                and report['original_database_unchanged'])
        (api.root / 'binding-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        print('WORKFLOW_BINDING_RESULT ' + str(api.root / 'binding-result.json'), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(run())
