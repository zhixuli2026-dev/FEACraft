"""Transactional primitives for read validation outside a case DB transaction.

Final publication source/dependency validation uses these reservations while
reading files outside the case transaction. Case-bound terminal, CLI, visual-
ization, request and Job lifetimes exclude reservations. Unknown historical
writers require recovery. No user-facing acquire endpoint or OS isolation.

All *_locked functions retain the case control lock until the caller commits.
An expired held row STILL blocks content writes. Only a separately committed
revoke/release can admit a writer; rolling back a later file-write transaction
must never resurrect a validator that may have observed the old content.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets

from sqlalchemy import select, text

from ..models import Case, CaseValidationFence
from .case_write import CaseWriteConflict, assert_case_available
from .durable_events import lock_case_execution


class ValidationConflict(CaseWriteConflict):
    def __init__(self, message='算例来源正在校验，请稍后重试', *, stale=False):
        super().__init__('validation_stale' if stale else 'validation_busy', message)


@dataclass(frozen=True)
class Reservation:
    case_id: int
    job_id: int
    attempt_id: str
    generation: int
    token: str = field(repr=False)


def _digest(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _duration(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 3600:
        raise ValueError('Validation lease must be an integer from 1 to 3600 seconds')
    return timedelta(seconds=seconds)


async def database_now(db):
    # PostgreSQL CURRENT_TIMESTAMP is transaction-start time. Query the current
    # clock only after obtaining the control lock, including after lock waits.
    dialect = db.get_bind().dialect.name
    if dialect == 'postgresql':
        return _utc(await db.scalar(text('SELECT clock_timestamp()')))
    if dialect == 'sqlite':
        value = await db.scalar(text("SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')"))
        return _utc(datetime.fromisoformat(value))
    raise RuntimeError('Unsupported case validation database: ' + dialect)


async def assert_writable_locked(db, case_id):
    """Caller holds case control; never revoke as part of a file-write txn."""
    from .storage_operations import admit_locked
    await admit_locked(db, case_id)
    row = await db.get(CaseValidationFence, case_id, populate_existing=True)
    if row is not None and row.status == 'held':
        raise ValidationConflict()


async def admit_native_locked(db, case_id):
    """Case -> native row lock order, before reservation or final release.

    Global sessions have no declared case scope; this is not OS isolation from
    arbitrary paths they or other same-user native processes could access.
    """
    if case_id is None:
        return
    await lock_case_execution(db, case_id)
    case = await db.get(Case, case_id, populate_existing=True)
    assert_case_available(case)
    await assert_writable_locked(db, case_id)


async def admit_session_locked(db, model, identifier):
    # Read the immutable case binding before acquiring/mutating the native row.
    # A missing row is rejected by the caller's exact-owner current() check.
    case_id = await db.scalar(select(model.case_id).where(model.id == identifier))
    await admit_native_locked(db, case_id)


def _staging_settled(case, selected):
    from .. import storage
    prefix = selected.get('source_prefix')
    if prefix is not None and prefix != case.s3_prefix:
        raise ValueError('Native source no longer matches its case')
    stage = selected.get('staging_prefix')
    if stage is not None:
        storage._check_stage_prefix(stage)
        if any(path.exists() for path in (storage._fs_path(stage, '.'),
                storage._staging_manifest_path(stage), storage._terminal_cleanup_marker(stage))):
            raise ValueError('Native staging has not settled')


async def require_sessions_stopped_locked(db, case):
    """Hold case control, including through the subsequent fence reservation.

    New native records take that same lock. Existing rows can only complete;
    expired leases and missing/invalid stop receipts never count as shutdown.
    Job attempts are checked separately; coordinated direct writes reject held rows.
    """
    import json
    from ..models import NativeSession, NativeCliSession, NativeVizSession, NativeRequest
    from . import terminal_records, codex_records, viz_records, native_request_records
    from .sandbox_retirement import verified as verified_retirement
    retirements = {}
    for model, public in ((NativeSession, terminal_records.public),
            (NativeCliSession, codex_records.public), (NativeVizSession, viz_records.public),
            (NativeRequest, native_request_records.public)):
        rows = await db.scalars(select(model).where(model.case_id == case.id)
            .execution_options(populate_existing=True))
        for row in rows:
            try:
                if model in (NativeSession, NativeRequest) and row.retirement_event_id is not None:
                    await verified_retirement(db, row, retirements)
                    # Independent exit evidence fences every original writer.
                    # Preserved staging is not a live writer and remains retained;
                    # administrative relocation still checks it separately.
                    continue
                state = public(row)
                if model is NativeRequest:
                    if row.kind=='job.finalize' and row.phase in {'succeeded','failed'} and row.error!='OwnerExited':
                        from .job_output_finalizing import require_request_stopped
                        await require_request_stopped(db,row)
                        continue
                    if row.kind=='job.finalize' and row.phase=='cancelled':
                        from .finalizing_request_cancellation import read as cancelled_validator
                        await cancelled_validator(db,row)
                        # The terminal fence is checked together with actual
                        # scope drainage and reconciled output windows. Missing
                        # original stop fields stay missing.
                        continue
                    if row.kind=='job.finalize' and row.error=='OwnerExited':
                        from .finalizing_request_recovery import read as recovered_validator
                        await recovered_validator(db,row)
                        # Missing old stop fields stay missing. This separate
                        # certificate checks the exact owner exit, actual scope
                        # closure and retained output credit before exclusion.
                        continue
                    from .sandbox_image_scope import require_closed
                    await require_closed(db,row)
                    from .sandbox_image_reconciliation import verified as verified_cleanup_exit
                    if await verified_cleanup_exit(db, row) is not None:
                        continue
                    if state['phase'] not in {'succeeded', 'failed', 'cancelled'} or state['children'] is None:
                        raise ValueError('Native request is active or unverified')
                    # Request children are durably reserved BEFORE spawning.
                    # An empty terminal journal cannot admit another child.
                    for child in state['children']['children'].values():
                        if (child['phase'] not in {'stopped', 'failed'} or child['stop'] is None
                                or child['stop']['verified'] is not True):
                            raise ValueError('Native request child has no verified stop')
                elif state['phase'] not in {'closed', 'failed'} or state['cleanup_verified'] is not True:
                    raise ValueError('Native session is active or unverified')
                _staging_settled(case, json.loads(row.profile_json))
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                raise CaseWriteConflict('validation_native_busy',
                    '算例仍有原生会话、未核验停止或待处理暂存，暂不能校验来源') from error


async def _owner(db, owner):
    from .job_execution import _owned
    job, attempt = await _owned(db, owner)
    case = await db.get(Case, job.case_id, populate_existing=True)
    assert_case_available(case)
    if job.status != 'running' or attempt.phase != 'finalizing':
        raise ValidationConflict('作业已取消、已结束或不处于发布校验阶段', stale=True)
    return job, attempt


async def reserve_locked(db, owner, *, lease_seconds):
    """Reserve in a SHORT dedicated txn; commit before starting any bulk read.

    Known case-bound sessions/requests must be stopped under this control lock.
    Job write-drain checkpoints are mandatory, including for the validator.
    Does not steal an expired reservation: revoke_expired must commit first.
    """
    duration = _duration(lease_seconds)
    job, attempt = await _owner(db, owner)
    await assert_writable_locked(db, job.case_id)
    case = await db.get(Case, job.case_id, populate_existing=True)
    await require_sessions_stopped_locked(db, case)
    from .job_write_checkpoint import require_drained_locked
    await require_drained_locked(db, case)
    row = await db.get(CaseValidationFence, job.case_id, populate_existing=True)
    stamp = await database_now(db)
    token = secrets.token_urlsafe(32)
    if row is None:
        row = CaseValidationFence(case_id=job.case_id, generation=1)
        db.add(row)
    else:
        row.generation += 1
    row.job_id, row.attempt_id = job.id, attempt.id
    row.status, row.ended_at = 'held', None
    row.token_sha256, row.claim_sha256 = _digest(token), _digest(owner.token)
    row.acquired_at = row.heartbeat_at = stamp
    row.lease_expires_at = stamp + duration
    await db.flush()
    return Reservation(job.case_id, job.id, attempt.id, row.generation, token)


def _matches(row, reservation):
    return (row is not None and row.case_id == reservation.case_id and row.job_id == reservation.job_id
        and row.attempt_id == reservation.attempt_id and row.generation == reservation.generation
        and hmac.compare_digest(row.token_sha256, _digest(reservation.token)))


async def require_current_locked(db, owner, reservation):
    """Check again in the final owned transaction, immediately before commit."""
    job, _ = await _owner(db, owner)
    return await _current_binding_locked(db, job, owner, reservation)


async def _current_binding_locked(db, job, owner, reservation):
    row = await db.get(CaseValidationFence, job.case_id, populate_existing=True)
    if (not _matches(row, reservation) or row.status != 'held'
            or not hmac.compare_digest(row.claim_sha256, _digest(owner.token))):
        raise ValidationConflict('来源校验占用已被释放、撤销或替换', stale=True)
    stamp = await database_now(db)
    if not _utc(row.heartbeat_at) <= stamp < _utc(row.lease_expires_at):
        raise ValidationConflict('来源校验租约已失效或数据库时钟回拨', stale=True)
    return row, stamp


async def release_current_locked(db, owner, reservation):
    """Finish-only release: keep checking the lease after terminal row updates.

    Unlike require_current this grants no read/validation authority. The caller
    has already checked that authority before changing Job/attempt state in the
    same control transaction. Recheck owner/token/clock immediately at commit.
    """
    from .job_execution import _owned
    job, _ = await _owned(db, owner)
    row, stamp = await _current_binding_locked(db, job, owner, reservation)
    row.status, row.ended_at = 'released', stamp
    await db.flush()


async def renew_locked(db, owner, reservation, *, lease_seconds):
    duration = _duration(lease_seconds)
    row, stamp = await require_current_locked(db, owner, reservation)
    row.heartbeat_at, row.lease_expires_at = stamp, stamp + duration
    await db.flush()


async def release_locked(db, reservation):
    """Exact-token cleanup also works after Job cancellation or ownership loss.

    Release reduces authority only. It cannot validate or publish, and cannot
    release a newer reservation. Commit in the final txn, or a cleanup-only txn.
    """
    await lock_case_execution(db, reservation.case_id)
    row = await db.get(CaseValidationFence, reservation.case_id, populate_existing=True)
    if not _matches(row, reservation) or row.status != 'held':
        return False
    row.status, row.ended_at = 'released', await database_now(db)
    await db.flush()
    return True


async def revoke_expired(case_id):
    """Own and COMMIT a short txn before a later writer can acquire control.

    Never call while holding a case lock in another session. Does not cancel,
    claim, restart or infer the death of any native process or Job owner.
    """
    from ..db import SessionLocal
    async with SessionLocal() as db:
        await lock_case_execution(db, case_id)
        row = await db.get(CaseValidationFence, case_id, populate_existing=True)
        if row is None or row.status != 'held':
            return False
        stamp = await database_now(db)
        if _utc(row.heartbeat_at) <= stamp < _utc(row.lease_expires_at):
            return False
        row.status, row.ended_at = 'revoked', stamp
        await db.commit()
        return True
