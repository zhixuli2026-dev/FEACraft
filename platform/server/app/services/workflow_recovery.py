"""Read-only recovery handoff; mutations retain the original workflow gates."""
import json
import uuid

from sqlalchemy import select

from ..models import Case, DurableEvent, WorkflowRun, WorkflowStep
from . import workflow_execution as execution, workflow_plan_migration as migration
from .workflow_state import ExecutionConflict, TERMINAL


async def job_reference(db, job, attempt):
    if attempt is None or attempt.step_id is None:
        return None
    step = await db.get(WorkflowStep, attempt.step_id)
    run = await db.get(WorkflowRun, step.run_id) if step else None
    if run is None or run.case_id != job.case_id:
        return None
    return {'run_id': run.id, 'step_id': step.id, 'step_key': step.step_key}


def lineage(plan):
    if not isinstance(plan, dict):
        return None
    values = [plan.get(key) for key in ('recomputation', 'legacy_migration', 'resumption') if plan.get(key) is not None]
    if len(values) != 1 or not isinstance(values[0], dict):
        return None
    value = values[0]
    try:
        if (str(uuid.UUID(value.get('source_run_id'))) != value['source_run_id']
                or type(value.get('source_run_version')) is not int or value['source_run_version'] < 0):
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    return value


async def links(db, source, *, before_seq=None, limit=20):
    # The indexed case/sequence window is paged even when damaged candidates
    # are skipped. Both the immutable event and the target plan must agree.
    query = select(DurableEvent).where(DurableEvent.case_id == source.case_id,
        DurableEvent.type == 'workflow.created', DurableEvent.payload_json.contains(source.id))
    if before_seq is not None:
        query = query.where(DurableEvent.seq < before_seq)
    events = list(await db.scalars(query.order_by(DurableEvent.seq.desc()).limit(limit + 1)))
    result = []
    for event in events[:limit]:
        try:
            payload = json.loads(event.payload_json)
            target = await db.get(WorkflowRun, event.run_id) if event.run_id else None
            relation = lineage(json.loads(target.plan_json)) if target else None
            if (not isinstance(payload, dict) or payload.get('source_run_id') != source.id
                    or target is None or target.id == source.id or target.case_id != source.case_id
                    or relation is None or relation.get('source_run_id') != source.id):
                continue
            result.append(dict(execution.run_summary(target), audit_event_id=event.id,
                audit_event_sequence=event.seq, source_run_version=relation.get('source_run_version')))
        except (ValueError, TypeError, AttributeError):
            continue
    return {'items': result, 'next_before_seq': events[limit-1].seq if len(events) > limit else None}


async def context(db, run_id, user, *, before_seq=None, limit=20):
    source = await execution.owned(db, run_id, user, write=False)
    case = await db.get(Case, source.case_id, populate_existing=True)
    reason = None
    action = None
    preview = None
    detail = dict(execution.run_summary(source), steps=[], parameters={})
    budget = None
    try:
        detail = await execution.detail(db, source.id, user)
        budget = json.loads(source.budget_json)
        preview = await migration.preview(db, source.id, user)
        if source.status not in TERMINAL:
            raise ExecutionConflict('state_conflict', '请先完成或取消原流程，并核验所有原作业已结束')
        await migration.source_drained(db, case, source)
        action = 'migrate' if preview['required'] else 'recompute'
    except ExecutionConflict as error:
        reason = {'code': error.code, 'message': error.detail}
    except (ValueError, TypeError, KeyError, AttributeError):
        reason = {'code': 'evidence_invalid', 'message': '原流程计划或执行记录无法核验，请保留现场人工核查'}
    # A damaged plan must still have an inspectable identity, never a fabricated
    # graph or an executable substitute for the original request.
    parent = None
    try:
        relation = lineage(json.loads(source.plan_json))
        candidate = await db.get(WorkflowRun, relation.get('source_run_id')) if relation else None
        if candidate and candidate.id != source.id and candidate.case_id == source.case_id:
            parent = execution.run_summary(candidate)
    except (ValueError, TypeError, AttributeError):
        pass
    return {'run': detail, 'current_revision': case.dict_revision, 'current_stage': case.workflow_state,
        'budget': budget, 'migration': preview, 'action': action, 'reason': reason, 'source': parent,
        'successors': await links(db, source, before_seq=before_seq, limit=limit)}


async def lookup(db, run_id, user, *, key, action, values):
    """Resolve a completed rejection before a user can replace its saved key.

    A network timeout alone never authorizes discarding a pending confirmation.
    Authorization precedes both a receipt and a negative lookup.
    """
    from . import job_execution, workflow_recompute, workflow_scheduler as scheduler
    from .case_evidence import safe_run_dir
    source = await execution.owned(db, run_id, user)
    job_execution.validate_key(key)
    payload = dict(values, operation='workflow.' + action, source_run_id=source.id)
    if action == 'recompute':
        plan = json.loads(source.plan_json)
        payload.update(target=values.get('target', source.target),
            run_dir=safe_run_dir(values.get('run_dir', source.run_dir)),
            parameters=scheduler.normalized_parameters(values.get('parameters', plan.get('parameters', {}))),
            budget=values.get('budget', json.loads(source.budget_json)))
    elif action == 'migrate':
        payload['parameters'] = scheduler.normalized_parameters(values['parameters'])
        payload['run_dir'] = safe_run_dir(values['run_dir'])
    target = await workflow_recompute.replay(db, source.case_id, user.id, key, job_execution.request_hash(payload))
    return {'found': target is not None, 'run': await execution.detail(db, target.id, user) if target else None}
