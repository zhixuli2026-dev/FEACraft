"""作业失败的阶段回退与证据时间线。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import uuid
from sqlalchemy import select
from sqlalchemy.orm import aliased

from .. import storage
from .async_ops import finish_before_cancel
from .casedict import CASE_DICT_PATH, get_workflow_state, prepare_case_dict, set_workflow_state, write_case_dict
from .case_events import build_case_event, prepare_case_events, CASE_EVENTS_PATH
from .case_write import CaseWriteConflict, bounded_case_read, coordinate
from .file_read import FileRead, FileReadConflict

JOB_STAGE = {"geometry": "A", "mesh": "B", "solve": "C",
             "postprocess": "D", "convert": "E", "report": "E", "udf": "V"}
STAGE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "V": 5}
REQUIRED = 'workflow.failure_fallback_required'
APPLIED = 'workflow.failure_fallback_applied'
SCHEMA = 'vibeflow.failure-fallback.v1'
SOURCE = 'job-failure-fallback'


def event_id(job, kind):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'vibeflow/failure-fallback/{job.case_id}/{job.id}/{kind}'))


async def record_required(db, job, attempt=None):
    """Persist with the failed Job transaction; no filesystem side effects."""
    if job.type not in JOB_STAGE:
        return None
    from ..models import Case, DurableEvent, JobAttempt
    from .durable_events import append_event, lock_case_execution
    await lock_case_execution(db, job.case_id)
    previous = await db.get(DurableEvent, event_id(job, REQUIRED))
    if previous is not None:
        return previous
    if attempt is None:
        attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id)
            .order_by(JobAttempt.number.desc()).limit(1))
    case = await db.get(Case, job.case_id, populate_existing=True)
    payload = {'schema':SCHEMA, 'case_id':case.id, 'job_id':job.id, 'job_type':job.type,
        'attempt_id':attempt.id if attempt else None, 'exit_code':job.exit_code,
        'observed_revision':case.dict_revision, 'observed_state':case.workflow_state}
    return await append_event(db, case.id, REQUIRED, payload,
        attempt_id=attempt.id if attempt else None, event_id=event_id(job, REQUIRED))


@dataclass(frozen=True)
class FallbackPlan:
    case_id: int
    job_id: int
    attempt_id: str
    required_id: str
    required_payload: str
    applied_id: str
    previous: str
    details: dict

    def result(self):
        return {'previous_state':self.previous,'target_state':self.details['target_state'],
                'regressed':self.details['regressed']}


def _replay(applied, case_id, job_id, attempt_id, required_id):
    from .workflow_state import ExecutionConflict

    value = json.loads(applied.payload_json)
    if (not isinstance(value, dict) or not isinstance(value.get('result'), dict) or
            applied.case_id != case_id or applied.type != APPLIED or applied.attempt_id != attempt_id or
            value.get('required_event_id') != required_id or value.get('job_id') != job_id):
        raise ExecutionConflict('evidence_invalid', 'Failure fallback receipt belongs to another source')
    return dict(value['result'], replayed=True)


async def _inspect(db, case, job_id, *, create_intent=False):
    from ..models import ArtifactPublication, DurableEvent, Job, JobAttempt
    from .artifact_publication import current
    from .job_delivery import occupying
    from .job_write_checkpoint import validate as validate_drain
    from .workflow_state import ExecutionConflict

    job = await db.get(Job, job_id, populate_existing=True)
    if job is None or job.case_id != case.id or job.status != 'failed':
        raise ExecutionConflict('state_conflict', 'Only this case\'s failed Job can request stage fallback')
    if job.type not in JOB_STAGE:
        return None
    attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id)
        .order_by(JobAttempt.number.desc()).limit(1).execution_options(populate_existing=True))
    required = await db.get(DurableEvent, event_id(job, REQUIRED), populate_existing=True)
    if required is None and create_intent:
        required = await record_required(db, job, attempt)
    if required is None:
        raise ExecutionConflict('recovery_required', 'Failure fallback requires its committed intent')
    receipt = json.loads(required.payload_json)
    expected = {'schema','case_id','job_id','job_type','attempt_id','exit_code','observed_revision','observed_state'}
    if (not isinstance(receipt, dict) or set(receipt) != expected or receipt['schema'] != SCHEMA or
            required.case_id != case.id or required.type != REQUIRED or receipt['job_id'] != job.id or
            receipt['case_id'] != case.id or receipt['job_type'] != job.type or receipt['exit_code'] != job.exit_code or
            required.attempt_id != (attempt.id if attempt else None) or receipt['attempt_id'] != required.attempt_id or
            type(receipt['observed_revision']) is not int or receipt['observed_revision'] < 0 or
            receipt['observed_state'] not in STAGE_ORDER):
        raise ExecutionConflict('evidence_invalid', 'Failure fallback intent no longer matches its Job and attempt')
    applied_id = event_id(job, APPLIED)
    applied = await db.get(DurableEvent, applied_id, populate_existing=True)
    if applied is not None:
        return _replay(applied, case.id, job.id, required.attempt_id, required.id)
    if attempt is None or attempt.phase != 'failed' or occupying(case, job, attempt):
        raise ExecutionConflict('recovery_required', 'Failure fallback requires original process stop and write-drain proof')
    try:
        validate_drain(case, job, attempt)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ExecutionConflict('recovery_required', 'Failure fallback cannot infer a stopped writer from terminal status alone') from error
    reservation = json.loads(attempt.publication_json)
    superseded_by = None
    if reservation.get('publication_id'):
        failed = await db.get(ArtifactPublication, reservation['publication_id'], populate_existing=True)
        if failed is not None and failed.case_id == case.id:
            _, active = await current(db, case.id, failed.kind)
            if active is not None and active.generation > failed.generation:
                superseded_by = active.id
    previous, target = case.workflow_state, JOB_STAGE[job.type]
    regressed = superseded_by is None and STAGE_ORDER[target] < STAGE_ORDER[previous]
    if regressed and (case.dict_revision != receipt['observed_revision'] or previous != receipt['observed_state']):
        raise ExecutionConflict('input_stale', 'Engineering state changed after this failure; explicit recovery review is required')
    details = {'job_id':job.id,'job_type':job.type,'exit_code':job.exit_code,'target_state':target,
               'regressed':regressed,'log_path':job.log_s3_key,'superseded_by_publication_id':superseded_by,
               'reason':f'作业失败，exit_code={job.exit_code}'}
    return FallbackPlan(case.id, job.id, attempt.id, required.id, required.payload_json, applied_id, previous, details)


async def _record_applied(db, case, plan):
    from .durable_events import append_event

    await append_event(db, case.id, APPLIED, {'schema':SCHEMA,'job_id':plan.job_id,
        'required_event_id':plan.required_id,'result':plan.result(),'revision':case.dict_revision},
        attempt_id=plan.attempt_id, event_id=plan.applied_id)


def _prepare(source, plan):
    from .case_dict_edits import Draft, read_member
    from .provenance import PROVENANCE_PATH
    from .workflow_state import ExecutionConflict

    prefix = source.s3_prefix
    raw = None
    if plan.details['regressed']:
        try:
            raw, _ = read_member(prefix, CASE_DICT_PATH)
            if raw is None:
                raise FileNotFoundError(CASE_DICT_PATH)
            text = raw.decode('utf-8')
        except (FileNotFoundError, FileReadConflict, UnicodeDecodeError) as error:
            raise ExecutionConflict('input_stale', 'caseDict is missing, unreadable, or changed during fallback') from error
        if get_workflow_state(text) != plan.previous:
            raise ExecutionConflict('input_stale', 'caseDict stage no longer matches the committed case stage')
    timeline, _ = read_member(prefix, CASE_EVENTS_PATH)
    receipt = dict(plan.result(), replayed=False)
    if raw is not None:
        provenance, _ = read_member(prefix, PROVENANCE_PATH)
        prepared = prepare_case_dict(source, set_workflow_state(text, plan.details['target_state']), source=SOURCE,
            allow_failure_regression=True, existing_files={CASE_EVENTS_PATH:timeline, PROVENANCE_PATH:provenance},
            extra_events=[('workflow.fallback',SOURCE,plan.previous,plan.details)])
        expected = {CASE_DICT_PATH:hashlib.sha256(raw).hexdigest(),**prepared.expected_files}
        return Draft(source, prepared, prepared.files, expected, receipt)
    event = build_case_event(source, 'workflow.fallback', SOURCE, plan.previous, plan.details)
    files = {CASE_EVENTS_PATH:prepare_case_events(source, [event], existing=timeline or b'')}
    before = None if timeline is None else {'size':len(timeline),'sha256':hashlib.sha256(timeline).hexdigest()}
    return Draft(source, None, files, {CASE_EVENTS_PATH:before}, receipt)


async def settle_failure_fallback(db, case, job) -> dict | None:
    """Commit a failed Job's fallback after the failed Job itself is durable."""
    from ..models import Case, DurableEvent
    from . import case_dict_edits, case_journal_recovery
    from .case_write import assert_case_available
    from .durable_events import lock_case_execution
    from .workflow_state import ExecutionConflict

    original = case_dict_edits.Source.from_case(case)
    workspace_id = case.workspace_id
    assert_case_available(case)
    plan = await _inspect(db, case, job.id)
    if not isinstance(plan, FallbackPlan):
        return plan

    async def checked(session):
        await lock_case_execution(session, original.id)
        current = await session.get(Case, original.id, populate_existing=True)
        assert_case_available(current)
        if (current.workspace_id, current.s3_prefix) != (workspace_id, original.s3_prefix):
            raise CaseWriteConflict('case_source_changed', '失败回退的算例来源已变化')
        return current

    def build(source):
        if source != original:
            raise CaseWriteConflict('revision_conflict', '失败回退期间算例状态已变化')
        return _prepare(source, plan)

    @asynccontextmanager
    async def publication(session, current, _draft):
        observed = await _inspect(session, current, plan.job_id)
        if isinstance(observed, dict):
            raise CaseWriteConflict('fallback_already_applied', '失败回退已提交')
        if observed != plan:
            raise ExecutionConflict('input_stale', 'Failure fallback authority changed while preparing history')
        yield
        await _record_applied(session, current, plan)

    try:
        if not storage._use_s3():
            pending_journal = case_journal_recovery.pending_local(case)
            if pending_journal is not None:
                await finish_before_cancel(db.commit())
                await case_journal_recovery.recover_local(checked, pending_journal)
        return await case_dict_edits.publish_draft(db, case, checked, build, revision=original.dict_revision,
            publication=publication, defer_journal=True)
    except CaseWriteConflict as error:
        await finish_before_cancel(db.rollback())
        applied = await db.get(DurableEvent, plan.applied_id, populate_existing=True)
        if applied is not None:
            return _replay(applied, plan.case_id, plan.job_id, plan.attempt_id, plan.required_id)
        if error.path == CASE_DICT_PATH:
            raise ExecutionConflict('input_stale', 'caseDict changed before the fallback could commit') from error
        raise


async def settle_retry_source(db, case, job):
    """Finish a recorded fallback before reusing the failed attempt."""
    from ..models import Case, DurableEvent, Job
    from .case_dict_edits import Source
    from .case_journal_recovery import DeferredFileRecovery
    from .case_provision import check_deadline
    from .workflow_state import ExecutionConflict

    if job.status!='failed' or job.type not in JOB_STAGE:
        return
    required=await db.get(DurableEvent,event_id(job,REQUIRED))
    if required is None:
        return  # Older failures did not all leave a durable fallback intent.
    original=Source.from_case(case)
    identifier=job.id
    while True:
        check_deadline()
        try:
            await settle_failure_fallback(db,case,job)
            return
        except CaseWriteConflict as error:
            timeline_changed=error.code=='file_conflict' and error.path==CASE_EVENTS_PATH
            if storage._use_s3() or not (timeline_changed or isinstance(error,DeferredFileRecovery)):
                raise
        # A competing edit can append to the timeline or leave a pending
        # journal while this fallback prepares. Inspect the current state again.
        await finish_before_cancel(db.rollback())
        await asyncio.sleep(.1)
        case=await db.get(Case,original.id,populate_existing=True)
        job=await db.get(Job,identifier,populate_existing=True)
        if case is None or Source.from_case(case)!=original or job is None or job.status!='failed':
            raise ExecutionConflict('input_stale','重试准备期间原失败作业或算例状态发生变化')


async def apply_failure_fallback(db, case, job) -> dict | None:
    """Compose a fallback in an existing caller-owned transaction."""
    async with coordinate(db, case):
        plan = await _inspect(db, case, job.id, create_intent=True)
        if not isinstance(plan, FallbackPlan):
            return plan
        await _apply_locked(db, case, plan)
        await _record_applied(db, case, plan)
        return dict(plan.result(), replayed=False)


async def pending(*, limit=64, after=0):
    """Bounded scan of explicit modern intents, never arbitrary old failures."""
    if type(limit) is not int or not 1 <= limit <= 256 or type(after) is not int or after < 0:
        raise ValueError('Invalid failure-fallback scan bound')
    from ..db import SessionLocal
    from ..models import DurableEvent, Job, JobAttempt
    required, applied = aliased(DurableEvent), aliased(DurableEvent)
    done = select(applied.id).where(applied.type == APPLIED,
        applied.case_id == required.case_id, applied.attempt_id == required.attempt_id).exists()
    query = (select(Job.id).join(JobAttempt, JobAttempt.job_id == Job.id)
        .join(required, required.attempt_id == JobAttempt.id)
        .where(Job.id > after, Job.status == 'failed', JobAttempt.phase == 'failed',
            required.case_id == Job.case_id, required.type == REQUIRED, ~done)
        .order_by(Job.id).limit(limit))
    async with SessionLocal() as db:
        return list(await db.scalars(query))


async def recover_once(*, after=0):
    from ..db import SessionLocal
    from ..models import Case, Job
    from .workflow_state import ExecutionConflict
    outcomes = {}
    for identifier in await pending(after=after):
        try:
            async with SessionLocal() as db:
                job = await db.get(Job, identifier)
                if job is None:
                    continue
                case = await db.get(Case, job.case_id)
                result = await settle_failure_fallback(db, case, job)
                outcomes[identifier] = result
        except asyncio.CancelledError:
            raise
        except ExecutionConflict as error:
            outcomes[identifier] = error.code
        except Exception as error:
            outcomes[identifier] = type(error).__name__
    return outcomes


async def recover_forever(stop):
    cursor = 0
    while not stop.is_set():
        try:
            outcomes = await recover_once(after=cursor)
            cursor = max(outcomes) if len(outcomes) == 64 else 0
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print('[WARN] Failure fallback recovery deferred: ' + type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


def _read_case_dict(prefix):
    return FileRead(prefix, CASE_DICT_PATH).read()


async def _apply_locked(db, case, plan):
    from .workflow_state import ExecutionConflict

    previous, details = plan.previous, plan.details
    if details['regressed']:
        try:
            raw = await bounded_case_read(_read_case_dict, case.s3_prefix, code='case_dict', label='caseDict 读取')
            content = raw.decode('utf-8')
        except (FileNotFoundError, FileReadConflict, UnicodeDecodeError) as error:
            raise ExecutionConflict('input_stale', 'caseDict is missing, unreadable, or changed during fallback') from error
        if get_workflow_state(content) != previous:
            raise ExecutionConflict('input_stale', 'caseDict stage no longer matches the committed case stage')
        try:
            await write_case_dict(db, case, set_workflow_state(content, details['target_state']),
                source=SOURCE, allow_failure_regression=True,
                expected_files={CASE_DICT_PATH: hashlib.sha256(raw).hexdigest()},
                extra_events=[('workflow.fallback', SOURCE, previous, details)])
        except CaseWriteConflict as error:
            if error.path == CASE_DICT_PATH:
                raise ExecutionConflict('input_stale', 'caseDict changed before the fallback could commit') from error
            raise
    else:
        from .file_transactions import write
        event = build_case_event(case, 'workflow.fallback', SOURCE, previous, details)
        existing = storage.get_bytes(case.s3_prefix, CASE_EVENTS_PATH) if storage.exists(case.s3_prefix, CASE_EVENTS_PATH) else None
        expected = None if existing is None else {'size': len(existing), 'sha256': hashlib.sha256(existing).hexdigest()}
        await write(db, case, {CASE_EVENTS_PATH: prepare_case_events(case, [event], existing=existing or b'')},
                    expected_files={CASE_EVENTS_PATH: expected},
                    content_types={CASE_EVENTS_PATH: 'text/plain; charset=utf-8'})
