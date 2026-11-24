"""Read-only process observation and durable unresolved-registration audit.

Unknown registration never becomes a running claim, stop receipt or replay.
Both observation and native probes happen after the case transaction closes.
"""
import asyncio
import json

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Case, DurableEvent, Job, JobAttempt, JobExecutionHead
from . import job_launch_registration as launches
from . import job_worker_identity, process_record
from .async_ops import finish_before_cancel
from .durable_events import json_text, lock_case_execution, now
from .job_drain_recovery import fingerprint, snapshot as drain_snapshot
from .workflow_state import TERMINAL

EVENT = 'attempt.registration_recovery_required'
SCHEMA = 'vibeflow.registration-recovery.v1'


async def rows(db, job_id):
    job = await db.get(Job, job_id)
    if job is None:return None, None, None, None
    await lock_case_execution(db, job.case_id)
    await db.refresh(job)
    attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id)
        .order_by(JobAttempt.number.desc()).limit(1).execution_options(populate_existing=True))
    case = await db.get(Case, job.case_id, populate_existing=True)
    head = await db.get(JobExecutionHead, attempt.root_job_id, populate_existing=True) if attempt else None
    return case, job, attempt, head


def snapshot(case, job, attempt, head):
    return {**drain_snapshot(case, job, attempt), 'version': attempt.version,
        'head_attempt': head.active_attempt_id if head else None,
        'cancel_requested_at': attempt.cancel_requested_at.isoformat() if attempt.cancel_requested_at else None,
        'checkpoint': attempt.write_checkpoint_json}


async def history(db, job, attempt):
    from .child_launch_registration import KINDS
    rows = list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id == attempt.id,
        DurableEvent.type.in_({launches.INTENT, launches.PREPARED, launches.ABORTED, launches.OBSERVED,
            'attempt.child_reserved','attempt.child_running','attempt.child_stopped'}|KINDS)).order_by(DurableEvent.seq)))
    return fingerprint([(row.id, row.case_id, row.seq, row.type, row.payload_json) for row in rows])


async def candidate(db, job, attempt):
    if attempt is None or not attempt.claim_token:return False
    if attempt.write_checkpoint_json!='{}':
        from .job_write_checkpoint import validate
        try:validate(await db.get(Case,job.case_id),job,attempt)
        except (ValueError,TypeError,KeyError,AttributeError):pass
        else:return False
    from .child_launch_registration import incomplete_children
    try:
        if incomplete_children(job,attempt):return True
    except (ValueError,TypeError,KeyError,AttributeError):
        # An invalid existing journal is still rejected by drain/active
        # recovery; it is not evidence that startup registration was missing.
        pass
    if attempt.phase == 'starting':return True
    # A cancellation or startup error may have ended the status while its
    # native registration remains missing. Keep that ambiguity visible too.
    if (attempt.phase == 'cancelling' or attempt.phase in TERMINAL) and attempt.process_identity_json == '{}':
        return await db.scalar(select(DurableEvent.id).where(DurableEvent.attempt_id == attempt.id,
            DurableEvent.type == launches.INTENT).limit(1)) is not None
    return False


async def prepared_identity(db, job, attempt):
    value = await launches.intent(db, job, attempt)
    if value is None:return None, 'legacy_registration'
    observed = await launches.event(db, job, attempt, launches.PREPARED)
    if observed is None:return None, 'launch_identity_missing'
    if not isinstance(observed, dict) or set(observed) != {'identity'}:
        raise ValueError('Invalid prepared launch record')
    launches.match(value, observed['identity'], job, attempt)
    preparation=await launches.latest_observation(db,job,attempt)
    if preparation is not None:
        from .process_preparation import matches_record
        matches_record(preparation,observed['identity'])
    registered = json.loads(attempt.process_identity_json)
    if registered and registered != observed['identity']:
        raise ValueError('Prepared and registered process identities disagree')
    return observed['identity'], 'registration_incomplete'


async def reconcile(job_id):
    from .job_allocation_recovery import reconcile as recover_allocation
    if await recover_allocation(job_id):return 'allocation_preparation_drained'
    from .job_linux_namespaces import reconcile as recover_namespace
    if await recover_namespace(job_id,starting=True):return 'startup_scope_drained'
    from .job_scope_recovery import reconcile as recover_scopes
    if await recover_scopes(job_id,starting=True):return 'startup_scope_drained'
    # Reuse ordinary owner-death interruption and scope recovery when the
    # original child preparation is complete. WSL scopes additionally require
    # their original kernel admission and independent retirement. Keep missing running
    # registration and stop callback unchanged in the recovered checkpoint.
    from .child_launch_registration import prepared_histories
    async with SessionLocal() as db:
        case,job,attempt,head=await rows(db,job_id)
        try:
            prepared=(await prepared_histories(db,job,attempt)) if (
                attempt is not None and attempt.phase in {'running','interrupted','cancelling'} and attempt.write_checkpoint_json=='{}') else {}
            from .child_handoff_recovery import histories as unlaunched_histories
            unlaunched=await unlaunched_histories(db,job,attempt) if attempt is not None and attempt.write_checkpoint_json=='{}' else {}
        except (ValueError,KeyError,TypeError,AttributeError):prepared={};unlaunched={}
    if prepared or unlaunched:
        from .job_recovery import reconcile_active_job
        from .job_scope_recovery import reconcile as recover_scopes
        await reconcile_active_job(job_id)
        if await recover_scopes(job_id):return 'child_startup_scope_drained'
    async with SessionLocal() as db:
        case, job, attempt, head = await rows(db, job_id)
        if job is None or not await candidate(db, job, attempt):return None
        before = snapshot(case, job, attempt, head)
        history_hash = await history(db, job, attempt)
        try:
            owner = job_worker_identity.document(job, attempt)
        except (ValueError, TypeError, KeyError, AttributeError):
            owner = None
        try:
            process, reason = await prepared_identity(db, job, attempt)
            preparation = await launches.latest_observation(db,job,attempt)
        except (ValueError, TypeError, KeyError, AttributeError):
            process, preparation, reason = None, None, 'launch_history_invalid'
        from .child_launch_registration import pending
        try:children=await pending(db,job,attempt)
        except (ValueError,TypeError,KeyError,AttributeError):
            children=[];reason='child_launch_history_invalid'
        if children:reason='child_registration_incomplete'
    from . import process_identity_native as native
    observation = (await finish_before_cancel(asyncio.to_thread(native.probe, owner['process']))) if owner else {
        'state': 'unverified', 'reason': 'invalid_or_legacy_worker_identity'}
    if observation['state'] == 'match':return 'owner_live'
    # Never probe a same-named local distribution on behalf of a foreign owner.
    process_observation = (await finish_before_cancel(process_record.inspect_record(process))) if (
        process is not None and observation == {'state': 'exited', 'reason': 'process_exited'}) else None
    from .process_preparation import inspect as inspect_preparation
    known_exit=observation=={'state':'exited','reason':'process_exited'}
    if process_observation is None and preparation is not None and known_exit:
        process_observation=await finish_before_cancel(inspect_preparation(preparation))
    semaphore=asyncio.Semaphore(4)
    async def inspect_child(child):
        async with semaphore:
            result=None
            if known_exit:
                if child['identity'] is not None:result=await finish_before_cancel(process_record.inspect_record(child['identity']))
                elif child['preparation'] is not None:result=await finish_before_cancel(inspect_preparation(child['preparation']))
            return {'child_id':child['child_id'],'reason':child['reason'],'observation':result}
    child_observations=await asyncio.gather(*(inspect_child(child) for child in children))
    observed_at = now().isoformat()
    body = {'schema': SCHEMA, 'snapshot_sha256': fingerprint(before), 'history_sha256': history_hash,
        'reason': reason, 'owner': observation, 'process': process_observation,
        'children':child_observations,
        'replayed': False, 'stop_evidence_created': False}
    async with SessionLocal() as db:
        case, job, attempt, head = await rows(db, job_id)
        if (job is None or not await candidate(db, job, attempt)
                or snapshot(case, job, attempt, head) != before
                or await history(db, job, attempt) != history_hash):
            return 'registration_changed'
        last = await db.scalar(select(DurableEvent).where(DurableEvent.attempt_id == attempt.id,
            DurableEvent.type == EVENT).order_by(DurableEvent.seq.desc()).limit(1))
        if last is not None:
            try:previous = json.loads(last.payload_json)
            except (ValueError, TypeError):previous = None
            if (last.case_id == job.case_id and isinstance(previous, dict)
                    and {key: value for key, value in previous.items() if key != 'observed_at'} == body):
                return 'registration_recovery_required'
        from .job_execution import attempt_event
        await attempt_event(db, job, attempt, EVENT, {**body, 'observed_at': observed_at})
        await db.commit()
        return 'registration_recovery_required'


async def public(db, job, attempt):
    """Authorized Job detail projection; do not expose private claim credentials."""
    try:
        value = await launches.intent(db, job, attempt)
        last = await db.scalar(select(DurableEvent).where(DurableEvent.attempt_id == attempt.id,
            DurableEvent.type == EVENT).order_by(DurableEvent.seq.desc()).limit(1))
        if value is None and last is None:return None
        process, reason = await prepared_identity(db, job, attempt)
        preparation=await launches.latest_observation(db,job,attempt)
        from .child_launch_registration import pending
        children=await pending(db,job,attempt)
        aborted = await launches.event(db, job, attempt, launches.ABORTED)
        stage = 'legacy' if value is None else 'aborted' if aborted is not None else 'registered' if attempt.process_identity_json != '{}' else 'prepared' if process else 'preparing' if preparation else 'intended'
        result = {'stage': stage, 'prepared_identity': process, 'preparation':preparation,'children':children,'recovery': None}
        if last is not None:
            case = await db.get(Case, job.case_id)
            head = await db.get(JobExecutionHead, attempt.root_job_id)
            notice = json.loads(last.payload_json)
            if (notice['schema'] == SCHEMA and notice['snapshot_sha256'] == fingerprint(snapshot(case, job, attempt, head))
                    and notice['history_sha256'] == await history(db, job, attempt)):
                result['recovery'] = {'state': 'requires_attention', 'reason': notice['reason'],
                    'owner_state': notice['owner']['state'],
                    'process_state': notice['process']['state'] if notice['process'] else 'unverified',
                    'children':notice.get('children',[]),
                    'observed_at': notice['observed_at'], 'automatic_restart_allowed': False}
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'stage': 'unverified', 'prepared_identity': None, 'recovery': {
            'state': 'requires_attention', 'reason': 'launch_history_invalid', 'automatic_restart_allowed': False}}
