"""Join genuine SQLite/S3 process completion, case writes, indexing and restart."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import sqlite3
import sys
import time
from urllib.parse import urlencode
import uuid

from live_llm_job_repeat_acceptance import main, put
from live_llm_outbox_restart_acceptance import restart
from live_llm_residual_chart_acceptance import NORMAL


OBSERVERS = '''
import functools, time
from app.services import artifact_publication, durable_events, job_execution, jobrunner, publication_source_validation

traces = root/'joined-observer.jsonl'
def trace(stage, phase, marker, *, detail=None):
    with traces.open('a', encoding='utf8') as stream:
        stream.write(json.dumps(dict(stage=stage, phase=phase, at=time.time(), marker=marker, detail=detail))+'\\n')

def observe(target, name, subject):
    original = getattr(target, name)
    @functools.wraps(original)
    async def observed(*args, **kwargs):
        marker = subject(*args)
        trace(name, 'enter', marker)
        try:
            value = await original(*args, **kwargs)
        except BaseException as error:
            trace(name, 'error', marker, detail=type(error).__name__)
            raise
        trace(name, 'return', marker)
        return value
    setattr(target, name, observed)

observe(jobrunner.JobProcess, '_run', lambda self, *_: self.job_id)
observe(job_execution, '_finish_owned', lambda owner, *_: owner.job_id)
observe(artifact_publication, 'verify_files_for_finish', lambda owner, *_: owner.job_id)
observe(publication_source_validation.SourceValidation, 'inspect', lambda self, *_: self.owner.job_id)
observe(durable_events, 'publish_to_local_bus', lambda event, *_: event.id)
'''


def observations(api):
    path = api.root / 'joined-observer.jsonl'
    if not path.exists():
        return []
    lines = path.read_text(encoding='utf8').splitlines(keepends=True)
    return [json.loads(line) for line in lines if line.endswith('\n')]


def persisted(api):
    file = (api.root / 'test.sqlite').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(file, uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row

        def rows(sql):
            return [dict(row) for row in db.execute(sql, (api.case_id,))]

        events = rows('''select e.id,e.seq,e.type,e.attempt_id,e.payload_json,
                          q.status delivery,q.attempts delivery_attempts
                         from durable_events e left join event_outbox q on q.event_id=e.id
                         where e.case_id=? order by e.seq''')
        for event in events:
            event['payload'] = json.loads(event.pop('payload_json'))
        reservations = {
            table: rows(f'select state,count(1) count,sum(remaining_bytes) remaining_bytes from {table} where case_id=? group by state')
            for table in ('publication_storage_reservations', 'output_archive_reservations', 'job_log_reservations',
                          'storage_write_reservations')
        }
        reservations['owner_storage_reservations'] = rows('''select state,count(1) count,sum(remaining_bytes) remaining_bytes
            from owner_storage_reservations where user_id=(select w.user_id from workspaces w join cases c
            on c.workspace_id=w.id where c.id=?) group by state''')
        return {
            'events': events,
            'publications': rows('''select p.id,p.attempt_id,p.kind,p.status,p.generation,
                                            h.active_publication_id, p.manifest_sha256
                                     from artifact_publications p left join artifact_publication_heads h
                                       on p.kind=h.kind and p.case_id=h.case_id
                                     where p.case_id=? order by p.generation'''),
            'fences': rows('select job_id,status,ended_at from case_validation_fences where case_id=?'),
            'reservations': reservations,
        }


def wait_until(check, description, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        answer = check()
        if answer:
            return answer
        time.sleep(.1)
    raise TimeoutError(description)


def scenario(api, *, store=None, **_):
    assert store is not None and api.database_url is None, 'This regression targets actual SQLite and SeaweedFS'
    record = {'passed': False, 'native_jobs': [], 'concurrent_requests': [], 'failure': None}
    evidence = api.root / 'joined-finalization.json'
    gate, started = api.root / 'child-release', api.root / 'child-started'
    script = 'Ref/joined-residual.py'
    input_path = 'Ref/joined-case-edit.txt'
    old_text = 'before' + uuid.uuid4().hex
    new_text = 'after' + uuid.uuid4().hex
    program = f'''import os, time
from pathlib import Path
Path({str(started)!r}).write_text(str(os.getpid()), encoding='ascii')
deadline = time.monotonic()+60
while not Path({str(gate)!r}).exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('Fixture did not release the Windows child')
    time.sleep(.05)
print({NORMAL!r}, end='')
'''

    def save():
        record['trace'] = observations(api)
        evidence.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')

    def submit():
        key = uuid.uuid4().hex
        request = {'type': 'solve', 'command': f'"{sys.executable}" "{script}"',
                   'runtime': 'windows', 'timeout_seconds': 80}
        def try_submit():
            value = api.call('POST', api.prefix+'/jobs', request, expect=(200, 409), extra_headers={'Idempotency-Key': key})
            if 'detail' not in value:
                return value
            assert value['detail']['code'] in {'storage_quota_busy', 'validation_busy'}, value
        value = wait_until(try_submit, 'Actual job admission did not drain', 30)
        entry = {'job_id': value['id'], 'submitted_at': time.time()}
        record['native_jobs'].append(entry)
        save()
        return entry

    def complete(entry, expected):
        identifier = entry['job_id']
        def inspect():
            value = api.call('GET', f'/jobs/{identifier}')
            state = {k: value.get(k) for k in ('status', 'phase', 'completion_pending', 'completion_error')}
            states = entry.setdefault('states', [])
            if not states or states[-1]['state'] != state:
                states.append({'at': time.time(), 'state': state})
                save()
            return value if ((value['status'] not in {'queued', 'running'} and not value.get('completion_pending'))
                             or value.get('completion_error')) else None
        value = wait_until(inspect, f'Job {identifier} had no visible result by the original 180-second limit', 180)
        assert value['status'] == expected and value.get('completion_error') is None, value
        attempt = value['attempt']
        assert attempt['process_stop']['verified'], attempt
        data = api.call('GET', api.prefix+'/raw?'+urlencode({'path': value['log_path']}), raw=True)
        assert NORMAL.encode() in data.replace(b'\r\n',b'\n'), data[:512]
        entry.update(visible_at=time.time(), result=value['status'], exit_code=value['exit_code'],
                     reason=attempt['reason_code'], attempt_id=attempt['id'],
                     log_path=value['log_path'], log_sha256=hashlib.sha256(data).hexdigest(),
                     process_stop_verified=True)
        save()
        return value

    def rebuild(entry):
        identifier = entry['job_id']
        def entered_prepared_file_verification():
            return next((row['at'] for row in observations(api) if row['marker']==identifier
                         and row['stage']=='verify_files_for_finish' and row['phase']=='enter'), None)
        verification_started = wait_until(entered_prepared_file_verification,
                                          f'Job {identifier} did not reach natural finalization', 65)
        started_at = time.time()
        result = api.call('POST', api.prefix+'/index:rebuild', {}, expect=(200, 409))
        return {'job_id': identifier, 'verification_started': verification_started, 'started_at': started_at,
                'ended_at': time.time(), 'result': result}

    try:
        put(api, script, program)
        put(api, input_path, old_text)
        gate.touch()
        baseline = submit()
        complete(baseline, 'succeeded')

        gate.unlink()
        started.unlink()
        stale = submit()
        wait_until(started.exists, 'Windows child did not start against the sealed source', 45)
        assert api.call('GET', f'/jobs/{stale["job_id"]}')['attempt']['phase'] == 'running'
        stale['child_pid'] = int(started.read_text(encoding='ascii'))
        wrote_at = time.time()
        put(api, input_path, new_text)
        stale['same_case_write_while_running'] = {'started_at': wrote_at, 'ended_at': time.time()}
        assert api.call('GET', api.prefix+'/raw?'+urlencode({'path': input_path}), raw=True) == new_text.encode()
        with ThreadPoolExecutor(max_workers=1) as pool:
            indexing = pool.submit(rebuild, stale)
            gate.touch()
            complete(stale, 'failed')
            record['concurrent_requests'].append(indexing.result(timeout=33))
        assert stale['reason'] == 'input_stale', stale
        prior = persisted(api)
        baseline_pub, = [p for p in prior['publications'] if p['attempt_id'] == baseline['attempt_id']]
        refused_pub, = [p for p in prior['publications'] if p['attempt_id'] == stale['attempt_id']]
        assert baseline_pub['status'] == 'committed' and refused_pub['status'] == 'failed'
        assert baseline_pub['active_publication_id'] == refused_pub['active_publication_id'] == baseline_pub['id']

        retry = submit()
        with ThreadPoolExecutor(max_workers=1) as pool:
            indexing = pool.submit(rebuild, retry)
            complete(retry, 'succeeded')
            record['concurrent_requests'].append(indexing.result(timeout=33))
        assert sum(request['result'].get('ok') is True for request in record['concurrent_requests']) >= 1, record
        assert all(request['verification_started'] <= request['started_at'] < entry['visible_at']
                   for request, entry in zip(record['concurrent_requests'], (stale, retry)))

        def settled():
            state = persisted(api)
            rows = state['events']
            write = [e for e in rows if e['type'] == 'files.changed' and input_path in e['payload'].get('paths', [])]
            if len(write) >= 2 and all(e['delivery'] == 'delivered' for e in rows):
                return state
        before = wait_until(settled, 'Actual durable outbox did not settle', 40)
        ids = [e['id'] for e in before['events']]
        assert len(ids) == len(set(ids)) and sorted(set(e['seq'] for e in before['events'])) == [e['seq'] for e in before['events']]
        writes = [event for event in before['events'] if event['type'] == 'files.changed' and input_path in event['payload'].get('paths', [])]
        assert len(writes) == 2, writes
        trace = observations(api)
        assert all(any(row['stage'] == 'publish_to_local_bus' and row['phase'] == 'return' and row['marker'] == event['id']
                       for row in trace) for event in writes)
        for entry in record['native_jobs']:
            related = [event for event in before['events'] if event['attempt_id'] == entry['attempt_id']]
            event_types = [event['type'] for event in related]
            prepared = 'attempt.finalization_prepared'
            visible = 'attempt.completion_visible'
            assert event_types.count(prepared) == 1, event_types
            if entry['result']=='succeeded':
                assert event_types.count(visible) == 1 and event_types.index(prepared) < event_types.index(visible), event_types
                visible_event = next(event for event in related if event['type']==visible)
                assert any(row['stage']=='publish_to_local_bus' and row['phase']=='return'
                           and row['marker']==visible_event['id'] for row in trace)
            else:
                assert visible not in event_types and event_types.count('workflow.failure_fallback_applied')==1, event_types
            assert 'attempt.resource_usage' in event_types and 'attempt.storage_usage' in event_types, event_types
        assert before['fences'] and all(fence['status']=='released' for fence in before['fences']), before['fences']
        assert all(all(row['state'] != 'active' and row['remaining_bytes'] == 0 for row in rows)
                   for rows in before['reservations'].values()), before['reservations']
        assert all(before['reservations'][table] for table in ('publication_storage_reservations',
                   'output_archive_reservations', 'job_log_reservations', 'owner_storage_reservations')), before['reservations']
        current, = [p for p in before['publications'] if p['id']==p['active_publication_id']]
        assert current['attempt_id'] == retry['attempt_id'] and current['status'] == 'committed'
        record['published_before_restart'] = before['publications']
        record['reservations_after_jobs'] = before['reservations']
        record['outbox_write_events'] = writes
        record['ordered_events'] = [{'id': e['id'], 'seq': e['seq'], 'type': e['type'], 'attempt_id': e['attempt_id']}
                                    for e in before['events']]
        save()

        prior_pid = api.proc.pid
        api.proc.terminate()
        api.proc.wait(timeout=20)
        restart(api)
        assert api.proc.pid != prior_pid
        assert api.call('GET', api.prefix+'/raw?'+urlencode({'path': input_path}), raw=True) == new_text.encode()
        for item in record['native_jobs']:
            state = api.call('GET', f'/jobs/{item["job_id"]}')
            assert state['attempt']['id'] == item['attempt_id'] and state['status'] == item['result']
            contents = api.call('GET', api.prefix+'/raw?'+urlencode({'path': item['log_path']}), raw=True)
            assert hashlib.sha256(contents).hexdigest() == item['log_sha256']
        assert persisted(api)['publications'] == record['published_before_restart']
        matches = api.call('GET', api.prefix+'/search?'+urlencode({'q': new_text}))['hits']
        assert any(hit['path'] == input_path for hit in matches), matches
        assert not any(hit['path'] == input_path for hit in api.call('GET', api.prefix+'/search?'+urlencode({'q': old_text}))['hits'])
        record.update(passed=True, original_api_exited=True, restart_distinct_pid=True,
                      exact_bytes_after_restart=True, index_after_restart=True, mock_llm_calls=0)
    except BaseException as error:
        record['failure'] = {'kind': type(error).__name__, 'detail': str(error)[:2500]}
        try:
            record['persistence_at_failure'] = persisted(api)
        except (OSError, sqlite3.Error):
            pass
        raise
    finally:
        gate.touch()
        for job in record['native_jobs']:
            if 'visible_at' not in job:
                try:
                    api.call('POST', f'/jobs/{job["job_id"]}:cancel', expect=(200, 409))
                except (OSError, AssertionError):
                    pass
        save()
    return {'passed': True, 'record': evidence.name, 'attempts': record['native_jobs']}


if __name__ == '__main__':
    if '--s3' not in sys.argv or '--postgres' in sys.argv:
        raise SystemExit('Use --source-profile-id <authorized DeepSeek profile> --s3 without --postgres')
    asyncio.run(main(selected_override=scenario, fixture_override=OBSERVERS, store_volume_max=6))
