"""Transactional case event sequence/outbox; delivery is at least once.

append_event never commits or broadcasts. Callers commit it with their state
change. Delivery claims and acknowledgements use fencing tokens; a crash after
publish and before ack deliberately causes redelivery with the same event ID.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import inspect
import json
import uuid

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..models import (Case, CaseEventCounter, DurableEvent, EventOutbox, Job, JobAttempt,
                      WorkflowRun, WorkflowStep, Workspace)


def now():
    return datetime.now(timezone.utc)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


async def ensure_parent_transaction(db):
    """Python 3.11 SQLite legacy mode does not BEGIN for SELECT/SAVEPOINT.

    Start the real parent before a savepoint if the driver has not done so;
    otherwise releasing the savepoint could commit despite Session.rollback().
    Do not change transaction behavior for long-lived read/SSE sessions globally.
    See https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#transactions-with-sqlite-and-the-sqlite3-driver
    """
    if db.get_bind().dialect.name == "sqlite":
        connection = await db.connection()
        raw = await connection.get_raw_connection()
        if not raw.driver_connection.in_transaction:
            await connection.exec_driver_sql("BEGIN")


def validate_payload(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or any(word in key.lower() for word in
                    ("password", "secret", "token", "authorization", "client_key", "access_key")):
                raise ValueError("Credentials are not allowed in durable event payloads")
            validate_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_payload(item)


async def _validate_links(db, case_id, run_id, step_id, attempt_id):
    if await db.get(Case, case_id) is None:
        raise ValueError("Event case does not exist")
    if run_id is not None:
        run = await db.get(WorkflowRun, run_id)
        if run is None or run.case_id != case_id:
            raise ValueError("Event run belongs to another case")
    if step_id is not None:
        step = await db.get(WorkflowStep, step_id)
        run = await db.get(WorkflowRun, step.run_id) if step else None
        if run is None or run.case_id != case_id or (run_id is not None and run.id != run_id):
            raise ValueError("Event step belongs to another run/case")
    if attempt_id is not None:
        attempt = await db.get(JobAttempt, attempt_id)
        job = await db.get(Job, attempt.job_id) if attempt else None
        if job is None or job.case_id != case_id or (step_id is not None and attempt.step_id != step_id):
            raise ValueError("Event attempt belongs to another step/case")
        if run_id is not None and attempt.step_id is None:
            raise ValueError("Standalone attempt does not belong to a workflow run")
        if run_id is not None and attempt.step_id is not None:
            step = await db.get(WorkflowStep, attempt.step_id)
            if step is None or step.run_id != run_id:
                raise ValueError("Event attempt belongs to another run")


async def lock_case_execution(db, case_id):
    """Serialize control writes without consuming an event sequence (caller commits)."""
    dialect = db.get_bind().dialect.name
    insert = {"sqlite": sqlite_insert, "postgresql": pg_insert}.get(dialect)
    if insert is None:
        raise RuntimeError("Unsupported durable event database: " + dialect)
    await db.execute(insert(CaseEventCounter).values(case_id=case_id, next_seq=1)
                     .on_conflict_do_nothing(index_elements=[CaseEventCounter.case_id]))
    await db.execute(update(CaseEventCounter).where(CaseEventCounter.case_id == case_id)
        .values(next_seq=CaseEventCounter.next_seq))


async def reserve_case_sequence(db, case_id):
    await lock_case_execution(db, case_id)
    return (await db.execute(update(CaseEventCounter).where(CaseEventCounter.case_id == case_id)
        .values(next_seq=CaseEventCounter.next_seq + 1).returning(CaseEventCounter.next_seq))).scalar_one() - 1


async def append_event(db, case_id, event_type, payload, *, run_id=None, step_id=None,
                       attempt_id=None, reserved_seq=None, event_id=None):
    if not isinstance(payload, dict) or not event_type or len(event_type) > 80:
        raise ValueError("Invalid durable event")
    validate_payload(payload)
    if event_id is not None and str(uuid.UUID(event_id)) != event_id:
        raise ValueError('Invalid durable event identity')
    encoded = json_text(payload)
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise ValueError("Durable event payload exceeds 256 KiB")
    await _validate_links(db, case_id, run_id, step_id, attempt_id)
    seq = reserved_seq if reserved_seq is not None else await reserve_case_sequence(db, case_id)
    event = DurableEvent(id=event_id or str(uuid.uuid4()), case_id=case_id, seq=seq, type=event_type,
        payload_json=encoded, run_id=run_id, step_id=step_id, attempt_id=attempt_id)
    db.add(event)
    await db.flush()
    db.add(EventOutbox(event_id=event.id))
    await db.flush()
    return event


async def publish_to_local_bus(event, session_factory):
    """Preserve jobs.updated fields for legacy clients and add durable identifiers."""
    from .eventbus import publish
    from .event_consumers import consume, requires_files
    from . import indexsvc
    from .async_ops import finish_before_cancel
    async with session_factory() as db:
        case = await db.get(Case, event.case_id)
        workspace = await db.get(Workspace, case.workspace_id) if case else None
        if workspace is None:
            raise LookupError("Event owner no longer exists")
        payload = json.loads(event.payload_json)
        if event.type == 'jobs.updated' and payload.get('status') == 'succeeded' and event.attempt_id:
            from .job_execution import completion_visibility
            attempt = await db.get(JobAttempt, event.attempt_id)
            if attempt is None or attempt.job_id != payload.get('job_id'):
                raise ValueError('Completion event has no matching attempt')
            if completion_visibility(attempt) in {'pending', 'invalid'}:
                # The immutable publication is committed, but readers/SSE/worker
                # still wait for compatibility completion. Retry this same event.
                raise RuntimeError('Job completion is not yet visible')
        user_id, prefix = workspace.user_id, case.s3_prefix
        # Serialize derived reads with business file transactions. A committed
        # older event must not index another writer's still-uncommitted bytes.
        from .case_read_capacity import reading
        from .trash import case_unavailable
        # Lifecycle notifications have no derived file reads. Reserving the
        # owner's entire free capacity for them can starve the very storage
        # operation whose start/end events are being delivered.
        if requires_files(event, payload) and not case_unavailable(case.id, case.workspace_id):
            # A retained storage operation already prevents derived reads.
            # Defer before reserving owner capacity; otherwise the dispatcher
            # repeatedly takes all free credit only to fail the case fence,
            # starving that operation's remote writes. This is an early refusal,
            # not admission: reading() still checks again under the case lock.
            from .case_journal_recovery import check_operation_for_delivery
            async def already_consumed():
                return await finish_before_cancel(asyncio.to_thread(indexsvc.already_consumed,
                    event.case_id, prefix, event.id, event.type, payload))
            if not await already_consumed():
                await check_operation_for_delivery(db, case)
                async with reading(db, case, defer_if_case_busy=True, skip_if_consumed=already_consumed,
                                   defer_journal=True) as snapshot:
                    if snapshot is not None:
                        await consume(event, prefix, payload)
    from .event_replay import envelope
    publish(user_id, envelope(event))


def _ready(at):
    return or_(and_(EventOutbox.status == "pending", EventOutbox.next_attempt_at <= at),
               and_(EventOutbox.status == "delivering", EventOutbox.lease_expires_at <= at))


async def _recover_deferred_files(session_factory, outbox_id, attempts, reference):
    """Recovery has its own deadline after the delivery lease is released."""
    from .case_journal_recovery import DeferredFileRecovery, recover_bound_case

    try:
        await recover_bound_case(reference, session_factory)
    except Exception as error:
        ready = now() + timedelta(seconds=min(60, 2 ** min(attempts, 5)))
        reason = type(error).__name__
    else:
        ready, reason = now(), ''
    async with session_factory() as db:
        await db.execute(update(EventOutbox).where(EventOutbox.id == outbox_id,
            EventOutbox.status == 'pending', EventOutbox.claim_token.is_(None),
            EventOutbox.attempts == attempts, EventOutbox.last_error == DeferredFileRecovery.__name__)
            .values(next_attempt_at=ready, last_error=reason))
        await db.commit()


async def deliver_pending(session_factory, *, publisher=None, limit=100, lease_seconds=30,
                          delivery_timeout=10, schedule_recovery=None):
    from .case_journal_recovery import DeferredFileRecovery
    from .request_deadline import scope as deadline_scope

    if lease_seconds <= delivery_timeout or delivery_timeout <= 0:
        raise ValueError("Outbox lease must exceed delivery timeout")
    delivered = 0
    for _ in range(limit):
        at, token = now(), uuid.uuid4().hex
        async with session_factory() as db:
            row = (await db.scalars(select(EventOutbox).where(_ready(at))
                .order_by(EventOutbox.next_attempt_at, EventOutbox.id).limit(1))).first()
            if row is None:
                break
            claimed = await db.execute(update(EventOutbox).where(EventOutbox.id == row.id, _ready(at))
                .values(status="delivering", claim_token=token, attempts=EventOutbox.attempts+1,
                        lease_expires_at=at+timedelta(seconds=lease_seconds))
                .execution_options(synchronize_session=False))
            if claimed.rowcount != 1:
                await db.rollback()
                continue
            event = await db.get(DurableEvent, row.event_id)
            outbox_id, attempts = row.id, row.attempts + 1
            await db.commit()
        try:
            if event is None:
                raise LookupError("Missing durable event")
            if publisher is None:
                async with deadline_scope(delivery_timeout):
                    await publish_to_local_bus(event, session_factory)
            else:
                operation = publisher(event)
                if inspect.isawaitable(operation):
                    await asyncio.wait_for(operation, delivery_timeout)
        except Exception as error:
            deferred = publisher is None and isinstance(error, DeferredFileRecovery)
            retry_delay = lease_seconds if deferred else min(60, 2 ** min(attempts, 5))
            async with session_factory() as db:
                reset = await db.execute(update(EventOutbox).where(EventOutbox.id == outbox_id,
                    EventOutbox.status == "delivering", EventOutbox.claim_token == token).values(
                        status="pending", claim_token=None, lease_expires_at=None,
                        next_attempt_at=now()+timedelta(seconds=retry_delay),
                        last_error=type(error).__name__))
                await db.commit()
            if deferred and reset.rowcount == 1:
                if schedule_recovery is None:
                    await _recover_deferred_files(session_factory, outbox_id, attempts, error.reference)
                else:
                    schedule_recovery(outbox_id, attempts, error.reference)
        else:
            async with session_factory() as db:
                ack = await db.execute(update(EventOutbox).where(EventOutbox.id == outbox_id,
                    EventOutbox.status == "delivering", EventOutbox.claim_token == token).values(
                        status="delivered", claim_token=None, lease_expires_at=None,
                        delivered_at=now(), last_error=""))
                await db.commit()
                delivered += int(ack.rowcount == 1)
    return delivered


async def reconcile_outbox_page(session_factory, *, after_id=None, until_id=None, limit=100):
    """Repair only delivery intents for already committed, immutable events.

    A bounded primary-key sweep has a fixed ceiling per pass. New events cannot
    postpone revisiting an older missing intent indefinitely. Existing leases,
    delivery receipts, payloads and business execution records are untouched.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Invalid outbox reconciliation page size')
    async with session_factory() as db:
        if until_id is None:
            until_id = await db.scalar(select(DurableEvent.id).order_by(DurableEvent.id.desc()).limit(1))
        if until_id is None:
            return {'scanned': 0, 'repaired': 0, 'next_after': None, 'until_id': None}
        query = select(DurableEvent.id, EventOutbox.id).outerjoin(EventOutbox,
            EventOutbox.event_id == DurableEvent.id).where(DurableEvent.id <= until_id)
        if after_id is not None:
            query = query.where(DurableEvent.id > after_id)
        rows = list((await db.execute(query.order_by(DurableEvent.id).limit(limit))).all())
        missing = [event_id for event_id, outbox_id in rows if outbox_id is None]
        repaired = 0
        if missing:
            insert = {'sqlite': sqlite_insert, 'postgresql': pg_insert}.get(db.get_bind().dialect.name)
            if insert is None:
                raise RuntimeError('Unsupported outbox reconciliation database')
            result = await db.execute(insert(EventOutbox).values([{'event_id': identity} for identity in missing])
                .on_conflict_do_nothing(index_elements=[EventOutbox.event_id]).returning(EventOutbox.event_id))
            repaired = len(result.scalars().all())
            await db.commit()
        more = len(rows) == limit
        return {'scanned': len(rows), 'repaired': repaired,
            'next_after': rows[-1][0] if more else None, 'until_id': until_id if more else None}


async def dispatch_forever(session_factory, stop, *, interval=0.25):
    loop = asyncio.get_running_loop()
    repair_after = repair_until = None
    repair_at = 0.0
    recovery = None

    async def recover_files(outbox_id, attempts, reference):
        try:
            await _recover_deferred_files(session_factory, outbox_id, attempts, reference)
        except Exception as error:
            print('[WARN] outbox file recovery deferred: ' + type(error).__name__)

    def schedule(outbox_id, attempts, reference):
        nonlocal recovery
        # The durable pending row already holds a retry. Don't accumulate
        # in-memory tasks or prevent unrelated events from delivering.
        if recovery is None or recovery.done():
            recovery = asyncio.create_task(recover_files(outbox_id, attempts, reference))

    try:
        while not stop.is_set():
            try:
                if loop.time() >= repair_at:
                    try:
                        repair = await reconcile_outbox_page(session_factory, after_id=repair_after, until_id=repair_until)
                        repair_after, repair_until = repair['next_after'], repair['until_id']
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        print('[WARN] outbox reconciliation deferred: ' + type(error).__name__)
                    repair_at = loop.time() + 1
                await deliver_pending(session_factory, schedule_recovery=schedule)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print("[WARN] outbox delivery deferred: " + type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        if recovery is not None:
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
