"""Two actual private attempts share a case. One Redis worker dies; its peer survives."""
import argparse
import asyncio
from contextlib import asynccontextmanager, closing
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import uuid
from urllib.parse import quote


SERVER = Path(__file__).resolve().parent
ROOT = SERVER.parents[1]
CONTROL = '/fixture/attempt-control-owner'
FIXTURE = '''
import os
from fastapi import Depends
from app.main import app
from app.deps import get_current_user
from app.services import process_identity_native as identity

@app.get('/api/v1/fixture/attempt-control-owner')
async def control_identity(user=Depends(get_current_user)):
    return identity.observe(os.getpid())

@app.post('/api/v1/fixture/attempt-control-owner:exit')
async def control_exit(user=Depends(get_current_user)):
    os._exit(19)
'''
OBSERVE_DELIVERY = '''
from app.services import job_delivery
original_deliver = job_delivery.deliver
async def observe_delivery(job_id, **kwargs):
    result = await original_deliver(job_id, **kwargs)
    with (Path(STOP).parent / 'deliveries.jsonl').open('a', encoding='utf8') as stream:
        stream.write(json.dumps({'job_id':job_id, 'result':result}) + '\\n')
    return result
job_delivery.deliver = observe_delivery
'''


def configure(api, settings):
    path = api.root / 'attempt_fixture.py'
    path.write_text('import os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE, encoding='utf8')


async def eventually(read, allowed, label, *, timeout=100):
    try:
        async with asyncio.timeout(timeout):
            while True:
                value = await read()
                if allowed(value):
                    return value
                await asyncio.sleep(.2)
    except TimeoutError:
        raise AssertionError('Timed out while observing ' + label) from None


def command(root, name):
    directory = 'C-run/' + name
    source = f'''from pathlib import Path
import hashlib, json, time
run = Path({directory!r})
field = run / '0/U'
seed = field.read_bytes()
assert seed == {name.encode() + b'-input'!r}
assert (run / '0.orig/U').read_bytes() == {name.encode() + b'-original'!r}
output = run / 'result.txt'
output.write_bytes(seed + b'|first')
if {name!r} == 'left':
    field.write_bytes(b'private-left-edit')
ready = Path({str(root / (name + '-ready.json'))!r})
staged = ready.with_suffix('.pending')
staged.write_text(json.dumps({{'cwd': str(Path.cwd()), 'seed_sha256': hashlib.sha256(seed).hexdigest()}}), encoding='utf8')
staged.replace(ready)
until = time.monotonic() + 150
while not Path({str(root / (name + '-release'))!r}).exists():
    if time.monotonic() > until:
        raise TimeoutError('Fixture release did not arrive')
    time.sleep(.05)
output.write_bytes(seed + b'|finished')
'''
    return f'"{sys.executable}" -c "exec(bytes.fromhex(\'{source.encode().hex()}\'))"'


@asynccontextmanager
async def database_connection(url):
    import asyncpg
    from sqlalchemy.engine import make_url

    target = make_url(url)
    db = await asyncpg.connect(host=target.host, port=target.port or 5432, user=target.username,
                               password=target.password, database=target.database, timeout=10)
    try:
        assert await db.fetchval('SELECT current_database()') == target.database
        yield db
    finally:
        await db.close()


async def database_rows(url, case_id):
    async with database_connection(url) as db:
        return [dict(row) for row in await db.fetch('SELECT j.id,j.status,a.id AS attempt_id,a.phase,'
            'a.worker_identity_json,a.write_checkpoint_json '
            'FROM jobs j JOIN job_attempts a ON j.id=a.job_id WHERE j.case_id=$1 ORDER BY j.id,a.number', case_id)]


async def scope_events(url, attempt_id, event_types):
    async with database_connection(url) as db:
        return [{'seq': row['seq'], 'type': row['type'], 'payload': json.loads(row['payload_json'])}
                for row in await db.fetch('SELECT seq,type,payload_json FROM durable_events '
                    'WHERE attempt_id=$1 AND type=ANY($2::text[]) ORDER BY seq', attempt_id, event_types)]


async def stop_api(api):
    from app.services.process_identity_native import probe

    birth = await asyncio.to_thread(api.call, 'GET', CONTROL)
    assert probe(birth)['state'] == 'match'
    try:
        await asyncio.to_thread(api.call, 'POST', CONTROL + ':exit')
    except OSError:
        pass
    else:
        raise AssertionError('Expected the isolated API to disconnect during self-exit')
    assert await asyncio.to_thread(api.proc.wait, 20) == 19
    state = probe(birth)
    assert state['state'] == 'exited'
    return {'birth': birth, 'exit': state, 'exit_code': 19}


async def exercise(api, peer, broker, workers, settings, store, url, report, *, left_type):
    from arq import create_pool
    from arq.connections import RedisSettings

    from app.services.distributed_queue import QUEUE_NAME
    from app.services.job_launch_registration import INTENT as LAUNCH_INTENT, PREPARED as LAUNCH_PREPARED
    from app.services.job_scope_guardians import OBSERVED as GUARDIAN_OBSERVED, PREPARED as GUARDIAN_PREPARED
    from app.services.process_identity_native import probe
    import workflow_batch_redis_support as support

    async def call(server, method, path, **kwargs):
        return await asyncio.to_thread(server.call, method, path, **kwargs)

    def read_s3(key, *, raw=False):
        client = store.raw_client if raw else store.client
        with closing(client.get_object(Bucket=store.bucket, Key=key)['Body']) as stream:
            return stream.read()

    with api:
        case = await asyncio.to_thread(api.create_case, 'Private attempts with one failed owner')
        original = support.WORKER
        marker = 'while not Path(STOP).exists() and not task.done():await asyncio.sleep(.1)'
        assert original.count(marker) == 1
        support.WORKER = original.replace(marker, 'while not Path(STOP).exists() and not task.done():\n'
             "   if Path(STOP+'.crash').is_file():os._exit(37)\n   await asyncio.sleep(.1)")
        support.WORKER = support.WORKER.replace('async def ready(ctx):', OBSERVE_DELIVERY + '\nasync def ready(ctx):', 1)
        try:
            await asyncio.to_thread(workers.start)
        finally:
            support.WORKER = original
        ids = {}
        for name in ('left', 'right'):
            for relative, suffix in (('0/U', '-input'), ('0.orig/U', '-original')):
                await asyncio.to_thread(api.put, f'C-run/{name}/{relative}', name + suffix)
        for name in ('left', 'right'):
            body = {'type': left_type if name == 'left' else 'convert', 'runtime': 'windows',
                    'command': command(api.root, name), 'timeout_seconds': 200}
            if name == 'left':
                body['input_paths'] = [f'C-run/{name}']
            job = await call(api, 'POST', api.prefix + '/jobs', body=body,
                extra_headers={'Idempotency-Key': 'single-owner-' + name})
            ids[name] = job['id']
        details = {}
        private_roots = {}
        for name, job_id in ids.items():
            ready = api.root / (name + '-ready.json')
            async def marker_bytes():
                return ready.read_bytes() if ready.exists() else None
            private = json.loads(await eventually(marker_bytes, bool, name + ' actual payload'))
            view = await call(api, 'GET', f'/jobs/{job_id}')
            attempt = view['attempt']
            assert view['status'] == 'running' and attempt['workspace']['storage']['backend'] == 's3'
            assert probe(attempt['process_identity']['host'])['state'] == 'match'
            inputs = set(attempt['workspace']['files'])
            own = {f'C-run/{name}/{path}' for path in ('0/U', '0.orig/U')}
            assert own.issubset(inputs)
            if name == 'left':
                assert inputs == own and attempt.get('handler') is None
                assert attempt['workspace']['source']['selection_mode'] == 'explicit'
                assert attempt['workspace']['selection'] == ['C-run/left']
            else:
                assert attempt['handler']['key'] == 'command.convert'
            private_path = Path(private['cwd']).resolve()
            assert private_path.is_relative_to(api.root) and private_path != api.root
            assert (private_path / f'C-run/{name}/result.txt').read_bytes() == (name + '-input|first').encode()
            details[name] = view
            private_roots[name] = private_path
        assert private_roots['left'] != private_roots['right']
        assert (private_roots['left'] / 'C-run/left/0/U').read_bytes() == b'private-left-edit'

        left_attempt = details['left']['attempt']
        launch_types = [LAUNCH_INTENT, GUARDIAN_PREPARED, LAUNCH_PREPARED]
        events = await scope_events(url, left_attempt['id'], launch_types)
        assert [item['type'] for item in events] == launch_types, events
        original_guardian = events[1]['payload']
        assert original_guardian['attempt_id'] == left_attempt['id'] and original_guardian['child_id'] is None
        report['left_launch_events'] = [{'seq': item['seq'], 'type': item['type']} for item in events]
        report['left_input_selection'] = {'paths': left_attempt['workspace']['selection'],
            'files': sorted(left_attempt['workspace']['files']), 'handler': left_attempt.get('handler')}

        rows = await database_rows(url, case['id'])
        owners = {item['id']: json.loads(item['worker_identity_json'])['process'] for item in rows}
        left = next(record for record in workers.running if record['identity'] == owners[ids['left']])
        right = next(record for record in workers.running if record['identity'] == owners[ids['right']])
        assert left is not right
        report['ownership'] = {'left': left['identity'], 'right': right['identity'], 'private_roots':
                                {name: str(path) for name, path in private_roots.items()}}
        left['stop'].with_suffix('.crash').touch()
        assert await asyncio.to_thread(left['process'].wait, 15) == 37
        assert probe(left['identity'])['state'] == 'exited'
        left['log'].close()
        workers.running.remove(left)
        report['left_worker_fault'] = {'exit_code': 37, 'identity': left['identity'], 'actual_exit': True}

        async def left_scope():
            return await asyncio.to_thread(probe, details['left']['attempt']['process_identity']['host'])
        left_exit = await eventually(left_scope, lambda value: value['state'] == 'exited', 'the killed worker scope')
        assert probe(right['identity'])['state'] == 'match'
        assert probe(details['right']['attempt']['process_identity']['host'])['state'] == 'match'
        settings = {**settings, 'VIBEFLOW_JWT_SECRET': api.env['VIBEFLOW_JWT_SECRET']}
        configure(peer, settings)
        with peer:
            peer.token = api.token
            left_path, right_path = (f'/jobs/{ids[name]}' for name in ('left', 'right'))
            left_result = await eventually(lambda: call(peer, 'GET', left_path), lambda row: row['status'] == 'interrupted',
                                           'interrupted status from an independent API')
            assert left_result['attempt']['id'] == details['left']['attempt']['id']
            assert left_result['attempt']['process_stop'] is None
            assert left_result['attempt']['workspace'] == left_attempt['workspace']
            assert (await call(peer, 'GET', right_path))['status'] == 'running'
            async def recovered():
                rows = await database_rows(url, case['id'])
                checkpoint = next(row['write_checkpoint_json'] for row in rows if row['id'] == ids['left'])
                return json.loads(checkpoint)
            proof = await eventually(recovered, lambda data: data.get('state') == 'recovered_stopped',
                                     'the original Windows scope drain certificate')
            assert proof['schema'] == 'vibeflow.job-write-checkpoint.v4' and proof['stop'] is None
            assert proof['recovery']['original_root_stop_missing'] is True
            assert proof['attempt_id'] == details['left']['attempt']['id']
            witnesses = proof['recovery']['guardians']
            assert len(witnesses) == 1 and witnesses[0]['prepared'] == original_guardian
            observed = await scope_events(url, left_attempt['id'], [GUARDIAN_OBSERVED])
            assert len(observed) == 1 and observed[0]['payload'] == witnesses[0]['observed']
            assert observed[0]['payload']['host_scope_verified'] is True
            report['left_guardian_observed_seq'] = observed[0]['seq']
            assert probe(right['identity'])['state'] == 'match'
            assert probe(details['right']['attempt']['process_identity']['host'])['state'] == 'match'
            report['independent_drain_certificate'] = proof
            redis = await create_pool(RedisSettings(host='127.0.0.1', port=broker.port))
            try:
                await redis.enqueue_job('run_delivery', ids['left'], _job_id='left-replay-'+uuid.uuid4().hex,
                                        _queue_name=QUEUE_NAME)
            finally:
                await redis.aclose()
            (api.root / 'right-release').touch()
            right_result = await eventually(lambda: call(peer, 'GET', right_path), lambda row:
                row['status'] == 'succeeded' and bool(row['attempt']['process_stop']) and not row['completion_pending'],
                'surviving worker publication')
            assert right_result['attempt']['id'] == details['right']['attempt']['id']
            assert right_result['attempt']['process_stop']['verified'] and right_result['attempt']['process_stop']['scope_empty']
            right_exit = probe(details['right']['attempt']['process_identity']['host'])
            assert right_exit['state'] == 'exited'
            deliveries = right['root'] / 'deliveries.jsonl'
            async def observed_replay():
                if not deliveries.exists():
                    return None
                return next((value for value in (json.loads(line) for line in deliveries.read_text('utf8').splitlines() if line)
                             if value['job_id'] == ids['left']), None)
            accepted = await eventually(observed_replay, bool, 'actual Redis consumption of the old attempt')
            assert accepted['result'] == 'already_owned_or_terminal', accepted

            prefix = right_result['attempt']['workspace']['storage']['prefix'].rstrip('/')
            publication = right_result['attempt']['outputs']['publication']
            assert publication['status'] == 'committed'
            path = 'C-run/right/result.txt'
            immutable = f'{prefix}/.vibeflow/publications/{publication["id"]}/files/{path}'
            for getter in (lambda: read_s3(immutable, raw=True), lambda: read_s3(f'{prefix}/{path}')):
                assert getter() == b'right-input|finished'
            assert await call(peer, 'GET', api.prefix + '/raw?path=' + quote(path), raw=True) == b'right-input|finished'
            await call(peer, 'GET', api.prefix + '/raw?path=C-run/left/result.txt', expect=404)
            for name, original in details.items():
                for path, suffix in (('0/U', '-input'), ('0.orig/U', '-original')):
                    relative = f'C-run/{name}/{path}'
                    expected = (name + suffix).encode()
                    sealed = original['attempt']['workspace']['inputs_path']
                    assert read_s3(f'{prefix}/{sealed}/{relative}', raw=True) == expected
                    assert read_s3(f'{prefix}/{relative}') == expected
            final_rows = await database_rows(url, case['id'])
            assert [(row['id'], row['attempt_id'], row['status']) for row in final_rows] == [
                (ids[name], details[name]['attempt']['id'], phase) for name, phase in
                (('left', 'interrupted'), ('right', 'succeeded'))]
            report.update(jobs=ids, scopes={'left': left_exit, 'right': right_exit},
                          statuses={'left': 'interrupted', 'right': 'succeeded'}, attempts=len(final_rows),
                          redis_redelivery=accepted, right_publication=publication['id'],
                          right_artifact_sha256=hashlib.sha256(read_s3(immutable, raw=True)).hexdigest(),
                          both_frozen_and_live_initial_fields_match=True, left_output_absent=True)
            report['peer_api'] = await stop_api(peer)
        await asyncio.to_thread(workers.stop)
        report['surviving_workers'] = workers.receipts
        report['original_api'] = await stop_api(api)


async def verify(*, left_type):
    import asyncpg
    from sqlalchemy.engine import make_url

    from app.config import Settings
    from s3testsupport import ObjectStore
    from testsupport import IsolatedServer
    from workflow_batch_redis_support import Broker, Workers
    from workflow_migration_smoke import alembic

    target = make_url(Settings(_env_file=SERVER / '.env').database_url)
    assert target.drivername == 'postgresql+asyncpg' and target.host in {'127.0.0.1', 'localhost', '::1'}
    database = 'tmp_vf_attemptowner_' + uuid.uuid4().hex
    url = target.set(database=database).render_as_string(hide_password=False)
    evidence = Path(tempfile.mkdtemp(prefix='attempt-single-owner-loss-', dir=ROOT / 'tmp'))
    report = {'passed': False, 'database': database, 'database_dropped': False, 'model_calls': 0, 'mock_llm_calls': 0,
              'job_types': {'left': left_type, 'right': 'convert'}}
    print('ATTEMPT_SINGLE_OWNER_EVIDENCE ' + str(evidence), flush=True)
    admin = await asyncpg.connect(host=target.host, port=target.port or 5432, user=target.username,
                                  password=target.password, database='postgres', timeout=10)
    created = False
    api = IsolatedServer('attempt-owner-api', app='attempt_fixture:app', database_url=url)
    peer = IsolatedServer('attempt-owner-reader', app=api.app, database_url=url)
    store = ObjectStore(executable=ROOT / 'third_party/seaweed-4.52-runtime/weed', volume_max=6)
    broker = Broker(api.root)
    workers = Workers(api, broker, store)
    report.update(api_evidence=str(api.root), peer_evidence=str(peer.root), store_evidence=str(store.root))
    try:
        await admin.execute('CREATE DATABASE "' + database + '"')
        created = True
        await asyncio.to_thread(alembic, url, evidence, 'upgrade', 'head')
        await broker.start()
        with store:
            try:
                settings = {**broker.settings(), **store.settings()}
                configure(api, settings)
                await exercise(api, peer, broker, workers, settings, store, url, report, left_type=left_type)
            finally:
                (api.root / 'left-release').touch(exist_ok=True)
                (api.root / 'right-release').touch(exist_ok=True)
                if workers.running:
                    await asyncio.to_thread(workers.stop)
        assert store.proc is not None and store.proc.poll() is not None
        report['passed'] = True
    finally:
        try:
            await broker.stop()
        finally:
            try:
                if created:
                    assert await admin.fetchval('SELECT current_database()') == 'postgres'
                    await admin.execute('DROP DATABASE "' + database + '"')
                    assert not await admin.fetchval('SELECT 1 FROM pg_database WHERE datname=$1', database)
                    report['database_dropped'] = True
            finally:
                await admin.close()
                report.update(redis_stops=broker.stops, store_exited=store.proc is not None and store.proc.poll() is not None)
                report['passed'] = (report['passed'] and report['database_dropped'] and report['store_exited']
                                    and bool(broker.stops) and all(stop['verified'] for stop in broker.stops))
                (evidence / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    assert report['passed']
    print('ATTEMPT_SINGLE_OWNER_PASS ' + str(evidence / 'result.json'), flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sealed-shell-left', action='store_true', help='Reproduce the earlier sealed shell case')
    arguments = parser.parse_args()
    asyncio.run(verify(left_type='shell' if arguments.sealed_shell_left else 'convert'))
