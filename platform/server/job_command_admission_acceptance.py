"""Verify the ordinary Jobs API command filter against real isolated services."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
import sys
import time

import httpx

from testsupport import IsolatedServer, SERVER


FIXTURE = '''
from pathlib import Path
from functools import wraps

from app.main import app
from app.services import job_delivery, jobrunner, quota

trace = Path(__file__).with_name('job-admission.jsonl')

def observe(module, name):
    original = getattr(module, name)
    @wraps(original)
    async def measured(*args, **kwargs):
        with trace.open('a', encoding='utf8') as stream:
            stream.write(name+'\\n')
        return await original(*args, **kwargs)
    setattr(module, name, measured)

observe(quota, 'check_job_quota')
observe(job_delivery, 'request')
observe(jobrunner, 'start_job')
'''

# These are inert echo arguments even if an assertion catches a filter regression.
SAMPLES = ('echo format C:', 'echo mkfs sample', 'echo rm -rf /path',
           'echo del /f file', 'echo rd /s folder')
RUNTIMES = ('windows', 'wsl', 'wsl-sandbox')
TABLES = ('jobs','job_attempts','job_execution_heads','job_submission_requests',
          'event_outbox','job_log_reservations','output_archive_reservations')


def snapshot(api):
    if api.database_url is None:
        uri = (api.root/'test.sqlite').resolve().as_uri()+'?mode=ro'
        with closing(sqlite3.connect(uri,uri=True)) as db:
            return {table:db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in TABLES}

    async def postgres():
        import asyncpg

        dsn = api.database_url.replace('postgresql+asyncpg:', 'postgresql:', 1)
        connection = await asyncpg.connect(dsn,timeout=8)
        try:
            async with connection.transaction(readonly=True):
                return {table:await connection.fetchval(f'SELECT count(*) FROM {table}') for table in TABLES}
        finally:
            await connection.close()
    return asyncio.run(postgres())


def trace(api):
    path = api.root/'job-admission.jsonl'
    return path.read_text(encoding='utf8').splitlines() if path.exists() else []


def stream(http, job_id):
    text, done = [], None
    with http.stream('GET',f'/jobs/{job_id}/log-stream?offset=0') as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith('data: '):
                continue
            event = json.loads(line[6:])
            assert event['type']!='error',event
            if event['type']=='log':
                text.append(event['text'])
            if event['type']=='done':
                done=event
    assert done is not None
    return ''.join(text),done


def verify_stop(api, job_id, expected):
    deadline = time.monotonic()+75
    while time.monotonic()<deadline:
        job = api.call('GET',f'/jobs/{job_id}')
        if job['status'] not in {'queued','running','cancelling',expected}:
            raise AssertionError((job_id,job['status'],job.get('log_backlog')))
        stopped = (job.get('attempt') or {}).get('process_stop') or {}
        if job['status']==expected and stopped.get('verified') and not job.get('completion_pending'):
            return
        time.sleep(.2)
    raise TimeoutError(f'Job {job_id} did not persist its verified stop')


def exercise(api, report):
    api.create_case('Ordinary Job command admission')
    owner_route, owner_token = api.prefix, api.token
    api.put('Ref/unchanged.txt','survives rejected submission')
    api.create_case('Separate Job account')
    api.call('POST',owner_route+'/jobs',{'command':SAMPLES[0]},expect=404)
    api.prefix, api.token = owner_route, owner_token
    baseline, old_trace = snapshot(api),trace(api)
    cases = [(runtime,command) for runtime in RUNTIMES for command in SAMPLES]

    def rejected(index, sample):
        runtime, command = sample
        result = api.call('POST',owner_route+'/jobs',{'command':command,'runtime':runtime},expect=400,
                         extra_headers={'Idempotency-Key':f'rejected-{index}'})
        assert '高危命令' in result['detail']
        return runtime

    with ThreadPoolExecutor(max_workers=4) as pool:
        rejected_runtimes = list(pool.map(lambda item:rejected(*item),enumerate(cases)))
    # Outbox delivery of case creation is asynchronous. Compare row counts,
    # while the fixture above identifies any new job quota, delivery or launch.
    assert snapshot(api)==baseline and trace(api)==old_trace
    assert api.call('GET',owner_route+'/jobs')==[]
    assert api.call('GET',owner_route+'/raw?path=Ref/unchanged.txt',raw=True)==b'survives rejected submission'
    report.update(denied_requests=len(cases),runtimes=sorted(set(rejected_runtimes)),
                  record_counts_unchanged_after_denials=baseline,
                  operation_observers_unchanged_after_denials=True,foreign_account_http=404)

    mark = 'JOB_ADMISSION_ACCEPTED'
    headers = {'Idempotency-Key':'rejected-0'}
    allowed = {'command':f'echo {mark}','runtime':'windows'}
    job = api.call('POST',owner_route+'/jobs',allowed,extra_headers=headers)['id']
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+owner_token},
                      timeout=60,trust_env=False) as http:
        text, done = stream(http,job)
    verify_stop(api,job,'succeeded')
    assert mark in text and mark in api.call('GET',f'/jobs/{job}/log')['chunk']
    assert api.call('POST',owner_route+'/jobs',allowed,extra_headers=headers)['id']==job
    report.update(same_idempotency_key_allowed_once=True,job_success_with_verified_stop=job,sse_done=done['type']=='done')

    shell = {'command':"test \"$(id -u)\" = '65534' && echo JOB_ADMISSION_CANCEL_READY && sleep 40",
             'runtime':'wsl','timeout_seconds':65}
    report['stage'] = 'submit_wsl'
    launched_at = time.monotonic()
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+owner_token},
                      timeout=75,trust_env=False) as http:
        response = http.post(owner_route+'/jobs',json=shell)
        assert response.status_code==200,response.text
        active = response.json()['id']
    report['wsl_submit_seconds'] = round(time.monotonic()-launched_at,3)
    report['stage'] = 'read_wsl_output'
    deadline = time.monotonic()+50
    while time.monotonic()<deadline:
        state = api.call('GET',f'/jobs/{active}')
        assert state['status'] in {'queued','running'},state
        if 'JOB_ADMISSION_CANCEL_READY' in state.get('log_backlog',''):
            break
        time.sleep(.2)
    else:
        raise TimeoutError('Nonprivileged WSL job did not start')
    report['stage'] = 'request_wsl_cancellation'
    assert api.call('POST',f'/jobs/{active}:cancel',{})['ok']
    report['stage'] = 'verify_wsl_stop'
    verify_stop(api,active,'cancelled')
    for name in ('check_job_quota','request','start_job'):
        assert trace(api)[len(old_trace):].count(name)==2
    report.update(stage='finished',wsl_uid=65534,cancelled_job_with_verified_stop=active)


def run(database_url=None, store=None):
    api = IsolatedServer('job-command-admission',app='admission_fixture:app',database_url=database_url,access_log=True)
    module = api.root/'admission_fixture.py'
    module.write_text('import os\nos.environ.update('+repr(store.settings() if store else {})+')\n'+FIXTURE,
                      encoding='utf8')
    print('JOB_COMMAND_ADMISSION_EVIDENCE',api.root,flush=True)
    report = {'passed':False,'database':'postgresql' if database_url else 'sqlite',
              'storage':'authenticated_seaweedfs' if store else 'fs','actual_model_calls':0,'simulated_llm_calls':0}
    try:
        with api:
            exercise(api,report)
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'description':str(error)[:900]}
        raise
    finally:
        module.unlink(missing_ok=True)
        (api.root/'result-job-admission.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')


async def main(postgres, s3):
    from live_llm_terminal_agent_acceptance import disposable_database
    from s3testsupport import ObjectStore

    storage = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                          volume_max=8,authenticated=True) if s3 else nullcontext()
    async with disposable_database(postgres) as database_url:
        with storage as store:
            await asyncio.to_thread(run,database_url,store)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    enforce([Path(__file__)])
    asyncio.run(main(args.postgres,args.s3))
