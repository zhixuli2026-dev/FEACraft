"""Conservative startup reconciliation, with no replay or process signalling.

An expired lease cannot establish that its worker exited. Unknown/foreign
owners, unclaimed delivery, incomplete registration and publication recovery
remain untouched until their respective recovery protocols can settle them.
"""
import asyncio
import json
from collections import Counter
from datetime import timezone

from sqlalchemy import select, func

from ..db import SessionLocal
from ..models import Job, JobAttempt, JobLogReservation, OutputArchiveReservation, PublicationStorageReservation, ArtifactPublication
from . import job_execution, job_worker_identity, process_record
from .workflow_state import transition
from . import job_recovery_notice as notices
from .async_ops import finish_before_cancel
from .durable_events import now


async def reconcile_job(job_id):
    from .job_scope_guardians import reconcile as reconcile_guardians
    guardian_witnesses=await reconcile_guardians(job_id)
    from .child_handoff_recovery import recover as recover_child_handoffs
    await recover_child_handoffs(job_id,guardian_witnesses)
    from .job_native_scopes import recover as recover_native_scopes
    await recover_native_scopes(job_id,guardian_witnesses)
    from .job_log_storage import reconcile as reconcile_log
    await reconcile_log(job_id)
    from .output_archive_storage import reconcile as reconcile_archive
    await reconcile_archive(job_id)
    from .publication_storage import reconcile as reconcile_copy_capacity
    await reconcile_copy_capacity(job_id)
    from .publication_alias_recovery import reconcile as reconcile_publication
    publication=await reconcile_publication(job_id)
    if publication is not None:return publication
    from .publication_finalizing import schedule as schedule_finalization
    finalization=await schedule_finalization(job_id)
    if finalization is not None:return finalization
    from .job_registration_recovery import reconcile as reconcile_registration
    registration = await reconcile_registration(job_id)
    if registration is not None:
        return registration
    async with SessionLocal() as db:
        job=await db.get(Job,job_id)
        if job is None:return 'removed'
        status=job.status
        terminal=status not in {'queued','running'}
    if terminal:
        from .job_scope_recovery import reconcile as reconcile_scope_drain
        if await reconcile_scope_drain(job_id):return 'scope_content_drained'
        from . import job_drain_recovery as drain
        if status=='cancelled' and await drain.reconcile_drained_cancellation(job_id):
            return 'drained_cancellation_recovered'
        return await drain.reconcile(job_id)
    outcome=await reconcile_active_job(job_id)
    from .job_scope_recovery import reconcile as reconcile_scope_drain
    if await reconcile_scope_drain(job_id):return 'scope_content_drained'
    return outcome


async def reconcile_active_job(job_id):
    async with SessionLocal() as db:
        records = await notices.rows(db, job_id, lock=True)
        _, job, attempt, head = records
        if job is None:
            return 'removed'
        if job.status not in {'queued', 'running'}:
            return 'terminal'
        unsealed=False
        if attempt and attempt.phase=='queued' and attempt.claim_token is None:
            from .attempt_workspace import require_sealed
            try:require_sealed(job,attempt)
            except job_execution.ExecutionConflict:unsealed=True
        reason = ('legacy_unverified' if attempt is None else
            'lineage_unverified' if head is None or head.active_attempt_id != attempt.id else
            'input_recovery_required' if unsealed else
            'unclaimed_delivery' if attempt.phase == 'queued' and attempt.claim_token is None else None)
        if reason:
            await notices.record(db, records, reason)
            await db.commit()
            return reason
        before = notices.fingerprint(records)
        # The identity probe can block in OS access. Close the transaction first
        # so cancellation and the original worker's heartbeat can still commit.
        detached_job, detached_attempt = job, attempt
    state = await finish_before_cancel(asyncio.to_thread(job_worker_identity.inspect, detached_job, detached_attempt))
    async with SessionLocal() as db:
        records = await notices.rows(db, job_id, lock=True)
        _, job, attempt, head = records
        if job is None:
            return 'removed'
        if notices.fingerprint(records) != before:
            return 'recovery_changed'
        if state.get('reason') == 'different_worker_pid_namespace':
            expires = attempt.lease_expires_at
            if expires and (expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)) > now():
                return 'await_original_owner'
        if state['state'] == 'match':
            _, previous = await notices.latest(db, job, attempt)
            if (previous and previous.get('snapshot_sha256') == before
                    and previous.get('reason') != 'owner_live'):
                await notices.record(db, records, 'owner_live', state)
                await db.commit()
            return 'owner_live'
        from .early_finalization_recovery import interrupt
        if await interrupt(db,records,state):
            await db.commit()
            return 'early_finalization_interrupted'
        early_stage=None
        if (state=={'state':'exited','reason':'process_exited'} and attempt.phase=='finalizing'
                and json.loads(attempt.output_manifest_json).get('schema')!='vibeflow.attempt-outputs.v1'):
            from .finalizing_stage import read as read_stage
            try:early_stage=await read_stage(db,records[0],job,attempt)
            except (ValueError,KeyError,TypeError,AttributeError,job_execution.ExecutionConflict):pass
        reason = ('owner_unverified' if state != {'state': 'exited', 'reason': 'process_exited'} else
            'finalizing_stage_recovery_required' if early_stage is not None and early_stage['supported'] else
            'publication_recovery_required' if attempt.phase == 'finalizing' else
            'registration_recovery_required' if attempt.phase == 'starting' or
                (attempt.phase == 'running' and process_record.public(job, attempt) is None) else
            'state_recovery_required' if attempt.phase not in {'claimed', 'running'} else None)
        if reason:
            await notices.record(db, records, reason, state)
            await db.commit()
            return reason
        # Exact owner exit rules out a later owner callback. This records an
        # interrupted attempt, not a verified child stop or permission to replay.
        await transition(db, 'attempt', attempt.id, 'interrupted', expected_version=attempt.version,
                         claim_token=attempt.claim_token, reason_code='recovery_required',
                         detail='Recorded worker birth has exited; execution was not replayed')
        await job_execution._job_status(db, job, 'interrupted', None, attempt)
        await job_execution.attempt_event(db, job, attempt, 'attempt.owner_exited',
                                         {'job_id': job.id, 'observation': state, 'replayed': False})
        await db.commit()
        return 'owner_exited_interrupted'


RECOVERY_PAGE_SIZE = 64


async def _retain_unreadable_output(job_id):
    """Record a confirmed corrupt manifest without mutating its Job or attempt."""
    async with SessionLocal() as db:
        records = await notices.rows(db, job_id, lock=True)
        _, job, attempt, _ = records
        if job is None or attempt is None:
            return False
        try:
            output = json.loads(attempt.output_manifest_json)
        except (ValueError, TypeError):
            output = None
        if isinstance(output, dict):
            return False
        await notices.record(db, records, 'state_recovery_required')
        await db.commit()
        return True


async def reconcile_startup(*, local_worker=False):
    from . import latex_workspace_cleanup as latex_cleanup
    from .job_drain_recovery import drained_cancellation_candidates

    # Stop at the highest existing ID so new admissions cannot extend this pass.
    async with SessionLocal() as db:
        last = await db.scalar(select(func.max(Job.id)))
    if last is None:
        return {}
    # Include old terminal attempts missing drain metadata. Valid existing
    # checkpoints and never-claimed cancellations need no historical probe.
    query = select(Job.id, Job.command).outerjoin(JobAttempt,JobAttempt.job_id==Job.id).where(
        Job.status.in_(('queued','running')) | (JobAttempt.id.is_(None)) |
        ((JobAttempt.write_checkpoint_json=='{}') & JobAttempt.claim_token.is_not(None)) |
        Job.id.in_(select(JobLogReservation.job_id).where(JobLogReservation.state=='active')) |
        JobAttempt.id.in_(select(OutputArchiveReservation.attempt_id).where(OutputArchiveReservation.state=='active')) |
        JobAttempt.id.in_(select(ArtifactPublication.attempt_id).join(PublicationStorageReservation,
            PublicationStorageReservation.publication_id==ArtifactPublication.id).where(PublicationStorageReservation.state=='active')) |
        latex_cleanup.recovery_candidates() | drained_cancellation_candidates() |
        ((Job.status=='succeeded') & JobAttempt.output_manifest_json.contains('"state":"pending"'))).distinct()
    if local_worker:
        from .process_identity_native import host_id
        latest=select(JobAttempt.job_id.label('job_id'),func.max(JobAttempt.number).label('number')).group_by(JobAttempt.job_id).subquery()
        query=query.join(latest,latest.c.job_id==Job.id).where(JobAttempt.number==latest.c.number,
            JobAttempt.worker_identity_json.contains(host_id()))
    query = query.where(Job.id <= last).order_by(Job.id).limit(RECOVERY_PAGE_SIZE)
    cursor = None
    counts = Counter()
    while True:
        async with SessionLocal() as db:
            jobs = (await db.execute(query if cursor is None else query.where(Job.id > cursor))).all()
        for job_id, command in jobs:
            try:
                counts[await reconcile_job(job_id)] += 1
                if command == latex_cleanup.COMMAND:
                    await latex_cleanup.retire(job_id)
            except Exception as error:
                if isinstance(error, (ValueError, TypeError, AttributeError)) and await _retain_unreadable_output(job_id):
                    counts['unreadable_output_retained'] += 1
                elif local_worker:
                    counts['recovery_error_'+type(error).__name__] += 1
                else:
                    raise
        if len(jobs) < RECOVERY_PAGE_SIZE or jobs[-1][0] == last:
            break
        cursor = jobs[-1][0]
    return dict(counts)
