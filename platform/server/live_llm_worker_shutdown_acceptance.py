"""Real Redis workers: graceful finish, emergency drain, user cancellation, restart and DeepSeek."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import time

from live_llm_acceptance import FIXTURE, source
from live_llm_sandbox_recovery_acceptance import model_read
from testsupport import IsolatedServer, SERVER
from workflow_batch_redis_support import Broker, Workers
import workflow_batch_redis_support as worker_support

ROOT = SERVER.parents[1]


async def verify(configuration):
    api = IsolatedServer('worker-shutdown', app='shutdown_fixture:app')
    broker = Broker(api.root)
    workers = Workers(api, broker, None)
    original_worker = worker_support.WORKER
    result = {'passed': False, 'mock_llm_calls': 0, 'checks': [], 'jobs': []}
    def save():
        (api.root/'shutdown-result.json').write_text(json.dumps(result, indent=2)+'\n', 'utf8')
    async def call(method, path, **kwargs):
        deadline = time.monotonic()+45
        while True:
            try:
                return await asyncio.to_thread(api.call, method, path, **kwargs)
            except AssertionError as error:
                if ('storage_quota_busy' not in str(error) and 'validation_busy' not in str(error)) or time.monotonic()>=deadline:
                    raise
                await asyncio.sleep(.2)
    async def wait(job_id, predicate):
        deadline = time.monotonic()+120
        while time.monotonic()<deadline:
            job = await call('GET', '/jobs/'+str(job_id))
            if predicate(job):
                return job
            await asyncio.sleep(.2)
        raise TimeoutError('Actual job did not reach required state: '+str(job_id))
    async def submit(code, kind=None):
        body = {'command': 'python3 -c '+shlex.quote(code), 'runtime': 'wsl', 'timeout_seconds': 180}
        if kind:
            body['type'] = kind
        return await call('POST', api.prefix+'/jobs', body=body)
    async def record(job_id, expected, name):
        job = await wait(job_id, lambda j: j['status']==expected and bool(j['attempt'].get('process_stop')))
        assert job['attempt']['process_stop']['verified'], job['attempt']['process_stop']
        result['jobs'].append({'id': job_id, 'status': job['status'], 'attempt': job['attempt']})
        result['checks'].append(name)
        save()
        print('WORKER_SHUTDOWN_PASS '+name, flush=True)
        return job
    async def start(grace):
        worker_support.WORKER = original_worker.replace('await worker.close();', f'await worker.close(grace_seconds={grace});')
        await asyncio.to_thread(workers.start)
    try:
        await broker.start()
        (api.root/'shutdown_fixture.py').write_text('import os\nos.environ.update('+repr(broker.settings())+')\n'+FIXTURE, 'utf8')
        with api:
            await asyncio.to_thread(api.create_case, 'Actual worker shutdown')
            await start(10)
            job = await submit("import time\nprint('WORKER_RUNNING',flush=True)\ntime.sleep(2)\n")
            await wait(job['id'], lambda j: 'WORKER_RUNNING' in j.get('log_backlog', ''))
            await asyncio.to_thread(workers.stop)
            await record(job['id'], 'succeeded', 'graceful shutdown completes the running command before worker exit')

            await start(0)
            job = await submit("import time\nprint('WORKER_RUNNING',flush=True)\ntime.sleep(120)\n")
            await wait(job['id'], lambda j: 'WORKER_RUNNING' in j.get('log_backlog', ''))
            await asyncio.to_thread(workers.stop)
            interrupted = await record(job['id'], 'interrupted', 'emergency shutdown drains native execution and persists interrupted')
            assert interrupted['attempt']['process_stop']['reason']=='ownership_lost'

            await start(1)
            job = await submit("import time\nprint('WORKER_RUNNING',flush=True)\ntime.sleep(120)\n")
            await wait(job['id'], lambda j: 'WORKER_RUNNING' in j.get('log_backlog', ''))
            await asyncio.to_thread(workers.stop)
            await record(job['id'], 'interrupted', 'expired graceful deadline drains and persists interrupted')

            await start(0)
            job = await submit("import time\nprint('WORKER_RUNNING',flush=True)\ntime.sleep(120)\n")
            await wait(job['id'], lambda j: 'WORKER_RUNNING' in j.get('log_backlog', ''))
            await call('POST', '/jobs/'+str(job['id'])+':cancel')
            await record(job['id'], 'cancelled', 'explicit user cancellation remains cancelled')
            job = await submit("from pathlib import Path\nimport secrets\nPath('Ref/proof.txt').write_text('RETIREMENT_'+secrets.token_hex(16)+'\\n')\n", 'convert')
            await record(job['id'], 'succeeded', 'restarted workers execute and publish a fresh command')
            proof = (await call('GET', api.prefix+'/raw?path=Ref/proof.txt', raw=True)).decode().strip()
            result['real_model_calls'] = await asyncio.to_thread(model_read, api, '', '', proof, configuration,
                already_restored=True, path='Ref/proof.txt')
            await asyncio.to_thread(workers.stop)
            result['workers'] = workers.receipts
            result['passed'] = True
    except BaseException as error:
        result['failure_type'] = type(error).__name__
        raise
    finally:
        try:
            if workers.running:
                await asyncio.to_thread(workers.stop)
        finally:
            worker_support.WORKER = original_worker
            await broker.stop()
            result['redis_stops'] = broker.stops
            save()
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--review', type=Path, required=True)
    args = parser.parse_args()
    review = json.loads(args.review.read_text('utf8'))
    assert review['entry']=='platform/server/live_llm_worker_shutdown_acceptance.py'
    for name, digest in review['sources'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest, name
    await verify(await source(args.source_profile_id))


if __name__=='__main__':
    asyncio.run(main())
