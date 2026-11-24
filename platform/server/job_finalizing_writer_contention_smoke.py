"""Exercise a real CadQuery finalizing transition while SQLite has a writer."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from app.services import process_identity_native as identity
from testsupport import IsolatedServer


SHAPE = 'sqlite_finalizing_writer'
SOURCE = ('import cadquery as cq\n'
          'def build_geometry(ctx):\n'
          f'    return {{{SHAPE!r}: cq.Workplane("XY").box(2, 3, 4)}}\n')

FIXTURE = r'''
import asyncio
from contextlib import closing
from contextvars import ContextVar
import json
import os
from pathlib import Path
import sqlite3
import time

from sqlalchemy import event
from app.db import engine
from app.main import app
from app.services import job_execution, jobrunner, process_identity_native

root = Path(__file__).parent
(root/'api-birth.json').write_text(json.dumps(process_identity_native.observe(os.getpid())), encoding='utf8')
short_phase_wait = ContextVar('short_phase_wait', default=False)

def trace(name, **values):
    with (root/'finalizing-trace.jsonl').open('a', encoding='utf8') as out:
        out.write(json.dumps({'name': name, 'time': time.monotonic(), **values})+'\n')

@event.listens_for(engine.sync_engine, 'checkout')
def short_fixture_timeout(connection, _record, _proxy):
    selected = short_phase_wait.get()
    cursor = connection.cursor()
    try:
        cursor.execute('PRAGMA busy_timeout=' + ('100' if selected else '30000'))
        if selected:
            cursor.execute('PRAGMA busy_timeout')
            trace('phase_sqlite_timeout', busy_timeout_ms=cursor.fetchone()[0])
    finally:
        cursor.close()

def hold_writer(loop, ready):
    with closing(sqlite3.connect(root/'test.sqlite', isolation_level=None, timeout=6)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        trace('writer_acquired', held_for_seconds=.8, busy_timeout_ms=100)
        loop.call_soon_threadsafe(ready.set)
        try:
            time.sleep(.8)
        finally:
            connection.rollback()
        trace('writer_released')

phase = job_execution.phase

async def observed_phase(owner, target, **kwargs):
    if target != 'finalizing':
        return await phase(owner, target, **kwargs)
    proc = jobrunner.get_proc(owner.job_id)
    owned = proc._owned_process
    result = owned.stop_result
    receipt = owned.guardian_proof['receipt']
    trace('phase_entry', job_id=owner.job_id, attempt_id=owner.attempt_id,
          stop_verified=result['verified'], root_exit_code=result['root_exit_code'],
          guardian_active_processes=receipt['active_processes'], guardian_members=receipt['membership']['exit_verified'])
    ready = asyncio.Event()
    writer = asyncio.create_task(asyncio.to_thread(hold_writer, asyncio.get_running_loop(), ready))
    try:
        await asyncio.wait_for(ready.wait(), timeout=8)
        trace('phase_call')
        wait_token = short_phase_wait.set(True)
        try:
            value = await phase(owner, target, **kwargs)
        finally:
            short_phase_wait.reset(wait_token)
        trace('phase_committed', status=value)
        return value
    except BaseException as error:
        raw = getattr(error, 'orig', error)
        trace('phase_error', error=type(error).__name__,
              sqlite_code=getattr(raw, 'sqlite_errorcode', None), sqlite_name=getattr(raw, 'sqlite_errorname', None),
              statement=getattr(error, 'statement', None))
        raise
    finally:
        await asyncio.shield(writer)

job_execution.phase = observed_phase
'''


def poll(fetch, predicate, timeout):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        selected = fetch()
        if predicate(selected):
            return selected
        time.sleep(.05)
    raise TimeoutError(f'Finalizing phase not observed after {timeout:g}s: {selected}')


def snapshot(root, job_id):
    uri = (root/'test.sqlite').resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT a.id, a.phase, a.output_manifest_json, j.status, j.exit_code '
                         'FROM job_attempts a JOIN jobs j ON a.job_id=j.id WHERE j.id=?', (job_id,)).fetchone()
        result = dict(row)
        output = json.loads(result.pop('output_manifest_json'))
        result['output_fields'] = sorted(output)
        result['verified_stop'] = bool(output.get('process_stop', {}).get('verified'))
        result['events'] = [entry[0] for entry in db.execute('SELECT type FROM durable_events '
                            'WHERE attempt_id=? ORDER BY seq', (result['id'],))]
        return result


def main():
    harness = IsolatedServer('job-finalizing-writer-contention', app='job_finalizing_writer_fixture:app')
    root = harness.root
    (root/'job_finalizing_writer_fixture.py').write_text(FIXTURE, encoding='utf8')
    trace_path = root/'finalizing-trace.jsonl'
    report = {'passed': False, 'model_calls': 0, 'mock_llm_calls': 0}

    def trace():
        if not trace_path.exists():
            return []
        return [json.loads(line) for line in trace_path.read_text(encoding='utf8').splitlines() if line]

    print(f'JOB_FINALIZING_WRITER_CONTENTION_EVIDENCE {root}', flush=True)
    try:
        with harness as api:
            api.create_case('Actual CadQuery finalizing writer')
            case_root = root/'files'/api.call('GET', api.prefix)['storage_prefix']
            (case_root/'A-geo/geometry.py').write_text(SOURCE, encoding='utf8')
            submitted = api.call('POST', api.prefix+'/geometry:preview', {'tolerance': .1})
            job_id = submitted['job_id']
            report['job_id'] = job_id
            records = poll(trace, lambda rows: any(row['name'] == 'writer_released' for row in rows), 65)
            report['phase_trace'] = records
            report['checkpoint'] = snapshot(root, job_id)
            facts = {row['name']: row for row in records}
            entry = facts['phase_entry']
            assert entry['stop_verified'] and entry['root_exit_code'] == 0 and entry['guardian_active_processes'] == 0, entry
            assert facts['phase_sqlite_timeout']['busy_timeout_ms'] == 100, facts
            ordered = list(facts)
            assert ordered.index('writer_acquired') < ordered.index('phase_call'), facts
            if 'phase_error' in facts:
                state = poll(lambda: api.call('GET', f'/jobs/{job_id}'),
                             lambda value: value.get('completion_error'), 5)
                report['public_completion_error'] = state.get('completion_error')
                raise AssertionError(f"Finalizing did not acquire the real SQLite writer: {facts['phase_error']}")
            records = poll(trace, lambda rows: any(row['name'] == 'phase_committed' for row in rows), 10)
            report['phase_trace'] = records
            facts = {row['name']: row for row in records}
            ordered = list(facts)
            assert ordered.index('writer_released') < ordered.index('phase_committed'), facts
            job = api.wait_job(job_id, timeout=45)
            assert job['exit_code'] == 0 and 'scene.json written: 1 objects' in job['log_backlog'], job
            scene = api.call('GET', api.prefix+'/geometry/scene')
            assert [value['name'] for value in scene['objects']] == [SHAPE], scene
            assert scene['publication']['id'] == submitted['publication_id'], scene['publication']
            report['terminal'] = snapshot(root, job_id)
            assert report['terminal']['phase'] == 'succeeded' and report['terminal']['verified_stop'], report['terminal']
            assert report['terminal']['events'].count('artifact.published') == 1, report['terminal']
        birth = json.loads((root/'api-birth.json').read_text(encoding='utf8'))
        poll(lambda: identity.probe(birth), lambda state: state['state'] == 'exited', 12)
        report.update(passed=True, api_owner_exit=True)
    except BaseException as error:
        report.update(error=type(error).__name__, detail=str(error), phase_trace=trace())
        raise
    finally:
        (root/'job-finalizing-writer-contention.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
    print(f'JOB_FINALIZING_WRITER_CONTENTION_OK {root}', flush=True)


if __name__ == '__main__':
    main()
