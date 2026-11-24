"""Real API, Redis workers and Windows/WSL process scopes, with no inference."""
import argparse
import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import JobAttempt
from app.services.process_record import inspect_record
from live_llm_terminal_agent_acceptance import disposable_database
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER
from workflow_batch_redis_support import Broker, Workers


ROOT = SERVER.parents[1]
FIXTURE = 'from app.main import app\n'


async def check_lease_faults(owner, peer, call, job_ids, case_query):
    """Inject lease/identity faults in the disposable DB while both workers run."""
    engine = create_async_engine(owner.env['VIBEFLOW_DATABASE_URL'])
    attempts = JobAttempt.__table__
    now = datetime.now(timezone.utc)
    expired = {'heartbeat_at': now - timedelta(seconds=80),
               'lease_expires_at': now - timedelta(seconds=5)}
    try:
        async with engine.begin() as db:
            rows = {row.job_id: row for row in (await db.execute(select(
                attempts.c.id, attempts.c.job_id, attempts.c.heartbeat_at,
                attempts.c.lease_expires_at, attempts.c.worker_identity_json
            ).where(attempts.c.job_id.in_(job_ids.values()), attempts.c.phase == 'running')))}
            assert set(rows) == set(job_ids.values())
            stale, unverifiable = rows[job_ids['windows']], rows[job_ids['wsl']]
            await db.execute(update(attempts).where(attempts.c.id == stale.id).values(**expired))
            await db.execute(update(attempts).where(attempts.c.id == unverifiable.id).values(worker_identity_json='{}'))
        try:
            owner_view, peer_view, monitor, metrics = await asyncio.gather(
                call(owner, 'GET', case_query), call(peer, 'GET', case_query),
                call(peer, 'GET', '/runtime/monitor'), call(owner, 'GET', '/runtime/metrics', raw=True))
            expected = {f'job:{stale.job_id}': 'expired', f'job:{unverifiable.job_id}': 'unverified'}
            assert owner_view == peer_view
            assert {row['id']: row['lease_state'] for row in peer_view['processes']} == expected, peer_view
            assert monitor['job_lease_states'] == {'expired': 1, 'unverified': 1}, monitor
            alerts = {alert['code'] for alert in monitor['alerts']}
            for state in expected.values():
                assert f'jobs.owner_lease.{state}' in alerts
                assert f'vibeflow_job_process_leases{{state="{state}"}} 1' in metrics.decode('utf8')
            return {'observed': expected, 'alerts': sorted(alerts), 'injected_metadata_faults': True}
        finally:
            async with engine.begin() as db:
                # Preserve a concurrently renewed heartbeat.
                await db.execute(update(attempts).where(
                    attempts.c.id == stale.id, attempts.c.heartbeat_at == expired['heartbeat_at'],
                    attempts.c.lease_expires_at == expired['lease_expires_at']
                ).values(heartbeat_at=stale.heartbeat_at, lease_expires_at=stale.lease_expires_at))
                await db.execute(update(attempts).where(
                    attempts.c.id == unverifiable.id, attempts.c.worker_identity_json == '{}'
                ).values(worker_identity_json=unverifiable.worker_identity_json))
    finally:
        await engine.dispose()


async def run(args, database_url, store):
    owner = IsolatedServer('runtime-supervisor', app='worker_fixture:app', database_url=database_url)
    peer = IsolatedServer('runtime-supervisor-peer', app=owner.app, database_url=database_url)
    broker = Broker(owner.root)
    workers = Workers(owner, broker, store)
    report = {'passed': False, 'model_calls': 0, 'mock_model_calls': 0, 'checks': []}
    evidence = owner.root / 'runtime-supervisor-worker.json'

    async def call(api, method, path, **options):
        return await asyncio.to_thread(api.call, method, path, **options)

    async def wait_job(job_id, predicate):
        async with asyncio.timeout(100):
            while True:
                job = await call(peer, 'GET', f'/jobs/{job_id}')
                if predicate(job):
                    return job
                if job['status'] in {'failed', 'interrupted', 'succeeded'}:
                    raise AssertionError(f"Job {job_id} ended before the expected observation: {job['status']}")
                await asyncio.sleep(.2)

    try:
        await broker.start()
        settings = broker.settings()
        if store:
            settings.update(store.settings())
        if args.lease_faults:
            settings.update(VIBEFLOW_JOB_HEARTBEAT_SECONDS='25', VIBEFLOW_JOB_LEASE_SECONDS='70')
        (owner.root / 'worker_fixture.py').write_text('import os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE, 'utf8')
        with owner:
            case = await asyncio.to_thread(owner.create_case, 'Supervisor cross-worker acceptance')
            outsider = await call(owner, 'POST', '/auth/register', body={
                'email': 'supervisor-outsider@example.com', 'password': 'isolated-test-123', 'name': 'Outsider'})
            outside = {'Authorization': 'Bearer ' + outsider['token']}
            settings.update(VIBEFLOW_JWT_SECRET=owner.env['VIBEFLOW_JWT_SECRET'],
                            VIBEFLOW_DATABASE_URL=owner.env['VIBEFLOW_DATABASE_URL'],
                            VIBEFLOW_STORAGE_FS_ROOT=owner.env['VIBEFLOW_STORAGE_FS_ROOT'])
            (peer.root / 'worker_fixture.py').write_text('import os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE, 'utf8')
            with peer:
                peer.token = owner.token
                if args.lease_faults:
                    owner.env.update(VIBEFLOW_JOB_HEARTBEAT_SECONDS='25', VIBEFLOW_JOB_LEASE_SECONDS='70')
                await asyncio.to_thread(workers.start)
                commands = {
                    'windows': 'echo SUPERVISOR_WINDOWS_ACTIVE & ping -n 110 127.0.0.1 >nul',
                    'wsl': 'printf "SUPERVISOR_WSL_ACTIVE\\n"; sleep 110',
                }
                job_ids = {}
                running = {}
                for runtime, command in commands.items():
                    created = await call(owner, 'POST', owner.prefix + '/jobs', body={
                        'type': 'shell', 'runtime': runtime, 'command': command, 'timeout_seconds': 150})
                    job_ids[runtime] = created['id']
                    running[runtime] = await wait_job(created['id'], lambda job:
                        job['status'] == 'running' and bool(job['attempt'].get('process_identity')))
                case_query = f'/runtime/processes?case_id={case["id"]}'
                case_body = {'case_id': case['id']}
                observations = [await call(api, 'GET', case_query) for api in (owner, peer)]
                monitor = await call(peer, 'GET', '/runtime/monitor')
                assert (await call(peer, 'GET', '/runtime/processes', extra_headers=outside))['processes'] == []
                await call(peer, 'GET', case_query, expect=404, extra_headers=outside)
                await call(peer, 'POST', '/runtime/processes:cancel', body=case_body, expect=404, extra_headers=outside)
                report.update(case_id=case['id'], jobs=job_ids, observed_processes=observations,
                              observed_monitor=monitor)
                identities = {runtime: job['attempt']['process_identity'] for runtime, job in running.items()}
                for runtime, identity in identities.items():
                    observation = await inspect_record(identity)
                    assert observation['state'] == 'match', observation
                    if runtime == 'wsl':
                        assert identity['native']['process']['uid'] == 65534
                report['checks'].append('actual Windows and nonprivileged WSL processes live on independent workers')

                if args.expect_missing:
                    assert all(not observation['processes'] for observation in observations)
                    assert 'job' not in monitor['processes']
                else:
                    expected = {f'job:{item}' for item in job_ids.values()}
                    assert observations[0] == observations[1]
                    assert {item['id'] for item in observations[0]['processes']} == expected, observations
                    assert all(item['phase'] == 'running' and item['lease_state'] == 'current'
                               for item in observations[0]['processes']), observations
                    assert monitor['processes']['job'] == 2 and monitor['job_lease_states'] == {'current': 2}, monitor
                    metrics = (await call(peer, 'GET', '/runtime/metrics', raw=True)).decode('utf8')
                    assert 'vibeflow_processes{kind="job"} 2' in metrics, metrics
                    assert 'vibeflow_job_process_leases{state="current"} 2' in metrics, metrics
                    report['checks'].append('both API replicas and monitoring see the same owned workers and current leases')
                    if args.lease_faults:
                        report['lease_fault_health'] = await check_lease_faults(owner, peer, call, job_ids, case_query)
                        for identity in identities.values():
                            assert (await inspect_record(identity))['state'] == 'match'
                        assert all(row['lease_state'] == 'current' for row in
                                   (await call(peer, 'GET', case_query))['processes'])
                        report['checks'].append('lease alerts keep both native scopes visible without taking ownership')

                result = await call(peer, 'POST', '/runtime/processes:cancel', body=case_body)
                report['cancel'] = result
                if args.expect_missing:
                    assert result['cancelled'] == {} and result['remaining'] == [], result
                    for job_id in job_ids.values():
                        assert (await call(peer, 'GET', f'/jobs/{job_id}'))['status'] == 'running'
                        await call(peer, 'POST', f'/jobs/{job_id}:cancel')
                    report['checks'].append('old process list and case cancel ignored both real workers')
                else:
                    assert result['ok'] and result['cancelled'] == {'job': 2}, result
                closed = {}
                for runtime, job_id in job_ids.items():
                    terminal = await wait_job(job_id, lambda job:
                        job['status'] == 'cancelled' and bool(job['attempt'].get('process_stop')))
                    stop = terminal['attempt']['process_stop']
                    observed = await inspect_record(identities[runtime])
                    assert stop['verified'] and stop['scope_empty'] and observed['state'] == 'exited'
                    closed[runtime] = {'stop': stop, 'observation': observed}
                assert (await call(peer, 'GET', case_query))['processes'] == []
                report['physical_stops'] = closed
                report['checks'].append('durable cancellations yield verified empty scopes and independent OS exit observations')
                await asyncio.to_thread(workers.stop)
                report['workers'] = workers.receipts
        report['passed'] = True
    except BaseException as error:
        report.update(error=type(error).__name__, detail=str(error)[:1500])
        raise
    finally:
        try:
            if workers.running:
                await asyncio.to_thread(workers.stop)
        finally:
            await broker.stop()
            report.update(redis=broker.births, redis_stops=broker.stops,
                          apis_exited=sum(api.proc is not None and api.proc.poll() is not None for api in (owner, peer)))
            evidence.write_text(json.dumps(report, indent=2), encoding='utf8')
            print('SUPERVISOR_WORKER_EVIDENCE ' + str(evidence), flush=True)


async def main(args):
    started = time.monotonic()
    backend = ObjectStore(executable=ROOT / 'third_party/seaweed-4.52-runtime/weed') if args.s3 else nullcontext()
    async with disposable_database(args.postgres) as database_url:
        with backend as store:
            await run(args, database_url, store)
    if args.s3:
        assert backend.proc is not None and backend.proc.poll() is not None
    print(f'SUPERVISOR_WORKER_COMPLETED {time.monotonic() - started:.1f}s', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--s3', action='store_true')
    parser.add_argument('--expect-missing', action='store_true', help='Record the missing behavior on the previous build')
    parser.add_argument('--lease-faults', action='store_true', help='Check expired/unverified monitor states on live jobs')
    args = parser.parse_args()
    if args.expect_missing and args.lease_faults:
        parser.error('--lease-faults requires the shared supervision implementation')
    asyncio.run(main(args))
