"""Snapshot-bound recovery decisions, never execution or cleanup authority."""
import hashlib
import json

from sqlalchemy import inspect, select

from ..models import Case, DurableEvent, Job, JobAttempt, JobExecutionHead
from .durable_events import append_event, json_text, lock_case_execution, now

EVENT = 'job.recovery_required'
RESOLVED = 'job.recovery_cleared'
SCHEMA = 'vibeflow.job-recovery-decision.v1'
POLICIES = {
    'owner_live': 'await_original_owner',
    'legacy_unverified': 'manual_review',
    'lineage_unverified': 'manual_review',
    'unclaimed_delivery': 'delivery_reconciliation',
    'input_recovery_required': 'review_inputs_and_resubmit',
    'owner_unverified': 'verify_original_owner',
    'publication_recovery_required': 'publication_reconciliation',
    'registration_recovery_required': 'registration_reconciliation',
    'state_recovery_required': 'manual_review',
    'process_cleanup_unverified': 'verify_process_cleanup',
    'finalizing_io_unverified': 'manual_review',
    'finalizing_stage_recovery_required': 'recover_preparation',
}


async def rows(db, job_id, *, lock=False):
    job = await db.get(Job, job_id, populate_existing=True)
    if job is None:
        return None, None, None, None
    if lock:
        await lock_case_execution(db, job.case_id)
        await db.refresh(job)
    case = await db.get(Case, job.case_id, populate_existing=True)
    attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id)
        .order_by(JobAttempt.number.desc()).limit(1).execution_options(populate_existing=True))
    head = await db.get(JobExecutionHead, attempt.root_job_id, populate_existing=True) if attempt else None
    return case, job, attempt, head


def fingerprint(records):
    # Include lease/heartbeat, inputs, cancellation, outputs and active lineage.
    # Only the digest is persisted or exposed; raw claims stay private.
    def document(row):
        if row is None:
            return None
        result = {}
        for column in inspect(type(row)).columns:
            value = getattr(row, column.key)
            result[column.key] = value.isoformat() if hasattr(value, 'isoformat') else value
        return result
    return hashlib.sha256(json_text([document(row) for row in records]).encode('utf8')).hexdigest()


async def latest(db, job, attempt):
    query = select(DurableEvent).where(DurableEvent.case_id == job.case_id,
        DurableEvent.type.in_((EVENT, RESOLVED)), DurableEvent.attempt_id == (attempt.id if attempt else None))
    # Legacy jobs share a null attempt link, so also check the explicit Job ID.
    for event in await db.scalars(query.order_by(DurableEvent.seq.desc())):
        try:
            payload = json.loads(event.payload_json)
            if isinstance(payload, dict) and payload.get('job_id') == job.id:
                return event, payload
        except (ValueError, TypeError):
            continue
    return None, None


async def record(db, records, reason, owner=None):
    """Caller holds the case lock and commits with its reconciliation decision."""
    _, job, attempt, _ = records
    body = {'schema': SCHEMA, 'job_id': job.id,
        'attempt_id': attempt.id if attempt else None, 'snapshot_sha256': fingerprint(records),
        'reason': reason, 'next_step': POLICIES[reason], 'owner': owner,
        'replayed': False, 'stop_evidence_created': False}
    _, previous = await latest(db, job, attempt)
    if previous and {k: v for k, v in previous.items() if k != 'observed_at'} == body:
        return
    # Case/Job linkage is enough even when a damaged workflow link prevents
    # constructing a valid workflow event. Keep the attempt link when valid.
    links = {'attempt_id': attempt.id} if attempt else {}
    await append_event(db, job.case_id, RESOLVED if reason == 'owner_live' else EVENT,
        {**body, 'observed_at': now().isoformat()}, **links)


async def public(db, job):
    records = await rows(db, job.id)
    _, current, attempt, _ = records
    if current is None:
        return None
    event, value = await latest(db, current, attempt)
    if value is None:
        return None
    if value.get('reason') == 'owner_live':
        return None
    if (value.get('schema') != SCHEMA or value.get('snapshot_sha256') != fingerprint(records)
            or value.get('reason') not in POLICIES
            or value.get('next_step') != POLICIES[value['reason']]
            or value.get('attempt_id') != (attempt.id if attempt else None)
            or value.get('replayed') is not False or value.get('stop_evidence_created') is not False):
        return None
    return {'state': 'requires_attention', 'reason': value['reason'], 'next_step': value['next_step'],
        'owner': value.get('owner'), 'observed_at': value['observed_at'], 'event_id': event.id,
        'automatic_replay': False}
