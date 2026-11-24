"""Actual expired worker lease, pre-registration death and Redis redelivery."""
import asyncio
from contextlib import contextmanager
from copy import copy
from functools import partial
import json
from pathlib import Path
import tempfile
import traceback
import uuid

from arq import create_pool
from arq.connections import RedisSettings
import asyncpg
from sqlalchemy.engine import make_url

from app.services import process_identity_native as identity
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put
from live_llm_professional_worker_death_acceptance import DELIVERY_OBSERVER, kill_owned_worker
from testsupport import SERVER
from workflow_batch_redis_support import Broker, Workers
import workflow_batch_redis_support as support


FAULTS = '''
from app.services import job_execution, job_launch_registration
controls = Path(READY).parent.parent

def witness(name, data):
    target = controls / name
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(data))
    temp.replace(target)

heartbeat = job_execution.heartbeat
async def delay_heartbeat(owner):
    if (controls / 'pause-heartbeats').exists():
        witness('heartbeat-held', {'job':owner.job_id, 'worker_pid':os.getpid()})
        while (controls / 'pause-heartbeats').exists():
            await asyncio.sleep(.1)
    return await heartbeat(owner)
job_execution.heartbeat = delay_heartbeat

observed = job_launch_registration.observed
async def delay_registration(owner, value):
    if value['stage'] == 'created' and (controls / 'pause-registration').exists():
        assert identity.probe(value['host'])['state'] == 'match'
        witness('gate-held', {'job':owner.job_id, 'worker_pid':os.getpid(), 'gate':value['host']})
        while (controls / 'pause-registration').exists():
            await asyncio.sleep(.1)
    return await observed(owner, value)
job_launch_registration.observed = delay_registration
'''


@contextmanager
def armed(root, name):
    marker = root / name
    marker.touch(exist_ok=False)
    try:
        yield
    finally:
        marker.unlink(missing_ok=True)


async def sql_state(url, identifier):
    db = await asyncpg.connect(host=url.host, port=url.port, user=url.username,
                               password=url.password, database=url.database)
    try:
        rows = await db.fetch("""
            SELECT id,phase,lease_expires_at < clock_timestamp() AS expired,
                   process_identity_json <> '{}' AS registered
            FROM job_attempts WHERE job_id=$1 ORDER BY number
        """, identifier)
        assert len(rows) == 1, 'Duplicate or missing SQL attempt'
        state = dict(rows[0])
        recovered = await db.fetch("""
            SELECT payload_json FROM durable_events
            WHERE attempt_id=$1 AND type='attempt.content_scope_recovered'
        """, state['id'])
        state['scope_recoveries'] = [json.loads(row['payload_json']) for row in recovered]
        return state
    finally:
        await db.close()


async def enqueue(port, identifier):
    pool = await create_pool(RedisSettings(host='127.0.0.1', port=port))
    try:
        entry = await pool.enqueue_job('run_delivery', identifier, _job_id='lease-proof-'+uuid.uuid4().hex,
                                       _queue_name='vibeflow:jobs')
        assert entry is not None
    finally:
        await pool.close()


def scenario(api, *, broker, store=None, **_):
    assert api.database_url, 'This check reads the PostgreSQL lease clock directly'
    url = make_url(api.database_url)
    workers = Workers(api, broker, store)
    def start():
        original = support.WORKER
        try:
            support.WORKER = (DELIVERY_OBSERVER + original.replace('async def ready(ctx):', FAULTS+'\nasync def ready(ctx):')
                              .replace('await worker.close();','await worker.close(grace_seconds=0);'))
            workers.start()
        finally:
            support.WORKER = original

    evidence = {'backend':'s3' if store else 'fs', 'deaths':[]}
    def terminate(pid):
        record = next(w for w in workers.running if w['identity']['pid'] == pid)
        evidence['deaths'].append(kill_owned_worker(record))
        workers.running.remove(record)

    def read(name):
        path = api.root / name
        wait_for(path.is_file, 'No worker evidence for '+name, 90)
        return json.loads(path.read_text())

    def redeliver(identifier):
        def count():
            values = [json.loads(line) for file in api.root.glob('batch-worker-*/actual-deliveries.jsonl')
                      for line in file.read_text().splitlines() if line]
            return sum(v['job_id']==identifier and v['actual_result']=='already_owned_or_terminal' for v in values)
        before = count()
        asyncio.run(enqueue(broker.port, identifier))
        wait_for(lambda:count()>before, 'Actual Redis duplicate was not consumed', 60)

    def submit(label, *, hold=False):
        actor = copy(api)
        actor.create_case('Lease and registration '+label)
        marker = api.root / (label+'-executions')
        put(actor, 'Ref/probe.py', "from pathlib import Path\nimport time\nwith Path("+repr(str(marker))+
            ").open('a') as f:f.write('started\\n')\n"+('time.sleep(180)\n' if hold else ''))
        body = {'command':'python Ref/probe.py'}
        key = uuid.uuid4().hex
        # Same key across transient storage admission conflicts.
        def send():
            value = actor.call('POST', actor.prefix+'/jobs', body, expect=(200,409), extra_headers={'Idempotency-Key':key})
            if value.get('id'):
                return value['id']
            assert value['detail']['code'] in {'storage_quota_busy','validation_busy'}, value
        return actor, wait_for(send,'Admission stayed busy'), marker

    try:
        start()
        actor, jid, marker = submit('lease', hold=True)
        wait_for(marker.is_file, 'Business command never started')
        with armed(api.root, 'pause-heartbeats'):
            beat = read('heartbeat-held')
            assert beat['job'] == jid
            before = wait_for(lambda: (s if (s:=asyncio.run(sql_state(url,jid)))['expired'] else None),
                              'Actual SQL lease never expired', 90)
            assert before['registered'] and before['phase']=='running'
            redeliver(jid)
            assert actor.call('GET', f'/jobs/{jid}')['status']=='running'
            assert actor.call('GET','/org/mine')['usage']['occupied_job_slots']==1
            assert marker.read_text().splitlines()==['started']
            terminate(beat['worker_pid'])
        after = actor.wait_job(jid, expected='interrupted', timeout=90)
        assert after['attempt']['id']==before['id']
        redeliver(jid)
        assert marker.read_text().splitlines()==['started']
        wait_for(lambda: actor.call('GET','/org/mine')['usage']['occupied_job_slots']==0,
                 'Verified owner-loss recovery did not free the original claim',60)
        evidence['lease'] = {'job':jid, 'expired_while_owner_alive':before,
            'slots_after_expired_redelivery':1,'slots_after_verified_recovery':0,
            'after_owner_death':asyncio.run(sql_state(url,jid)), 'status':after['status'],
            'actual_executions':1, 'duplicate_deliveries_refused':2}

        with armed(api.root, 'pause-registration'):
            actor, jid, marker = submit('registration')
            prepared = read('gate-held')
            assert prepared['job']==jid and identity.probe(prepared['gate'])['state']=='match'
            before = asyncio.run(sql_state(url,jid))
            assert before['phase']=='starting' and not before['registered']
            initial = actor.call('GET', f'/jobs/{jid}')['attempt']['registration']
            assert initial['stage']=='intended' and initial['preparation'] is None and initial['prepared_identity'] is None
            terminate(prepared['worker_pid'])
        wait_for(lambda:identity.probe(prepared['gate'])['state']=='exited', 'Unreleased gate survived owner death')
        start()
        def reviewed():
            row = actor.call('GET', f'/jobs/{jid}')
            notice = row['attempt']['registration']['recovery']
            return row if row['status']=='interrupted' or notice and notice['state']=='requires_attention' else None
        after = wait_for(reviewed, 'Missing registration never received a recovery decision', 75)
        notice = after['attempt']['registration']['recovery']
        assert after['attempt']['id']==before['id'] and after['attempt']['process_identity'] is None
        if after['status']!='interrupted':
            assert notice['automatic_restart_allowed'] is False and notice['owner_state']=='exited'
            assert notice['reason']=='launch_identity_missing'
        redeliver(jid)
        final = asyncio.run(sql_state(url,jid))
        assert final['id']==before['id'] and not final['registered'] and not marker.exists()
        if after['status']=='interrupted':
            proof, = final['scope_recoveries']
            assert proof['schema'] in {'vibeflow.job-root-startup-recovery.v1','vibeflow.job-launch-preparation-recovery.v1'}
            assert proof['replayed'] is False and proof['original_root_stop_missing'] is True
            assert after['attempt']['reason_code']=='recovery_required'
        occupied=actor.call('GET','/org/mine')['usage']['occupied_job_slots']
        assert occupied==(0 if final['scope_recoveries'] else 1)
        evidence['registration'] = {'job':jid, 'gate':prepared['gate'], 'gate_exited':True,
            'slots_after_reconciliation':occupied,
            'before_crash':before, 'after_redelivery':final,'notice':notice,'actual_executions':0}
    except Exception as error:
        evidence['failure'] = {'type':type(error).__name__, 'frames':[
            (Path(f.filename).name,f.lineno,f.name) for f in traceback.extract_tb(error.__traceback__)[-6:]]}
        (api.root/'lease-registration.json').write_text(json.dumps(evidence,indent=2),encoding='utf8')
        raise
    finally:
        workers.stop()
    evidence['surviving_workers_stopped'] = workers.receipts
    (api.root/'lease-registration.json').write_text(json.dumps(evidence,indent=2),encoding='utf8')
    return evidence


async def verify():
    root = Path(tempfile.mkdtemp(prefix='lease-registration-redis-',dir=SERVER.parents[1]/'tmp'))
    broker = Broker(root)
    try:
        await broker.start()
        await main(selected_override=partial(scenario,broker=broker), environment_overrides=broker.settings())
    finally:
        await broker.stop()


if __name__=='__main__':
    asyncio.run(verify())
