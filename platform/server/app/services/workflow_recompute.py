"""Explicit, idempotent new workflows from current dependency observations.

Old runs and attempts are never reset. Reused artifacts have no new Job and
still pass the ordinary pinned engineering confirmation gates before use.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from ..models import WorkflowRun
from .. import storage
from . import dependency_status, workflow_scheduler as scheduler
from .casedict import CASE_DICT_PATH
from .durable_events import append_event, json_text, lock_case_execution
from .workflow_state import ExecutionConflict, TERMINAL

SCHEMA = 'vibeflow.workflow-plan.v3'
FIELDS = {'id', 'kind', 'generation', 'attempt_id', 'manifest_sha256'}


def checked_snapshot(case, *, original_dictionary=None):
    snapshot = scheduler.case_snapshot(case, original_dictionary=original_dictionary)
    if snapshot['state'] != case.workflow_state:
        raise ExecutionConflict('input_stale', 'Engineering files and case mirror disagree')
    return snapshot


def make_plan(target, parameters, recomputation):
    """Rebuild the only supported graph; clients never supply step definitions."""
    if (not isinstance(recomputation, dict) or set(recomputation) != {'source_run_id', 'source_run_version',
            'observed_revision', 'observed_event_sequence', 'reused', 'rebuild'}):
        raise ValueError('Invalid recomputation lineage')
    if not isinstance(recomputation['source_run_id'], str) or str(uuid.UUID(recomputation['source_run_id'])) != recomputation['source_run_id']:
        raise ValueError('Invalid source workflow identity')
    for key in ('source_run_version', 'observed_revision', 'observed_event_sequence'):
        if type(recomputation[key]) is not int or recomputation[key] < 0:
            raise ValueError('Invalid recomputation observation')
    kinds = scheduler.producer_kinds(target,parameters)
    reused, rebuild = recomputation['reused'], recomputation['rebuild']
    if not isinstance(reused, dict) or not isinstance(rebuild, list) or not rebuild:
        raise ValueError('Recomputation must rebuild at least one step')
    boundary = len(reused)
    if set(reused) != set(kinds[:boundary]) or rebuild != kinds[boundary:]:
        raise ValueError('Only a fresh producer prefix may be reused')
    for kind, reference in reused.items():
        if not isinstance(reference, dict) or set(reference) != FIELDS or reference['kind'] != kind:
            raise ValueError('Invalid reused publication identity')
        for key in ('id', 'attempt_id'):
            if not isinstance(reference[key], str) or str(uuid.UUID(reference[key])) != reference[key]:
                raise ValueError('Invalid reused publication UUID')
        if type(reference['generation']) is not int or reference['generation'] < 1:
            raise ValueError('Invalid reused publication generation')
        digest = reference['manifest_sha256']
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid reused publication manifest digest')
    plan = scheduler.make_plan(target, 'A', parameters)
    plan.update(schema=SCHEMA, recomputation=recomputation)
    for step in plan['steps']:
        if step['key'] in reused:
            step['reuse'] = reused[step['key']]
    return plan


def definition(run, key):
    plan = json.loads(run.plan_json)
    return next((s for s in plan['steps'] if s['key'] == key), {})


async def complete_reuse(db, run, case, step, reference):
    """Record an artifact reuse, not a fabricated successful native attempt.

Admission checked the source outside the transaction. This checks the exact DB
binding again; the next explicit confirmation performs full source validation.
"""
    from .artifact_publication import current
    _, row = await current(db, case.id, reference['kind'])
    if row is None or any(getattr(row, key) != value for key, value in reference.items()):
        await scheduler.move_step(db, step, 'blocked', 'input_stale', 'Selected reusable publication is no longer current')
        return
    scheduler.require_inputs(run, case)
    if step.active_attempt_id is not None:
        raise ExecutionConflict('evidence_invalid', 'A reused step cannot own a native attempt')
    step.input_manifest_json = json_text({'reuse': reference})
    plan = json.loads(run.plan_json)
    relation = plan.get('recomputation') or plan.get('resumption')
    step.output_manifest_json = json_text({'publication': dict(reference, status='committed'),
                                          'reuse': {'requested_from_run_id': relation['source_run_id']}})
    await scheduler.move_step(db, step, 'running')
    await scheduler.move_step(db, step, 'finalizing')
    await scheduler.move_step(db, step, 'succeeded')
    await append_event(db, case.id, 'step.artifact_reused', {'publication_id': row.id, 'kind': row.kind},
                       run_id=run.id, step_id=step.id)


async def replay(db, case_id, user_id, key, digest):
    run = await db.scalar(select(WorkflowRun).where(WorkflowRun.case_id == case_id,
        WorkflowRun.created_by == user_id, WorkflowRun.idempotency_key == key))
    if run is not None and run.request_hash != digest:
        raise ExecutionConflict('idempotency_conflict', 'Workflow request key has a different payload')
    return run


async def start(db, run_id, user, *, expected_version, expected_revision, confirmed_reset,
                key, target=None, run_dir=None, parameters=None, budget=None, request=None):
    from ..models import User
    from . import workflow_execution as execution, job_execution, workflow_reset
    from .case_evidence import safe_run_dir
    from .case_provision import io
    from .step_handlers import CONTRACT
    if confirmed_reset is not True or key is None:
        raise ExecutionConflict('confirmation_required', 'Recomputation requires explicit reset confirmation and an idempotency key')
    job_execution.validate_key(key)
    source = await execution.owned(db, run_id, user)
    target = source.target if target is None else target
    run_dir = safe_run_dir(source.run_dir if run_dir is None else run_dir)
    original_plan = json.loads(source.plan_json)
    inherit=parameters is None
    parameters = scheduler.normalized_parameters(original_plan.get('parameters', {}) if parameters is None else parameters)
    budget = json.loads(source.budget_json) if budget is None else budget
    scheduler.make_plan(target, 'A', parameters)
    if (set(budget) != {'timeout_seconds', 'max_log_bytes'} or
            not 0 < budget['timeout_seconds'] <= 86400 or not 1024 <= budget['max_log_bytes'] <= 64*1024*1024):
        raise ExecutionConflict('invalid_request', 'Invalid recomputation budget')
    payload = {'operation': 'workflow.recompute', 'source_run_id': run_id, 'expected_version': expected_version,
        'expected_revision': expected_revision, 'confirmed_reset': True, 'target': target,
        'run_dir': run_dir, 'parameters': parameters, 'budget': budget}
    digest = job_execution.request_hash(payload)
    user_id = user.id
    existing = await replay(db, source.case_id, user_id, key, digest)
    if existing is not None:
        return await execution.detail(db, existing.id, user)
    if original_plan.get('execution_contract') != CONTRACT:
        raise ExecutionConflict('recovery_required', 'Legacy workflow requires registered-plan migration before recomputation')
    if source.status not in TERMINAL or source.version != expected_version:
        raise ExecutionConflict('state_conflict', 'Source workflow must be terminal at the expected version')
    source_case_id, source_plan = source.case_id, source.plan_json
    case, repeated = await workflow_reset.prepare_case(db, source_case_id, user_id, key, digest)
    if repeated is not None:
        return repeated
    await lock_case_execution(db, source_case_id)
    await db.refresh(case)
    existing = await replay(db, source_case_id, user_id, key, digest)
    if existing is not None:
        identifier = existing.id
        await db.commit()
        actor = await db.get(User, user_id, populate_existing=True)
        return await execution.detail(db, identifier, actor)
    if case.dict_revision != expected_revision:
        raise ExecutionConflict('revision_conflict', 'Case revision changed before recomputation')
    from .workflow_plan_migration import source_drained
    source = await db.get(WorkflowRun, run_id, populate_existing=True)
    if source is None or source.status not in TERMINAL or source.version != expected_version or source.plan_json != source_plan:
        raise ExecutionConflict('state_conflict', 'Source workflow changed before recomputation planning')
    await source_drained(db, case, source)
    before = await dependency_status.checkpoint(db, case)
    db.expunge_all()
    await db.rollback()
    document=(await io(storage.get_bytes,case.s3_prefix,CASE_DICT_PATH)).decode('utf-8')
    initial_snapshot = checked_snapshot(case,original_dictionary=document)
    parameters=scheduler.apply_case_execution(document,target,parameters,inherit=inherit)
    graph, reads = await dependency_status.inspect(case, before, run_dir=run_dir)
    channels = {c['kind']: c for c in graph['channels']}
    nodes = {n['publication_id']: n for n in graph['publications']}
    wanted = scheduler.producer_kinds(target,parameters)
    expected = {s['key']: s['handler'] for s in scheduler.make_plan(target, 'A', parameters)['steps']}
    reused, rebuild = {}, []
    from ..models import ArtifactPublication
    for kind in wanted:
        channel = channels.get(kind,{'status':'missing','publication_id':None})
        if channel['status'] == 'unverifiable':
            raise ExecutionConflict('recovery_required', 'Inspect the unverifiable '+kind+' publication before recomputation')
        node = nodes.get(channel['publication_id'], {})
        matching = (channel['status'] == 'fresh' and node.get('handler') == expected[kind]
                    and (kind!='solve' or expected[kind]!='solve.openfoam.mpi'
                         or node.get('subdomains')==parameters['subdomains'])
                    and (kind not in {'udf-build','solve'} or expected[kind] not in {'udf.build.workflow','solve.openfoam.udf'}
                         or node.get('name')==parameters['udf_name'])
                    and (kind != 'report' or node.get('mode') == parameters.get('report_mode', 'compose')))
        if not matching or rebuild:
            rebuild.append(kind)
        else:
            # The inspector has sealed its read set. These identities are taken
            # from the already observed row, never by extending the proof.
            observed = next(r for r in reads.reads.values() if r.operation == 'get'
                            and r.query is ArtifactPublication and r.identity == channel['publication_id'])
            from ..db import SessionLocal
            from .publication_read_set import digest as observed_digest
            async with SessionLocal() as check:
                row = await observed.execute(check)
                if observed_digest(row) != observed.value_sha256:
                    raise ExecutionConflict('input_stale', 'Reusable publication changed during planning')
                reused[kind] = {field: getattr(row, field) for field in FIELDS}
    if not rebuild:
        raise ExecutionConflict('state_conflict', 'The requested target is already current; no recomputation is needed')
    lineage = {'source_run_id': run_id, 'source_run_version': expected_version,
        'observed_revision': expected_revision, 'observed_event_sequence': before['event_sequence'],
        'reused': reused, 'rebuild': rebuild}
    plan = make_plan(target, parameters, lineage)
    from .job_execution_hosts import require_remote
    require_remote(plan)
    existing = await replay(db, source_case_id, user_id, key, digest)
    if existing is not None:
        actor = await db.get(User, user_id, populate_existing=True)
        return await execution.detail(db, existing.id, actor)

    async def validate(session, current, _draft):
        source = await session.get(WorkflowRun, run_id, populate_existing=True)
        if source is None or source.status not in TERMINAL or source.version != expected_version or source.plan_json != source_plan:
            raise ExecutionConflict('state_conflict', 'Source workflow changed during recomputation planning')
        after = await dependency_status.checkpoint(session, current)
        after['event_sequence'] = before['event_sequence']
        if after != before:
            raise ExecutionConflict('input_stale', 'Case changed during recomputation planning')
        await workflow_reset.require_sequence(session, current.id, before['event_sequence'],
                                              'Case changed during recomputation planning')
        await reads.require_current(session)
        active = await session.scalar(select(WorkflowRun.id).where(WorkflowRun.case_id == current.id,
            WorkflowRun.status.not_in(TERMINAL)).limit(1))
        if active is not None:
            raise ExecutionConflict('state_conflict', 'Finish or cancel the active case workflow before resetting its engineering stage')
        await source_drained(session, current, source)

    async def create(session, current, draft):
        run, reset = await workflow_reset.create_run(session, current, user_id, target=target, run_dir=run_dir,
            key=key, digest=digest, budget=budget, plan=plan, draft=draft, reset_name='recomputation_reset')
        await append_event(session, current.id, 'workflow.created', {'status':'queued', 'target':target,
            'version':0, 'step_count':len(plan['steps']), 'source_run_id':run_id,
            'reused_steps':list(reused), 'rebuild_steps':rebuild, 'reset':reset}, run_id=run.id)
        return run.id

    return await workflow_reset.publish(db, case, user_id, key=key, digest=digest, revision=expected_revision,
        expected_sha256=initial_snapshot['case_dict_sha256'], validate=validate, create=create, request=request)
