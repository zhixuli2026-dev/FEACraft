"""Actual waiting-gate cancellation and subsequent admission, without LLM calls.

The fixture pauses after the production 'created' observation has committed.
It neither substitutes process output nor constructs identities/stop receipts.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3
import time
import traceback

from testsupport import IsolatedServer, SERVER

ROOT = SERVER.parents[1]
APP = '''
import asyncio,os
from pathlib import Path
from fastapi import Depends,HTTPException
from app.main import app
from app.deps import get_current_user
from app.services import job_launch_registration as launches
from app.services.process_identity_native import observe
root=Path.cwd();state={'armed':False,'release':asyncio.Event()}
original=launches.observed
async def observed(owner,value):
 await original(owner,value)
 if state['armed'] and value['stage']=='created':
  state.update(armed=False,job_id=owner.job_id,actual_created=value)
  await state['release'].wait()
launches.observed=observed
@app.get('/api/v1/test/starting-cancel/state')
async def inspect(user=Depends(get_current_user)):
 return {'job_id':state.get('job_id'),'actual_created':state.get('actual_created'),'API_birth':observe(os.getpid())}
@app.post('/api/v1/test/starting-cancel/{operation}')
async def control(operation:str,user=Depends(get_current_user)):
 if operation=='arm':
  if state['armed'] or state.get('job_id') and not state['release'].is_set():raise HTTPException(409)
  state.clear();state.update(armed=True,release=asyncio.Event())
 elif operation=='release':state['release'].set()
 else:raise HTTPException(400)
 return {'ok':True}
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256'] == hashlib.sha256(APP.encode()).hexdigest()
    assert review['source_hashes'][Path(__file__).resolve().relative_to(ROOT).as_posix()] == sha(Path(__file__))
    for name, digest in review['source_hashes'].items():
        assert sha(ROOT / name) == digest, name
    api = IsolatedServer('job-starting-cancel', app='starting_cancel_fixture:app')
    (api.root / 'starting_cancel_fixture.py').write_text(APP, encoding='utf8')
    result = {'passed': False, 'real_model_calls': 0, 'mock_llm_calls': 0,
              'review_sha256': sha(args.review), 'scenarios': [], 'actual_jobs': []}
    entered = False
    handle = None

    def job(identifier):
        row = api.call('GET', '/jobs/' + str(identifier))
        attempt = row.get('attempt') or {}
        return {'id': row['id'], 'status': row['status'], 'phase': row['phase'],
                'attempt_id': attempt.get('id'), 'identity': attempt.get('process_identity'),
                'stop': attempt.get('process_stop'), 'registration': attempt.get('registration')}

    def until(read, predicate, seconds=90):
        deadline = time.monotonic() + seconds
        while True:
            value = read()
            if predicate(value):
                return value
            if time.monotonic() >= deadline:
                result['last_observation'] = value
                raise TimeoutError('Original operation has not reached the required observation')
            time.sleep(.1)

    def stopped(identifier, status):
        return until(lambda: job(identifier), lambda v: v['status'] == v['phase'] == status
                     and (v['stop'] or {}).get('verified') is True)

    def history(identifier):
        with sqlite3.connect('file:' + (api.root / 'test.sqlite').as_posix() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT id,claim_token,process_identity_json,write_checkpoint_json '
                             'FROM job_attempts WHERE job_id=? ORDER BY number DESC LIMIT 1', (identifier,)).fetchone()
            events = db.execute('SELECT type FROM durable_events WHERE attempt_id=? ORDER BY seq', (row[0],)).fetchall()
        return {'attempt_id': row[0], 'claimed': bool(row[1]), 'identity': json.loads(row[2]),
                'checkpoint': json.loads(row[3]), 'event_types': [r[0] for r in events]}

    try:
        api.__enter__()
        entered = True
        api.create_case('Actual WSL batch starting cancellation')
        from app.services.windows_process_scope import ProcessHandle
        birth = api.call('GET', '/test/starting-cancel/state')['API_birth']
        handle = ProcessHandle.query(birth)
        result['API_birth'] = birth
        from app.services.owned_process import wsl_path
        for mode, runtime in [('batch', 'wsl'), ('single', 'windows'), ('single', 'wsl-sandbox')]:
            if mode == 'single':
                workspace = api.call('GET', '/workspaces')[0]
                case = api.call('POST', f"/workspaces/{workspace['id']}/cases",
                                {'name': 'Actual '+runtime+' single starting cancellation'})
                api.case_id = case['id']
                api.prefix = f"/cases/{api.case_id}"
            marker = api.root / ('unreleased-' + mode + '-' + runtime + '.txt')
            command = ('printf UNRELEASED > ' + wsl_path(marker) if runtime == 'wsl'
                       else 'echo UNRELEASED>"' + str(marker) + '"')
            if runtime=='wsl-sandbox':command='echo N6_CANCELLED_SANDBOX_PAYLOAD_FORBIDDEN'
            api.call('POST', '/test/starting-cancel/arm', {})
            scenario = {'mode': mode, 'runtime': runtime, 'passed': False}
            result['scenarios'].append(scenario)
            with ThreadPoolExecutor(max_workers=1) as pool:
                submitted = None
                try:
                    if mode == 'batch':
                        batch = api.call('POST', api.prefix + '/jobs:batch',
                                         {'commands': [command, command], 'runtime': runtime, 'max_concurrent': 1}, expect=202)
                        identifiers = [j['id'] for j in batch['jobs']]
                        scenario['batch_id'] = batch['id']
                    else:
                        submitted = pool.submit(api.call, 'POST', api.prefix + '/jobs',
                                                {'command': command, 'runtime': runtime}, expect=(200, 409, 500), raw=True)
                        identifiers = []
                    prepared = until(lambda: api.call('GET', '/test/starting-cancel/state'),
                                     lambda v: bool(v['actual_created']))
                    first = prepared['job_id']
                    if mode == 'single':
                        identifiers = [first]
                    result['actual_jobs'].extend(identifiers)
                    scenario['jobs'] = identifiers
                    scenario['original_created'] = prepared['actual_created']
                    assert job(first)['phase'] == 'starting'
                    if mode == 'batch':
                        api.call('POST', api.prefix + '/job-batches/' + batch['id'] + ':cancel', {})
                    else:
                        api.call('POST', '/jobs/' + str(first) + ':cancel', {})
                    scenario['before_release'] = job(first)
                    assert not marker.exists()
                finally:
                    api.call('POST', '/test/starting-cancel/release', {})
                if submitted is not None:
                    response = submitted.result(timeout=90)
                    scenario['original_submit_response_bytes'] = len(response)
            scenario['cancelled_first'] = stopped(first, 'cancelled')
            scenario['first_history'] = history(first)
            assert scenario['first_history']['checkpoint']['state'] == 'stopped'
            assert scenario['cancelled_first']['registration']['stage'] == 'aborted'
            if runtime in {'wsl','wsl-sandbox'}:
                assert 'attempt.native_scope_retired' in scenario['first_history']['event_types']
            if runtime=='wsl-sandbox':
                assert 'N6_CANCELLED_SANDBOX_PAYLOAD_FORBIDDEN' not in api.call('GET','/jobs/'+str(first))['log_backlog']
            if mode == 'batch':
                scenario['queued'] = job(identifiers[1])
                scenario['queued_history'] = history(identifiers[1])
                assert scenario['queued']['status'] == scenario['queued']['phase'] == 'cancelled'
                assert scenario['queued_history']['claimed'] is False
                assert scenario['queued_history']['identity'] == {}
                assert 'attempt.launch_intended' not in scenario['queued_history']['event_types']
            assert not marker.exists(), 'Cancelled payload was actually released'
            scenario['cancelled_payload_not_executed'] = True
            # The same case must accept and finish new real work after drainage.
            followup_command='echo STARTING_CANCEL_FOLLOWUP'
            if runtime=='wsl':
                api.put('Ref/n6-wsl-input.txt','ACTUAL_WSL_INPUT\n')
                payload="""import errno,json,os
from pathlib import Path
source=Path('Ref/n6-wsl-input.txt').read_text()
Path('Ref/n6-wsl-output.txt').write_text(source+'ACTUAL_WSL_OUTPUT\\n')
status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
denied=False;denial_errno=None
try:os.setuid(0)
except OSError as error:
 if error.errno not in {errno.EPERM,errno.EINVAL}:raise
 denied=True;denial_errno=error.errno
print('ACTUAL_WSL_SCOPE '+json.dumps({'uid':os.getuid(),'euid':os.geteuid(),'gid':os.getgid(),'groups':os.getgroups(),'no_new_privs':status['NoNewPrivs'].strip(),'root_denied':denied,'denial_errno':denial_errno,'input':source,'output':Path('Ref/n6-wsl-output.txt').read_text(),'cwd':os.getcwd()}))
"""
                followup_command='python3 -c '+shlex.quote(payload)
            followup = api.call('POST', api.prefix + '/jobs', {'command': followup_command, 'runtime': runtime})
            result['actual_jobs'].append(followup['id'])
            scenario['followup'] = stopped(followup['id'], 'succeeded')
            if runtime=='wsl':
                detail=api.call('GET','/jobs/'+str(followup['id']))
                observed=json.loads(next(line.removeprefix('ACTUAL_WSL_SCOPE ') for line in detail['log_backlog'].splitlines() if line.startswith('ACTUAL_WSL_SCOPE ')))
                assert observed['uid']==observed['euid']==observed['gid']==65534
                assert observed['groups']==[] and observed['no_new_privs']=='1' and observed['root_denied']
                assert observed['input']=='ACTUAL_WSL_INPUT\n' and observed['output']=='ACTUAL_WSL_INPUT\nACTUAL_WSL_OUTPUT\n'
                assert observed['cwd'].endswith('/run')
                assert detail['attempt']['process_identity']['native']['process']['uid']==65534
                scenario['actual_unprivileged_workspace_io']=observed
            scenario['passed'] = True
            print('STARTING_CANCEL_SCENARIO ' + mode + ' passed', flush=True)
        result['passed'] = all(s['passed'] for s in result['scenarios'])
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'message': str(error)[:700],
                             'frames': [{'file': Path(r.filename).name, 'line': r.lineno} for r in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                api.call('POST', '/test/starting-cancel/release', {})
                for identifier in result['actual_jobs']:
                    value = job(identifier)
                    if value['status'] in {'queued', 'running'}:
                        api.call('POST', '/jobs/' + str(identifier) + ':cancel', {})
                    durable = history(identifier)
                    if durable['claimed']:
                        until(lambda: job(identifier), lambda v: v['status'] == v['phase']
                              and (v['stop'] or {}).get('verified') is True)
                    else:
                        assert durable['identity'] == {} and 'attempt.launch_intended' not in durable['event_types']
                        assert job(identifier)['phase'] == 'cancelled'
            except BaseException as error:
                result['cleanup_failure'] = type(error).__name__
            api.__exit__(None if result['passed'] else RuntimeError, None, None)
        if handle is not None:
            handle.wait_exit(15)
            handle.close()
            result['API_exit_verified'] = True
        result['passed'] = result['passed'] and result.get('API_exit_verified') is True and 'cleanup_failure' not in result
        (api.root / 'starting-cancel-result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf8')
        print('STARTING_CANCEL_RESULT ' + str(api.root / 'starting-cancel-result.json'), flush=True)
        print(json.dumps({k: result.get(k) for k in ['passed', 'failure', 'cleanup_failure', 'API_exit_verified']}), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(run())
