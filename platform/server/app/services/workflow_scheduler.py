"""Persisted dependency scheduler and explicit engineering confirmation nodes.

No dynamic imports or client-supplied DAGs. Native handler adapters queue through
queue_step, then dispatch the returned Job through the normal owned runner.
Reconcile is repeatable after reconnect and never reruns an existing attempt.
The staged confirmation owns commit; other mutations flush for their caller.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
import re
import uuid

from sqlalchemy import select, update

from .. import storage
from ..models import Case, JobAttempt, WorkflowRun, WorkflowStep
from . import engineering_stage, job_execution
from .case_evidence import case_input_fingerprint, safe_run_dir
from .casedict import CASE_DICT_PATH, get_workflow_state, parse_casedict
from .durable_events import append_event, ensure_parent_transaction, json_text, lock_case_execution, now
from .workflow_state import ExecutionConflict, TERMINAL, transition

DEFINITIONS = (
    ("preflight", "case.preflight", None),
    ("geometry", "geometry.preview", None), ("confirm_B", "engineering.confirm", "B"),
    ("mesh", "mesh.generate", None), ("confirm_C", "engineering.confirm", "C"),
    ("solve", "solve.openfoam", None), ("confirm_D", "engineering.confirm", "D"),
    ("postprocess", "postprocess.run", None), ("confirm_E", "engineering.confirm", "E"),
    ("report", "report.build", None),
)
TARGET_END = {"geometry":"confirm_B", "mesh":"confirm_C", "solve":"confirm_D",
              "postprocess":"confirm_E", "report":"report"}
JOB_TYPES = {"geometry.preview":"geometry", 'geometry.export':'geometry', "mesh.generate":"mesh", "solve.openfoam":"solve", 'solve.openfoam.mpi':'solve',
             'udf.build.workflow':'udf','solve.openfoam.udf':'solve',"postprocess.run":"postprocess", "report.build":"convert"}
FAILED_STEPS = {"failed", "cancelled", "interrupted", "blocked"}
ACTIVE_STEPS = {"ready", "running", "finalizing"}


def normalized_parameters(parameters):
    if not isinstance(parameters, dict) or set(parameters)-{'geometry_mode','report_mode','solve_mode','subdomains','udf_name','placement'}:
        raise ExecutionConflict('invalid_request', 'Unsupported professional workflow parameters')
    mode = parameters.get('geometry_mode','preview')
    if not isinstance(mode,str) or mode not in {'preview','export'}:
        raise ExecutionConflict('invalid_request', 'geometry_mode must be preview or export')
    result={'geometry_mode':mode}
    if 'placement' in parameters:
        if parameters['placement']!='remote':
            raise ExecutionConflict('invalid_request','Remote placement must use a registered deployment worker')
        result['placement']='remote'
    solve_mode=parameters.get('solve_mode','serial')
    if not isinstance(solve_mode,str) or solve_mode not in {'serial','mpi','udf'}:
        raise ExecutionConflict('invalid_request','solve_mode must be serial, mpi or udf')
    if 'solve_mode' in parameters:result['solve_mode']=solve_mode
    if solve_mode=='mpi':
        count=parameters.get('subdomains',2)
        if type(count) is not int or not 1<=count<=64:
            raise ExecutionConflict('invalid_request','MPI subdomains must be an integer from 1 to 64')
        result['subdomains']=count
    elif 'subdomains' in parameters:
        raise ExecutionConflict('invalid_request','subdomains requires explicit MPI solve mode')
    if solve_mode=='udf':
        name=parameters.get('udf_name')
        if not isinstance(name,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,39}',name):
            raise ExecutionConflict('invalid_request','UDF solve requires a valid udf_name')
        result['udf_name']=name
    elif 'udf_name' in parameters:
        raise ExecutionConflict('invalid_request','udf_name requires explicit UDF solve mode')
    if 'report_mode' in parameters:
        if not isinstance(parameters['report_mode'],str) or parameters['report_mode'] not in {'compose','markdown'}:
            raise ExecutionConflict('invalid_request','report_mode must be compose or markdown')
        result['report_mode']=parameters['report_mode']
    return result


def apply_case_execution(document,target,parameters,*,inherit=False):
    from .case_execution_settings import SOLVE_TARGETS, selected_workflow
    from .openfoam_structural_feedback import selected as structural_feedback
    if target not in SOLVE_TARGETS:
        if parameters and 'placement' in parameters:
            raise ExecutionConflict('invalid_request','Remote placement requires a workflow with a solver step')
        return parameters
    try:
        parsed=parse_casedict(document)
        selected=selected_workflow(parsed)
        feedback=structural_feedback(parsed)
    except ValueError as error:
        raise ExecutionConflict('invalid_request',str(error)) from error

    if parameters is None and 'placement' not in selected:
        return parameters
    effective=dict(parameters or {})
    if inherit:
        effective.pop('placement',None)
    if selected.get('placement')=='remote':
        effective['placement']='remote'
    if 'solve_mode' in selected and (inherit or 'solve_mode' not in effective):
        effective={key:value for key,value in effective.items() if key not in {'solve_mode','subdomains','udf_name'}}
        effective.update(selected)
    if feedback and effective.get('solve_mode','serial')!='serial':
        raise ExecutionConflict('invalid_request','结构位移反馈需要串行 pimpleFoam 工作流')
    return normalized_parameters(effective)


def make_plan(target, start_state, parameters=None):
    if target not in TARGET_END or start_state not in "ABCDE" or len(start_state) != 1:
        raise ExecutionConflict("invalid_request", "Unsupported workflow target or engineering stage")
    names = [definition[0] for definition in DEFINITIONS]
    start = 1 if start_state == "A" else names.index("confirm_"+start_state)+1
    end = names.index(TARGET_END[target])+1
    definitions = [DEFINITIONS[0], *DEFINITIONS[start:end]]
    if parameters is not None:
        parameters = normalized_parameters(parameters)
        from .step_handlers import get_handler
        definitions = [(key, 'geometry.'+parameters['geometry_mode'] if key=='geometry' else handler, stage)
            for key, handler, stage in definitions]
        if parameters.get('solve_mode')=='mpi':
            definitions=[(key,'solve.openfoam.mpi' if key=='solve' else handler,stage)
                for key,handler,stage in definitions]
        elif parameters.get('solve_mode')=='udf':
            expanded=[]
            for key,handler,stage in definitions:
                if key=='solve':
                    expanded.append(('udf-build','udf.build.workflow',None))
                    handler='solve.openfoam.udf'
                expanded.append((key,handler,stage))
            definitions=expanded
        for _, handler, stage in definitions:
            if handler not in {'case.preflight','engineering.confirm'}:
                get_handler(handler, '1')
    steps = []
    for key, handler, stage in definitions:
        steps.append({"key":key, "handler":handler, "version":"1", "confirmation_state":stage,
            "dependencies":[steps[-1]["key"]] if steps else []})
    plan = {"schema":"vibeflow.workflow-plan.v1", "start_state":start_state, "target":target, "steps":steps}
    if parameters is not None:
        from .step_handlers import CONTRACT
        plan.update(schema='vibeflow.workflow-plan.v2', parameters=parameters, execution_contract=CONTRACT)
    return plan


def producer_kinds(target,parameters):
    return [s['key'] for s in make_plan(target,'A',parameters)['steps']
        if s['handler'] not in {'case.preflight','engineering.confirm'}]


def case_snapshot(case, *, original_dictionary=None):
    raw = (storage.get_bytes(case.s3_prefix, CASE_DICT_PATH) if original_dictionary is None
           else original_dictionary.encode('utf-8'))
    text = raw.decode('utf-8') if original_dictionary is None else original_dictionary
    return {"schema":"vibeflow.workflow-input.v1", "revision":case.dict_revision,
        "case_dict_sha256":hashlib.sha256(raw).hexdigest(),
        "engineering_sha256":case_input_fingerprint(text), "state":get_workflow_state(text)}


def require_inputs(run, case, *, original_dictionary=None):
    snapshot = case_snapshot(case, original_dictionary=original_dictionary)
    initial = json.loads(run.input_manifest_json)
    if snapshot["engineering_sha256"] != initial["engineering_sha256"]:
        raise ExecutionConflict("input_stale", "Engineering inputs changed; a new execution decision is required")
    return snapshot


async def create_run(db, *, case_id, user, target, run_dir, expected_revision,
                     idempotency_key=None, timeout_seconds=86400, max_log_bytes=16*1024*1024,
                     parameters=None):
    from ..routers.workspaces import get_owned_case
    from .quota import ensure_user_org
    case = await get_owned_case(case_id, user, db)
    job_execution.validate_key(idempotency_key)
    run_dir = safe_run_dir(run_dir)
    if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0 or
            not 0 < timeout_seconds <= 86400 or not 1024 <= max_log_bytes <= 64*1024*1024):
        raise ExecutionConflict("invalid_request", "Invalid workflow revision or resource budget")
    request = {"target":target, "run_dir":run_dir, "expected_revision":expected_revision,
        "timeout_seconds":timeout_seconds, "max_log_bytes":max_log_bytes}
    if parameters is not None:
        parameters = normalized_parameters(parameters)
        request['parameters'] = parameters
    digest = job_execution.request_hash(request)
    await ensure_parent_transaction(db)
    async with db.begin_nested():
        await lock_case_execution(db, case.id)
        await db.refresh(case)
        if idempotency_key is not None:
            existing = await db.scalar(select(WorkflowRun).where(WorkflowRun.case_id == case.id,
                WorkflowRun.created_by == user.id, WorkflowRun.idempotency_key == idempotency_key))
            if existing is not None:
                if existing.request_hash != digest:
                    raise ExecutionConflict("idempotency_conflict", "Workflow request key has a different payload")
                return existing, False
        if expected_revision != case.dict_revision:
            raise ExecutionConflict("revision_conflict", "Case revision changed before submission")
        document=storage.get_bytes(case.s3_prefix, CASE_DICT_PATH).decode('utf-8')
        snapshot = case_snapshot(case, original_dictionary=document)
        if snapshot["state"] != case.workflow_state:
            raise ExecutionConflict("input_stale", "Engineering files and case mirror disagree")
        plan = make_plan(target, snapshot["state"], apply_case_execution(document,target,parameters))
        from .job_execution_hosts import require_remote
        require_remote(plan)
        org = await ensure_user_org(db, user)
        run = WorkflowRun(id=str(uuid.uuid4()), case_id=case.id, org_id=org.id, created_by=user.id,
            target=target, request_hash=digest, idempotency_key=idempotency_key,
            expected_revision=expected_revision, run_dir=run_dir, plan_json=json_text(plan),
            input_manifest_json=json_text(snapshot),
            budget_json=json_text({"timeout_seconds":timeout_seconds, "max_log_bytes":max_log_bytes}))
        db.add(run)
        await db.flush()
        for definition in plan["steps"]:
            db.add(WorkflowStep(run_id=run.id, step_key=definition["key"], handler=definition["handler"],
                handler_version=definition["version"], dependencies_json=json_text(definition["dependencies"]),
                confirmation_state=definition["confirmation_state"]))
        await db.flush()
        await append_event(db, case.id, "workflow.created", {"status":"queued", "target":target,
            "version":0, "step_count":len(plan["steps"])}, run_id=run.id)
    return run, True


@asynccontextmanager
async def locked_run(db, run_id):
    from .workflow_queries import active_binding, object_record, plan_record
    run = await db.get(WorkflowRun, run_id)
    if run is None:
        raise ExecutionConflict("invalid_request", "Workflow does not exist")
    await ensure_parent_transaction(db)
    async with db.begin_nested():
        await lock_case_execution(db, run.case_id)
        await db.refresh(run)
        case = await db.get(Case, run.case_id, populate_existing=True)
        from .case_write import assert_case_available
        assert_case_available(case)
        try:
            plan = plan_record(run)
            from . import workflow_recompute
            from . import workflow_plan_migration
            from . import workflow_resume
            if plan.get('schema') == workflow_resume.SCHEMA:
                expected_plan=workflow_resume.make_plan(run.target,plan['parameters'],plan['resumption'])
            elif plan.get('schema') == workflow_recompute.SCHEMA:
                expected_plan=workflow_recompute.make_plan(run.target,plan['parameters'],plan['recomputation'])
            elif plan.get('schema') == workflow_plan_migration.SCHEMA:
                expected_plan=workflow_plan_migration.make_plan(run.target,plan['parameters'],plan['legacy_migration'])
            else:
                expected_plan=make_plan(run.target,plan["start_state"],plan.get('parameters'))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ExecutionConflict("invalid_request", "Workflow plan is damaged") from error
        if plan != expected_plan:
            raise ExecutionConflict("invalid_request", "Workflow plan differs from the registered handler graph")
        if plan.get('schema') == workflow_resume.SCHEMA:
            await workflow_resume.require_binding(db,run,plan['resumption'])
        if plan.get('schema') == workflow_plan_migration.SCHEMA:
            await workflow_plan_migration.require_binding(db,run,plan['legacy_migration'])
        if plan.get('schema') == workflow_recompute.SCHEMA:
            source = await db.get(WorkflowRun, plan['recomputation']['source_run_id'], populate_existing=True)
            if (source is None or source.id == run.id or source.case_id != run.case_id or
                    source.version < plan['recomputation']['source_run_version']):
                raise ExecutionConflict('evidence_invalid', 'Recomputation source workflow binding is invalid')
        rows = list(await db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == run.id)
            .execution_options(populate_existing=True)))
        steps = {step.step_key:step for step in rows}
        if set(steps) != {definition["key"] for definition in plan["steps"]}:
            raise ExecutionConflict("invalid_request", "Workflow step records are incomplete")
        ordered = []
        for definition in plan["steps"]:
            step = steps[definition["key"]]
            try: dependencies = json.loads(step.dependencies_json)
            except (ValueError, TypeError):
                raise ExecutionConflict('evidence_invalid', '工作流步骤依赖无法核验，请保留原记录') from None
            if (step.handler != definition["handler"] or step.handler_version != definition["version"] or
                    step.confirmation_state != definition["confirmation_state"] or
                    dependencies != definition["dependencies"]):
                raise ExecutionConflict("invalid_request", "Stored step differs from the registered plan")
            await active_binding(db, run, step)
            if definition.get('reuse'):
                output = object_record(step.output_manifest_json, '复用步骤输出')
                if step.active_attempt_id is not None or (step.status == 'succeeded' and
                        output.get('publication') != dict(definition['reuse'], status='committed')):
                    raise ExecutionConflict('evidence_invalid', 'Reused step differs from its selected publication')
            ordered.append(step)
        yield run, case, ordered


async def move_step(db, step, target, reason="", detail=""):
    return await transition(db, "step", step.id, target, expected_version=step.version,
                            reason_code=reason, detail=detail)


async def arm_confirmation(db, run, case, step):
    step.confirmation_revision = case.dict_revision
    snapshot = case_snapshot(case)
    try:
        publications = await confirmation_publications(db, run, case, step)
    except ExecutionConflict as error:
        await move_step(db, step, 'blocked', error.code, error.detail)
        return
    if publications:
        snapshot['publications'] = publications
    step.input_manifest_json = json_text(snapshot)
    await db.flush()
    await move_step(db, step, "awaiting_confirmation")
    await append_event(db, run.case_id, "workflow.confirmation_required", {
        "state":step.confirmation_state, "revision":step.confirmation_revision, "version":step.version},
        run_id=run.id, step_id=step.id)


async def confirmation_publications(db, run, case, step, *, freshness=None):
    from .step_handlers import CONTRACT
    if json.loads(run.plan_json).get('execution_contract') != CONTRACT:
        return []
    from .publication_inputs import resolve, PublicationInputStale
    result = []
    for key in json.loads(step.dependencies_json):
        producer = await db.scalar(select(WorkflowStep).where(WorkflowStep.run_id==run.id,
            WorkflowStep.step_key==key).execution_options(populate_existing=True))
        publication = json.loads(producer.output_manifest_json).get('publication') if producer else None
        if not publication or publication.get('status') != 'committed':
            raise ExecutionConflict('input_stale', 'This workflow has no current committed producer result')
        requirement = {'B':('geometry','A-geo/validation.json'), 'C':('mesh','B-mesh/quality.json'),
            'D':('solve',run.run_dir+'/solve-evidence.json'),
            'E':('postprocess','D-post/data/latest-postprocess.json')}.get(step.confirmation_state)
        if step.confirmation_state=='D' and producer.handler=='solve.openfoam.mpi':
            requirement=('solve',run.run_dir+'/mpi-evidence.json')
        elif step.confirmation_state=='D' and producer.handler=='solve.openfoam.udf':
            requirement=('solve',run.run_dir+'/udf-evidence.json')
        if requirement is None or publication.get('kind') != requirement[0]:
            raise ExecutionConflict('evidence_invalid', 'Confirmation has no registered publication requirement')
        reference = {'publication_id':publication['id'], 'paths':[requirement[1]], 'require_current':True}
        try:
            await resolve(db, case, [reference], materialize=False, _validated_current=freshness)
        except (PublicationInputStale, ValueError, OSError) as error:
            raise ExecutionConflict('input_stale', 'This workflow result is no longer the current valid publication') from error
        result.append(reference)
    return result


async def reconcile_locked(db, run, case, steps):
    if run.status in TERMINAL:
        return steps
    if run.status == "queued":
        await transition(db, "run", run.id, "running", expected_version=run.version)
    keyed = {step.step_key:step for step in steps}
    for step in steps:
        if step.status in {"running", "finalizing", "cancelling"} and step.active_attempt_id:
            attempt = await db.get(JobAttempt, step.active_attempt_id, populate_existing=True)
            if attempt is None or attempt.step_id != step.id:
                raise ExecutionConflict("invalid_request", "Active attempt does not belong to its step")
            if run.status == "cancelling" and attempt.phase not in TERMINAL:
                await job_execution.request_cancel(db, attempt.job_id)
                await db.refresh(attempt)
            if attempt.phase == "cancelling" and step.status in {"running", "finalizing"}:
                await move_step(db, step, "cancelling")
            if attempt.phase == "finalizing" and step.status == "running":
                await move_step(db, step, "finalizing")
            elif attempt.phase == "succeeded":
                if step.status == "running":
                    await move_step(db, step, "finalizing")
                step.output_manifest_json = attempt.output_manifest_json
                await move_step(db, step, "succeeded")
            elif attempt.phase in {"failed", "interrupted", "cancelled"}:
                if attempt.phase == "cancelled" and step.status != "cancelling":
                    await move_step(db, step, "cancelling")
                await move_step(db, step, attempt.phase, attempt.reason_code, attempt.detail)
        if run.status == "cancelling" and step.status in {"pending", "ready", "awaiting_confirmation"}:
            await move_step(db, step, "cancelled")
            continue
        dependencies = [keyed[key] for key in json.loads(step.dependencies_json)]
        if step.status in {"pending", "ready", "awaiting_confirmation"} and any(
                dependency.status in FAILED_STEPS for dependency in dependencies):
            await move_step(db, step, "blocked", "dependency_failed", "An upstream step did not succeed")
        elif step.status == "pending" and all(dependency.status == "succeeded" for dependency in dependencies):
            await move_step(db, step, "ready")
        if step.status == 'ready':
            from .workflow_recompute import definition, complete_reuse
            reference = definition(run, step.step_key).get('reuse')
            if reference:
                await complete_reuse(db, run, case, step, reference)
        if step.status == "ready" and step.handler == "engineering.confirm":
            await arm_confirmation(db, run, case, step)
    statuses = {step.status for step in steps}
    if run.status == "cancelling":
        if statuses <= TERMINAL | {"blocked"}:
            await transition(db, "run", run.id, "cancelled", expected_version=run.version)
    elif statuses == {"succeeded"}:
        if run.status == "awaiting_confirmation":
            await transition(db, "run", run.id, "running", expected_version=run.version)
        await transition(db, "run", run.id, "succeeded", expected_version=run.version)
    elif statuses <= TERMINAL | {"blocked"}:
        target = "interrupted" if "interrupted" in statuses else "failed"
        await transition(db, "run", run.id, target, expected_version=run.version,
            reason_code="recovery_required" if target == "interrupted" else "dependency_failed")
    else:
        target = "awaiting_confirmation" if "awaiting_confirmation" in statuses and not statuses & ACTIVE_STEPS else "running"
        if target != run.status:
            await transition(db, "run", run.id, target, expected_version=run.version)
    return steps


async def reconcile(db, run_id):
    async with locked_run(db, run_id) as (run, case, steps):
        return await reconcile_locked(db, run, case, steps)


async def request_cancel(db, run_id, *, expected_version):
    async with locked_run(db, run_id) as (run, case, steps):
        if run.version != expected_version:
            raise ExecutionConflict("state_conflict", "Workflow version changed")
        if run.status in TERMINAL:
            return steps
        if run.status != "cancelling":
            await transition(db, "run", run.id, "cancelling", expected_version=run.version)
        return await reconcile_locked(db, run, case, steps)


def ready_step(run, steps, step_id, expected_version):
    step = next((step for step in steps if step.id == step_id), None)
    if step is None or run.status != "running" or step.status != "ready" or step.version != expected_version:
        raise ExecutionConflict("state_conflict", "Step is not ready at the requested version")
    keyed = {row.step_key:row for row in steps}
    if any(keyed[key].status != "succeeded" for key in json.loads(step.dependencies_json)):
        raise ExecutionConflict("dependency_failed", "Step dependencies are unfinished")
    return step


async def complete_preflight(db, run_id, *, expected_version):
    async with locked_run(db, run_id) as (run, case, steps):
        step = ready_step(run, steps, steps[0].id, expected_version)
        require_inputs(run, case)
        await move_step(db, step, "running")
        state = json.loads(run.plan_json)["start_state"]
        for stage in "BCDE"[:max(0, "ABCDE".index(state))]:
            reason = await engineering_stage.evidence_reason(db, case, stage)
            if reason:
                await move_step(db, step, "failed", "evidence_invalid", reason)
                return await reconcile_locked(db, run, case, steps)
        step.output_manifest_json = json_text(case_snapshot(case))
        await move_step(db, step, "finalizing")
        await move_step(db, step, "succeeded")
        return await reconcile_locked(db, run, case, steps)


async def queue_step(db, run_id, step_id, *, expected_version, spec, input_paths=None, input_publications=None,
                     prepared=None,staged_workspace=None):
    async with locked_run(db, run_id) as (run, case, steps):
        step = ready_step(run, steps, step_id, expected_version)
        from .workflow_batches import require_open
        await require_open(db,run)
        from .workflow_recompute import definition
        if definition(run, step.step_key).get('reuse'):
            raise ExecutionConflict('evidence_invalid', 'Reused artifacts cannot create native Jobs')
        require_inputs(run, case)
        from .step_handlers import CONTRACT, PreparedStep
        if json.loads(run.plan_json).get('execution_contract') == CONTRACT:
            if (not isinstance(prepared, PreparedStep) or prepared.key != step.handler or
                    prepared.version != step.handler_version or prepared.spec != spec or
                    prepared.input_paths != input_paths or prepared.input_publications != input_publications):
                raise ExecutionConflict('evidence_invalid', 'Registered workflow steps require their prepared professional contract')
            from .step_handlers import get_handler
            get_handler(prepared.key, prepared.version).validate_spec(spec)
        if step.handler not in JOB_TYPES or spec.get("case_prefix") != case.s3_prefix:
            raise ExecutionConflict("invalid_request", "Native handler or execution directory is invalid")
        budget = json.loads(run.budget_json)
        if (not 0 < spec.get("timeout_seconds", 0) <= budget["timeout_seconds"] or
                not 1024 <= spec.get("max_log_bytes", 0) <= budget["max_log_bytes"]):
            raise ExecutionConflict("resource_limit", "Step exceeds the workflow resource budget")
        request = {"execution_spec":spec, "input_paths":input_paths} if input_paths is not None else spec
        if input_publications is not None:
            from .publication_inputs import normalize
            input_publications = normalize(input_publications)
            request = {'execution_spec':spec, 'input_paths':input_paths, 'input_publications':input_publications}
        job, created = await job_execution.submit(db, case_id=case.id, user_id=run.created_by,
            key=f"workflow.{run.id}.{step.step_key}", digest=job_execution.request_hash(request),
            job_type=JOB_TYPES[step.handler], command=spec["command"], spec=spec, step_id=step.id,
            input_paths=input_paths, input_publications=input_publications, isolate_default=prepared is None,
            generated_inputs=prepared.generated_inputs if prepared is not None else None,
            staged_workspace=staged_workspace)
        attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id))
        if attempt is None or attempt.step_id != step.id:
            raise ExecutionConflict("state_conflict", "Queued attempt belongs to another step")
        if prepared is not None:
            from .step_handlers import bind
            await bind(db, case, job, attempt, prepared)
        step.active_attempt_id = attempt.id
        step.input_manifest_json = json_text(require_inputs(run, case))
        if prepared is not None:
            step.input_manifest_json = attempt.input_manifest_json
        await move_step(db, step, "running")
        return job, created


def _check_confirmation_status(run, step, case_id, target, confirmed, expected_version):
    if (run.case_id != case_id or step is None or step.run_id != run.id or
            step.status != 'awaiting_confirmation' or step.version != expected_version or
            run.status not in {'running','awaiting_confirmation'}):
        raise ExecutionConflict('state_conflict', 'Confirmation no longer belongs to an active waiting step')
    if not confirmed or target.upper() != step.confirmation_state:
        raise ExecutionConflict('confirmation_required', 'Explicit confirmation of the waiting stage is required')


async def _verify_confirmation(db, run, case, step, expected_revision, *, original_dictionary=None):
    snapshot = require_inputs(run, case, original_dictionary=original_dictionary)
    armed = json.loads(step.input_manifest_json)
    if (expected_revision != case.dict_revision or expected_revision != step.confirmation_revision or
            snapshot['case_dict_sha256'] != armed['case_dict_sha256']):
        raise ExecutionConflict('revision_conflict', 'Confirmation is stale; refresh it before confirming again')
    freshness = {} if step.confirmation_state in {'D','E'} else None
    publications = await confirmation_publications(db, run, case, step, freshness=freshness)
    if publications and publications != armed.get('publications'):
        raise ExecutionConflict('input_stale', 'Confirmation belongs to a different publication')
    reason = await engineering_stage.evidence_reason(db, case, step.confirmation_state,
        original_dictionary=original_dictionary, freshness=freshness)
    if reason:
        raise ExecutionConflict('evidence_invalid', reason)


async def _commit_confirmation(db, run, case, steps, step, user_id, expected_revision, expected_version):
    at = now()
    output = {'confirmation':{'user_id':user_id, 'state':step.confirmation_state,
        'from_revision':expected_revision, 'to_revision':case.dict_revision}}
    changed = await db.execute(update(WorkflowStep).where(WorkflowStep.id == step.id,
        WorkflowStep.version == expected_version, WorkflowStep.status == 'awaiting_confirmation')
        .values(status='succeeded', version=expected_version+1, output_manifest_json=json_text(output),
                ended_at=at, updated_at=at))
    if changed.rowcount != 1:
        raise ExecutionConflict('state_conflict', 'Concurrent confirmation won')
    await append_event(db, case.id, 'step.state_changed', {'previous':'awaiting_confirmation',
        'status':'succeeded', 'version':expected_version+1, **output}, run_id=run.id, step_id=step.id)
    await db.refresh(step)
    await reconcile_locked(db, run, case, steps)


async def confirm_staged_step(db, case, checked, *, run, step, user_id, target, confirmed,
                              expected_revision, expected_version):
    case_id, run_id, step_id = case.id, run.id, step.id
    _check_confirmation_status(run, step, case_id, target, confirmed, expected_version)

    @asynccontextmanager
    async def confirmation(session, current, draft):
        async with locked_run(session, run_id) as (locked, _, steps):
            selected = next((row for row in steps if row.id == step_id), None)
            _check_confirmation_status(locked, selected, current.id, target, confirmed, expected_version)
            await _verify_confirmation(session, locked, current, selected, expected_revision,
                                        original_dictionary=draft.original_text)
            yield
            await _commit_confirmation(session, locked, current, steps, selected, user_id,
                                       expected_revision, expected_version)

    return await engineering_stage.advance_staged(db, case, checked, target=target,
        confirmed=confirmed, expected_revision=expected_revision, confirmation=confirmation, actor_id=user_id)


async def confirm_step(db, *, run_id, step_id, case_id, user_id, target, confirmed,
                       expected_revision, expected_version):
    async with locked_run(db, run_id) as (run, case, steps):
        step = next((row for row in steps if row.id == step_id), None)
        _check_confirmation_status(run, step, case_id, target, confirmed, expected_version)
        await _verify_confirmation(db, run, case, step, expected_revision)
        result = await engineering_stage.advance(db, case, target=target, confirmed=confirmed,
                                                expected_revision=expected_revision, actor_id=user_id)
        await _commit_confirmation(db, run, case, steps, step, user_id, expected_revision, expected_version)
        return result


async def refresh_confirmation(db, run_id, step_id, *, expected_version):
    async with locked_run(db, run_id) as (run, case, steps):
        step = next((row for row in steps if row.id == step_id), None)
        if step is None or step.status != "awaiting_confirmation" or step.version != expected_version or run.status in TERMINAL:
            raise ExecutionConflict("state_conflict", "Confirmation is no longer waiting")
        snapshot = require_inputs(run, case)
        publications = await confirmation_publications(db, run, case, step)
        if publications:
            snapshot['publications'] = publications
        step.confirmation_revision = case.dict_revision
        step.input_manifest_json = json_text(snapshot)
        step.version += 1
        await db.flush()
        await append_event(db, case.id, "workflow.confirmation_required", {"state":step.confirmation_state,
            "revision":case.dict_revision, "version":step.version}, run_id=run.id, step_id=step.id)
        return step
