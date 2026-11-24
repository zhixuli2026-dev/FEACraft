"""Real arq completion waits for finalization and refuses failed DB completion."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from testsupport import SERVER
from job_delivery_redis_smoke import Broker
from job_delivery_recovery_smoke import new_api
from job_owner_startup_smoke import shared
from job_terminal_smoke import Collector
from job_release_smoke import wait_file
from workflow_migration_smoke import alembic

WORKER = '''import os
os.environ.update(OVERRIDES)
import asyncio,contextvars,json
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from app.services import distributed_queue,jobrunner,job_execution
from app.services.distributed_worker import create_worker
ROOT=Path(__file__).parent
MODE=MODE_VALUE
failed_commit=contextvars.ContextVar('fixture_commit_failure',default=False)
original_commit=AsyncSession.commit
async def commit(db):
 if failed_commit.get():raise OSError('Injected terminal database commit failure')
 return await original_commit(db)
AsyncSession.commit=commit
original_finish=job_execution._finish_owned
async def finish(*args,**kwargs):
 token=failed_commit.set(MODE=='commit_failure')
 try:return await original_finish(*args,**kwargs)
 finally:failed_commit.reset(token)
job_execution._finish_owned=finish
original_start=jobrunner.start_job
async def start(*args,**kwargs):
 async def finalizer(status,code):
  assert status=='succeeded' and code==0
  (ROOT/'finalizing.ready').touch()
  deadline=asyncio.get_running_loop().time()+45
  while not (ROOT/'finalizing.release').exists():
   assert asyncio.get_running_loop().time()<deadline,'Finalizer fixture timed out'
   await asyncio.sleep(.02)
  if MODE=='finalizer_failure':raise ValueError('Injected invalid finalizer evidence')
 kwargs['on_finish']=finalizer
 return await original_start(*args,**kwargs)
jobrunner.start_job=start
async def main():
 worker=create_worker(redis_settings=distributed_queue.redis_settings,queue_name=os.environ['DELIVERY_FIXTURE_QUEUE'],
  burst=True,handle_signals=False,poll_delay=.1)
 try:
  await worker.async_run()
  result={'complete':worker.jobs_complete,'failed':worker.jobs_failed}
  (ROOT/'worker-finished.json').write_text(json.dumps(result))
  assert result==({'complete':0,'failed':1} if MODE=='commit_failure' else {'complete':1,'failed':0}),result
 finally:await worker.close()
asyncio.run(main())
'''


def verify(broker, loop, url=None):
    checks, receipts = [], []
    for mode in ('normal', 'finalizer_failure', 'commit_failure'):
        queue = 'vf-finalization-' + uuid.uuid4().hex
        values = {'VIBEFLOW_REDIS_HOST': '127.0.0.1', 'VIBEFLOW_REDIS_PORT': str(broker.port),
            'VIBEFLOW_DISTRIBUTED_JOBS': 'true', 'DELIVERY_FIXTURE_QUEUE': queue,
            'VIBEFLOW_JOB_LEASE_SECONDS': '12', 'VIBEFLOW_JOB_HEARTBEAT_SECONDS': '2'}
        api = new_api('redis-finalization-' + mode, url, values, paused=True)
        with api:
            api.create_case('Real Redis finalization ' + mode)
            payload = api.root / 'payload.py'
            payload.write_text('print("FINALIZATION_NATIVE_EXIT_ZERO", flush=True)\n', encoding='utf8')
            job = api.call('POST', api.prefix + '/jobs', {'command': subprocess.list2cmdline([sys.executable, str(payload)])})
            job_id = job['id']
            worker_script = api.root / 'finalization_worker.py'
            worker_script.write_text(WORKER.replace('OVERRIDES', repr({**shared(api), **values}))
                                     .replace('MODE_VALUE', repr(mode)), encoding='utf8')
            env = dict(api.env, PYTHONPATH=str(SERVER) + os.pathsep + api.env.get('PYTHONPATH', ''))
            async def queue_size():
                client = Redis(host='127.0.0.1', port=broker.port)
                try:
                    return await client.zcard(queue)
                finally:
                    await client.aclose()
            def queued():
                return asyncio.run_coroutine_threadsafe(queue_size(), loop).result(timeout=10)
            with (api.root / 'worker.log').open('w', encoding='utf8') as log:
                worker = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(worker_script)], cwd=SERVER,
                    env=env, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                stream = None
                try:
                    wait_file(api.root / 'finalizing.ready')
                    held = api.call('GET', f'/jobs/{job_id}')
                    assert held['status'] == 'running' and held['attempt']['phase'] == 'finalizing' and held['ended_at'] is None, held
                    assert worker.poll() is None and queued() == 1 and not (api.root / 'worker-finished.json').exists()
                    stream = Collector(api, job_id)
                    assert stream.logged.wait(10) and not stream.closed.is_set()
                    assert not any(e['type'] == 'done' for e in stream.events)
                    checks.append(mode + '_real_arq_message_retained_until_finalization')
                    (api.root / 'finalizing.release').touch()
                    code = worker.wait(timeout=30)
                    assert code == 0, (code, (api.root / 'worker.log').read_text(encoding='utf8'))
                    result = json.loads((api.root / 'worker-finished.json').read_text())
                    final = api.call('GET', f'/jobs/{job_id}')
                    events = stream.wait()
                    assert queued() == 0
                    if mode == 'commit_failure':
                        assert result == {'complete': 0, 'failed': 1}
                        assert final['status'] != 'succeeded' and final['ended_at'] is None
                        assert final['attempt']['phase'] == 'finalizing', final
                        assert not any(e['type'] == 'done' for e in events), events
                        assert any(e['type'] == 'error' for e in events), events
                    else:
                        status = 'failed' if mode == 'finalizer_failure' else 'succeeded'
                        assert final['status'] == status and final['ended_at'] and final['attempt']['process_stop']['verified'], final
                        assert any(e['type'] == 'done' and e['status'] == status for e in events), events
                        assert result == {'complete': 1, 'failed': 0}
                    checks.append(mode + '_queue_result_matches_durable_completion_or_error')
                    receipts.append({'mode': mode, 'job_id': job_id, 'worker': result, 'exit_code': code,
                        'status': final['status'], 'phase': final['attempt']['phase'], 'events': events,
                        'evidence': str(api.root)})
                    print('PASS real_arq_' + mode + '_completion_contract', flush=True)
                finally:
                    (api.root / 'finalizing.release').touch()
                    if worker.poll() is None:
                        worker.terminate()
                        worker.wait(timeout=15)
                    if stream is not None and not stream.closed.is_set():
                        stream.disconnect()
    return {'passed': True, 'count': len(checks), 'checks': checks, 'receipts': receipts,
            'developer_database_touched': False, 'developer_redis_used': False}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--redis-root', type=Path, default=SERVER.parents[1] / 'tmp/delivery-redis-packages/root')
    args = parser.parse_args()
    report = Path(tempfile.mkdtemp(prefix='redis-finalization-evidence-', dir=SERVER.parents[1] / 'tmp'))
    hashes = {p.relative_to(SERVER).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (SERVER / 'app').rglob('*.py')}
    (report / 'source.json').write_text(json.dumps(hashes, indent=2), encoding='utf8')
    broker = Broker(args.redis_root, report)
    try:
        await broker.start()
        loop = asyncio.get_running_loop()
        results = {'sqlite': await asyncio.to_thread(verify, broker, loop)}
        (report / 'sqlite-result.json').write_text(json.dumps(results['sqlite'], indent=2), encoding='utf8')
        if args.postgres:
            from app.config import Settings
            import asyncpg
            url = make_url(Settings(_env_file=SERVER / '.env').database_url)
            assert url.drivername == 'postgresql+asyncpg' and url.host in {'127.0.0.1', 'localhost', '::1'}
            name = 'tmp_vf_redisfinish_' + uuid.uuid4().hex
            admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
                                          password=url.password, database='postgres', timeout=10)
            created = False
            try:
                await admin.execute('CREATE DATABASE "' + name + '"')
                created = True
                target = url.set(database=name).render_as_string(hide_password=False)
                await asyncio.to_thread(alembic, target, report, 'upgrade', 'head')
                results['postgresql'] = await asyncio.to_thread(verify, broker, loop, target)
            finally:
                if created:
                    assert await admin.fetchval('SELECT current_database()') == 'postgres'
                    await admin.execute('DROP DATABASE "' + name + '"')
                await admin.close()
            results['postgresql']['temporary_database_removed'] = True
    finally:
        await broker.stop()
        (report / 'broker-cleanup.json').write_text(json.dumps({'births': broker.births, 'stops': broker.stops}, indent=2), encoding='utf8')
    assert hashes == {p.relative_to(SERVER).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (SERVER / 'app').rglob('*.py')}
    (report / 'result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print('REDIS_FINALIZATION_OK ' + str(report), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
