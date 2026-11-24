"""Actual API death/restart at an unconfirmed engineering boundary, no inference."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def restart_at_confirmation(api, flow, step, report):
    assert step['status'] == 'awaiting_confirmation'
    assert api.proc.poll() is None
    root = next((api.root / 'files/workspaces').glob('*/cases/' + str(api.case_id)))

    def snapshot():
        with sqlite3.connect('file:' + (api.root / 'test.sqlite').as_posix() + '?mode=ro', uri=True) as db:
            assert not db.execute("SELECT id FROM jobs WHERE status NOT IN ('succeeded','failed','cancelled','interrupted')").fetchall()
            assert not db.execute("SELECT id FROM native_requests WHERE phase NOT IN ('succeeded','failed','cancelled')").fetchall()
            tables = ['jobs', 'job_attempts', 'job_execution_heads', 'workflow_runs', 'workflow_steps',
                      'artifact_publications', 'artifact_publication_heads', 'job_submission_requests', 'cases']
            rows = {table: sorted(map(repr, db.execute('SELECT * FROM ' + table).fetchall())) for table in tables}
        files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(root.rglob('*')) if p.is_file()}
        return {'tables': {name: hashlib.sha256('\n'.join(values).encode()).hexdigest() for name, values in rows.items()},
                'row_counts': {name: len(values) for name, values in rows.items()}, 'files': files}

    before = snapshot()
    old = api.proc
    argv = old.args
    old.terminate()
    old.wait(timeout=20)
    assert old.poll() is not None
    record = {'boundary': step['confirmation_state'], 'original_workflow_id': flow['id'],
              'original_step_id': step['id'], 'old_pid': old.pid, 'old_exit_code': old.returncode,
              'old_API_exit_verified': True, 'before': before}
    report['API_restart'] = record
    api.proc = subprocess.Popen(argv, cwd=api.root, env=api.env, stdout=api.log, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    record['new_pid'] = api.proc.pid
    until = time.monotonic() + 60
    while time.monotonic() < until:
        assert api.proc.poll() is None, 'Replacement API exited before readiness'
        try:
            if api.call('GET', '/api/health', api_prefix=False).get('ok'):
                break
        except OSError:
            pass
        time.sleep(.2)
    else:
        raise TimeoutError('Replacement API did not start')
    # The same original bearer token must remain valid after a real restart.
    current = api.call('GET', '/workflow-runs/' + flow['id'])
    current_step = next(row for row in current['steps'] if row['id'] == step['id'])
    assert current_step['status'] == 'awaiting_confirmation'
    assert current_step['version'] == step['version']
    assert current_step['confirmation_revision'] == step['confirmation_revision']
    after = snapshot()
    assert before == after, 'Read-only reconnect changed original engineering history or files'
    record.update(original_token_accepted=True, original_confirmation_still_pending=True,
                  exact_business_rows_and_files_unchanged=True, no_new_jobs_on_reconnect=True)
    (api.root / 'actual-api-restart.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=['cavity2d', 'cavity3d', 'conduction'])
    parser.add_argument('--stage', choices=['D', 'E'], default='D')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / 'platform/tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    from registered_cae_benchmarks import run_benchmark
    run_benchmark(args.kind, restart_at_stage=args.stage)


if __name__ == '__main__':
    main()
