"""Durable launch intent and observed startup identity, before payload release.

Events distinguish preparation from running registration. Only the original
worker holding the actual process may record aborted-start cleanup. A missing
acknowledgement never authorizes another launch or invents a stop receipt.
"""
import hashlib
import json

from sqlalchemy import select

from ..db import SessionLocal
from ..models import DurableEvent
from . import job_execution, job_worker_identity, process_record, process_stop
from .durable_events import json_text
from .workflow_state import ExecutionConflict, TERMINAL

INTENT = 'attempt.launch_intended'
PREPARED = 'attempt.launch_prepared'
ABORTED = 'attempt.launch_aborted'
OBSERVED = 'attempt.launch_observed'
SCHEMA = 'vibeflow.job-launch-intent.v1'


def sha(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()


def binding(job, attempt):
    job_worker_identity.document(job, attempt)
    return {'case_id': job.case_id, 'job_id': job.id, 'attempt_id': attempt.id,
        'worker_id': attempt.worker_id, 'claim_sha256': sha(attempt.claim_token),
        'worker_sha256': sha(attempt.worker_identity_json), 'input_sha256': sha(attempt.input_manifest_json)}


async def event(db, job, attempt, kind):
    rows = list(await db.scalars(select(DurableEvent).where(
        DurableEvent.attempt_id == attempt.id, DurableEvent.type == kind)))
    if not rows:
        return None
    if len(rows) != 1 or rows[0].case_id != job.case_id:
        raise ValueError('Launch history is duplicated or crosses case ownership')
    return json.loads(rows[0].payload_json)


async def intent(db, job, attempt):
    value = await event(db, job, attempt, INTENT)
    if value is None:
        return None
    return validate_intent(value, job, attempt)


def validate_intent(value, job, attempt):
    """Validate the same persisted binding when retained in a recovery proof."""
    expected = binding(job, attempt)
    fields = set(expected) | {'schema', 'launch_nonce', 'runtime', 'gate_sha256', 'identity_probe_sha256'}
    if (not isinstance(value, dict) or set(value) not in (fields,fields|{'preparation_contract'}) or value['schema'] != SCHEMA
            or any(value[key] != item for key, item in expected.items())
            or value['runtime'] != json.loads(attempt.input_manifest_json)['execution_spec']['runtime']):
        raise ValueError('Launch intent no longer matches the attempt')
    import re
    for key in ('launch_nonce', 'gate_sha256', 'identity_probe_sha256'):
        if not isinstance(value[key], str) or not re.fullmatch('[0-9a-f]{64}', value[key]):
            raise ValueError('Malformed launch intent identity')
    if 'preparation_contract' in value:
        from .job_allocation_recovery import validate_launch_contract
        validate_launch_contract(value['preparation_contract'])
    return value


def match(value, record, job, attempt):
    process_record.for_attempt(record, job, attempt)
    if value is None or any(value[key] != record[key] for key in (
            'launch_nonce', 'runtime', 'gate_sha256', 'identity_probe_sha256')):
        raise ValueError('Observed process differs from the durable launch intent')
    from .owned_process import native_payload
    argv = json.loads(attempt.input_manifest_json)['execution_spec'].get('argv')
    if argv:
        _, distribution, nonroot = native_payload(argv, record['launch_nonce'])
        if distribution is not None and (record['native'] is None or record['native']['distribution'] != distribution):
            raise ValueError('Observed native distribution differs from the execution specification')
        if nonroot and (record['native'] is None or record['native']['process']['uid'] == 0):
            raise ValueError('Observed process lacks the requested unprivileged identity')


async def reserve(owner, process):
    """Commit once before any native preparation. Duplicate delivery may not spawn."""
    from .owned_process import GATE, PROBE
    async with SessionLocal() as db:
        job, attempt = await job_execution._owned(db, owner)
        from .job_write_checkpoint import assert_native_open_locked
        await assert_native_open_locked(db, job, attempt)
        if (attempt.phase != 'starting' or job.status in TERMINAL
                or attempt.worker_id != job_execution.WORKER_ID or attempt.cancel_requested_at is not None):
            raise ExecutionConflict('state_conflict', 'Launch intent requires the original starting owner')
        if await event(db, job, attempt, INTENT) is not None:
            raise ExecutionConflict('recovery_required', 'This attempt already has a launch intent')
        spec = json.loads(attempt.input_manifest_json)['execution_spec']
        value = {**binding(job, attempt), 'schema': SCHEMA, 'launch_nonce': process.nonce,
            'runtime': spec['runtime'], 'gate_sha256': hashlib.sha256(GATE.read_bytes()).hexdigest(),
            'identity_probe_sha256': hashlib.sha256(PROBE.read_bytes()).hexdigest()}
        if job_worker_identity.document(job,attempt)['process']['platform']=='windows':
            from .job_allocation_recovery import launch_contract
            value['preparation_contract']=launch_contract()
        await job_execution.attempt_event(db, job, attempt, INTENT, value)
        await db.commit()


async def prepared(owner, process):
    """Persist the actual waiting gate; this neither marks running nor releases it."""
    await process.verify_registration()
    async with SessionLocal() as db:
        job, attempt = await job_execution._owned(db, owner)
        from .job_write_checkpoint import assert_native_open_locked
        await assert_native_open_locked(db, job, attempt)
        if (attempt.phase != 'starting' or job.status in TERMINAL
                or attempt.worker_id != job_execution.WORKER_ID or attempt.cancel_requested_at is not None):
            raise ExecutionConflict('state_conflict', 'Prepared launch no longer owns the starting attempt')
        record = process_record.bind(process.record, job, attempt, job_execution.WORKER_ID)
        match(await intent(db, job, attempt), record, job, attempt)
        from .process_preparation import matches_record
        observation=await latest_observation(db,job,attempt)
        if observation is not None:matches_record(observation,record)
        previous = await event(db, job, attempt, PREPARED)
        if previous is not None:
            if previous != {'identity': record}:
                raise ExecutionConflict('state_conflict', 'A different launch was already observed')
            return
        await job_execution.attempt_event(db, job, attempt, PREPARED, {'identity': record})
        await db.commit()


async def latest_observation(db,job,attempt):
    from .process_preparation import chain
    rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
        DurableEvent.type==OBSERVED).order_by(DurableEvent.seq)))
    if any(row.case_id!=job.case_id for row in rows):raise ValueError('Preparation observation crosses case ownership')
    return chain([json.loads(row.payload_json) for row in rows],await intent(db,job,attempt))


async def observed(owner,value):
    from .process_preparation import chain
    async with SessionLocal() as db:
        job,attempt=await job_execution._owned(db,owner)
        # Cancellation may arrive while the original owner's waiting gates are
        # being identified. Keep their real observations for startup cleanup;
        # prepared()/release_process() still reject a cancelled launch.
        if attempt.phase not in {'starting','cancelling'} or attempt.worker_id!=job_execution.WORKER_ID:
            raise ExecutionConflict('state_conflict','Preparation no longer owns the starting attempt')
        rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
            DurableEvent.type==OBSERVED).order_by(DurableEvent.seq)))
        if any(row.case_id!=job.case_id for row in rows):raise ValueError('Preparation observation crosses case ownership')
        existing=[json.loads(row.payload_json) for row in rows]
        if existing and existing[-1]==value:return
        chain(existing+[value],await intent(db,job,attempt))
        await job_execution.attempt_event(db,job,attempt,OBSERVED,value)
        await db.commit()


async def require_prepared(db, job, attempt, record):
    value = await intent(db, job, attempt)
    if value is None:
        return  # Historical callers without the new launch protocol remain explicit legacy history.
    match(value, record, job, attempt)
    if await event(db, job, attempt, PREPARED) != {'identity': record}:
        raise ValueError('Running registration does not match the observed launch')


async def aborted(owner, process):
    """Keep actual identity/cleanup even when the registration commit lost its ack.

    Persisting an identity here describes a stopped startup helper; it does not
    retroactively claim a running transition or permission to release payload.
    Unknown/failed cleanup stays explicit and cannot grant a drain checkpoint.
    """
    if process.record is None or process.stop_result is None:
        return
    async with SessionLocal() as db:
        job, attempt = await job_execution._owned(db, owner)
        if attempt.worker_id != job_execution.WORKER_ID or attempt.phase not in {'starting', 'running', 'cancelling'}:
            raise ExecutionConflict('state_conflict', 'Startup cleanup no longer belongs to this owner')
        record = process_record.bind(process.record, job, attempt, job_execution.WORKER_ID)
        match(await intent(db, job, attempt), record, job, attempt)
        stop = process_stop.validate(process.stop_result, record)
        registered = json.loads(attempt.process_identity_json)
        if registered and registered != record:
            raise ExecutionConflict('state_conflict', 'Startup cleanup differs from committed registration')
        value = {'identity': record, 'stop': stop, 'running_registration_committed': bool(registered),
            'release_observation': 'sent' if process.released else 'unconfirmed'}
        previous = await event(db, job, attempt, ABORTED)
        if previous is not None:
            if (not isinstance(previous, dict) or set(previous) != set(value)
                    or type(previous['running_registration_committed']) is not bool
                    or any(previous[key] != value[key] for key in value if key != 'running_registration_committed')):
                raise ExecutionConflict('state_conflict', 'Startup cleanup was already recorded differently')
            return
        attempt.process_identity_json = json_text(record)
        await job_execution.attempt_event(db, job, attempt, ABORTED, value)
        await db.commit()
