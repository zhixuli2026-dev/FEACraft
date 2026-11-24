"""Run real owned process exits against the fall-back file and SQL boundary."""

import argparse
import hashlib
import json
from pathlib import Path

from app.services.casedict import CASE_DICT_PATH, get_workflow_state, set_workflow_state
from app.services.case_events import CASE_EVENTS_PATH
from app.services.provenance import PROVENANCE_PATH
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_failure_stage_policy_acceptance import FIXTURE, scenario
from live_llm_job_repeat_acceptance import put, write
from testsupport import IsolatedServer


OBSERVATION = r'''
from pathlib import Path
from fastapi import HTTPException
from app import storage
from app.services import case_dict_edits
from app.services.casedict import set_workflow_state
from app.services.workflow_state import ExecutionConflict

original_prepare = workflow_fallback._prepare
original_publish = case_dict_edits.publish_draft

def stage_authority_prepare(source, plan):
 draft = original_prepare(source, plan)
 marker = Path(__file__).with_name(f'before-stage-write-{source.id}')
 try:
  marker.unlink()
 except FileNotFoundError:
  pass
 else:
  file = storage._fs_path(source.s3_prefix, workflow_fallback.CASE_DICT_PATH)
  text = set_workflow_state(file.read_bytes().decode('utf8'), 'D')
  file.write_bytes(text.encode('utf8') + b'\n// Owned concurrent edit between the read and commit.\n')
 return draft

async def stage_authority_publish(db, case, *args, **kwargs):
 marker = Path(__file__).with_name(f'before-stage-write-{case.id}')
 try:
  return await original_publish(db, case, *args, **kwargs)
 except workflow_fallback.CaseWriteConflict as error:
  if error.path == workflow_fallback.CASE_DICT_PATH:
   marker.with_suffix('.conflict').write_text(error.path, encoding='utf8')
  raise

workflow_fallback._prepare = stage_authority_prepare
case_dict_edits.publish_draft = stage_authority_publish

@app.get('/api/v1/jobs/{job_id}/testing/stage-authority')
async def stage_authority(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id)
 case=await get_owned_case(job.case_id,user,db,write=False)
 attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
 validate(case,job,attempt)
 required=await db.get(DurableEvent,workflow_fallback.event_id(job,workflow_fallback.REQUIRED))
 applied=await db.get(DurableEvent,workflow_fallback.event_id(job,workflow_fallback.APPLIED))
 return {'required_id':required.id,'required':json.loads(required.payload_json),
         'applied':json.loads(applied.payload_json) if applied else None,
         'stage':case.workflow_state,'revision':case.dict_revision,'drain_verified':True}

@app.post('/api/v1/jobs/{job_id}/testing/stage-authority')
async def stage_authority_replay(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id)
 case=await get_owned_case(job.case_id,user,db,write=True)
 try:
  return await workflow_fallback.settle_failure_fallback(db,case,job)
 except ExecutionConflict as error:
  raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
'''

OWNED_EDIT = b'\n// Owned concurrent edit between the read and commit.\n'
SOURCE_FILES = (CASE_DICT_PATH, CASE_EVENTS_PATH, PROVENANCE_PATH)


def capture(case_root):
    return {path: (case_root / path).read_bytes() for path in SOURCE_FILES}


def digests(files):
    return {path: hashlib.sha256(content).hexdigest() for path, content in files.items()}


def records(data):
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def request_fallback(api, route):
    def settled():
        value = api.call('POST', route, {}, expect=(200, 409))
        if value.get('detail', {}).get('code') in {'storage_quota_busy', 'validation_busy'}:
            return None
        return value
    return wait_for(settled, 'Fallback write coordination did not drain', seconds=20)


def authority_case(api, fault, observe_legacy=False):
    case_id = api.create_case(f'Stage fallback caseDict authority {fault}')['id']
    prefix = api.call('GET', api.prefix)['storage_prefix']
    case_root = api.root / 'files' / prefix
    stage = api.advance('V')
    started, release = (api.root / f'{fault}-{name}' for name in ('started', 'release'))
    put(api, 'Ref/stage-fault.py', f'''from pathlib import Path
import time
Path({str(started)!r}).touch()
release = Path({str(release)!r})
deadline = time.monotonic() + 20
while not release.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(98)
    time.sleep(.02)
print("ACTUAL_STAGE_AUTHORITY_FAULT", flush=True)
raise SystemExit(29)
''')
    submitted = write(api, 'POST', api.prefix + '/testing/stage-failure', {'job_type': 'solve'})
    identifier = submitted['job_id']
    wait_for(started.exists, 'Owned fault process did not launch', seconds=15)
    initial = capture(case_root)
    dictionary = case_root / CASE_DICT_PATH
    changed = set_workflow_state(initial[CASE_DICT_PATH].decode('utf8'), 'D').encode('utf8')
    if fault == 'stage-mismatch':
        dictionary.write_bytes(changed)
    elif fault == 'concurrent-edit':
        marker = api.root / f'before-stage-write-{case_id}'
        marker.touch()
        changed += OWNED_EDIT
    else:
        raise ValueError(fault)
    release.touch()
    job = api.wait_job(identifier, timeout=25, expected='failed')
    assert job['type'] == 'solve' and job['exit_code'] == 29
    assert job['attempt']['process_stop']['verified']
    assert 'ACTUAL_STAGE_AUTHORITY_FAULT' in job['log_backlog']
    route = f'/jobs/{identifier}/testing/stage-authority'
    before = api.call('GET', route)
    result = {'fault': fault, 'job_id': identifier, 'job_exit_code': job['exit_code'],
              'process_stop_verified': True, 'before': before, 'initial_hashes': digests(initial)}
    replay = request_fallback(api, route)
    unresolved = capture(case_root)
    after_request = api.call('GET', route)
    result.update(replay=replay, after_request=after_request,
                  unresolved_hashes=digests(unresolved),
                  owned_edit_sha256=hashlib.sha256(changed).hexdigest(),
                  actual_stage=get_workflow_state(unresolved[CASE_DICT_PATH].decode('utf8')))
    assert before['drain_verified'] and before['required']['observed_state'] == 'V'
    assert before['required']['exit_code'] == 29 and before['required']['observed_revision'] == stage['dict_revision']
    if observe_legacy:
        assert after_request['stage'] == result['actual_stage'] == 'C'
        assert unresolved[CASE_DICT_PATH] != changed and after_request['applied']
        return result

    assert replay['detail']['code'] == 'input_stale', result
    assert after_request['stage'] == 'V' and after_request['revision'] == stage['dict_revision']
    assert after_request['applied'] is None
    assert unresolved == {**initial, CASE_DICT_PATH: changed}
    if fault == 'concurrent-edit':
        conflict = marker.with_suffix('.conflict')
        def rejected_path():
            return conflict.read_text(encoding='utf8') if conflict.is_file() else None
        assert wait_for(rejected_path, 'Original fallback lost its file precondition', seconds=15) == CASE_DICT_PATH
        result['journal_rejected_changed_path'] = CASE_DICT_PATH
    dictionary.write_bytes(initial[CASE_DICT_PATH])
    replay = request_fallback(api, route)
    assert replay['previous_state'] == 'V' and replay['target_state'] == 'C' and replay['regressed']
    recovered = api.call('GET', route)
    assert recovered['stage'] == 'C' and recovered['revision'] == stage['dict_revision'] + 1
    assert recovered['applied']['required_event_id'] == before['required_id']
    final = capture(case_root)
    assert get_workflow_state(final[CASE_DICT_PATH].decode('utf8')) == 'C'
    events = records(final[CASE_EVENTS_PATH])
    assert len(events) == len(records(initial[CASE_EVENTS_PATH])) + 2
    event = events[-1]
    assert event['type'] == 'workflow.fallback' and event['job_id'] == identifier
    assert event['job_type'] == 'solve' and event['exit_code'] == 29 and event['log_path']
    assert event['target_state'] == event['workflow_state'] == 'C' and event['regressed']
    assert event in api.call('GET', api.prefix + '/events')['events']
    provenance = records(final[PROVENANCE_PATH])[-1]
    assert provenance['source'] == 'job-failure-fallback' and provenance['revision'] == recovered['revision']
    assert request_fallback(api, route)['replayed'] and capture(case_root) == final
    result.update(recovered=recovered, event=event, provenance=provenance, final_hashes=digests(final))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe-legacy', action='store_true')
    options = parser.parse_args()
    api = IsolatedServer('fallback-authority', app='stage_authority_fixture:app')
    (api.root / 'stage_authority_fixture.py').write_text(
        'from app.main import app\n' + FIXTURE + OBSERVATION, encoding='utf8')
    with api:
        faults = ['stage-mismatch'] if options.observe_legacy else ['stage-mismatch', 'concurrent-edit']
        report = {'mode': 'observe-legacy' if options.observe_legacy else 'acceptance',
                  'backend': 'fs', 'database': 'sqlite', 'model_calls': 0, 'source_authority': {}}
        destination = api.root / 'failure-fallback-authority.json'
        for fault in faults:
            report['source_authority'][fault] = authority_case(api, fault, options.observe_legacy)
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        if not options.observe_legacy:
            matrix = scenario(api)
            events = {}
            for path in (api.root / 'files').rglob('case-events.jsonl'):
                events.update({e['job_id']: e for e in records(path.read_bytes()) if e['type'] == 'workflow.fallback'})
            for check in matrix['checks']:
                event = events[check['job_id']]
                assert event['exit_code'] == 29 and event['log_path']
                assert event['job_type'] == check['job_type'] and event['target_state'] == check['target_state']
                assert event['regressed'] == check['regressed']
            report['typed_failure_matrix'] = matrix
        report['passed'] = True
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        print(destination, flush=True)


if __name__ == '__main__':
    main()
