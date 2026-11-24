"""Authorized registered-workflow admission and explicit dispatch/cancellation."""
import json

from fastapi import HTTPException
from sqlalchemy import select

from ..models import Job, JobAttempt, User, WorkflowRun, WorkflowStep
from . import job_execution, jobrunner, step_handlers, workflow_scheduler as scheduler
from .workflow_state import ExecutionConflict, TERMINAL


async def owned(db, run_id, user, *, write=True):
    from ..routers.workspaces import get_owned_case
    run = await db.get(WorkflowRun, run_id, populate_existing=True)
    if run is None:
        raise HTTPException(404, '工作流不存在')
    await get_owned_case(run.case_id, user, db, write=write)
    return run


async def start(db, *, case_id, user, target, run_dir, expected_revision, parameters, budget, key=None,request=None):
    run, _ = await scheduler.create_run(db, case_id=case_id, user=user, target=target,
        run_dir=run_dir, expected_revision=expected_revision, parameters=parameters,
        idempotency_key=key, **budget)
    await dispatch(db, run.id, user,request=request)
    return await detail(db, run.id, user)


async def dispatch(db, run_id, user, *, expected_version=None,request=None):
    from . import document_admission as admission
    from . import workflow_step_inputs
    caller_id=user.id
    await owned(db, run_id, user)
    launch_ids = [];preparations=[];staged_steps=[]
    async with scheduler.locked_run(db, run_id) as (run, case, steps):
        if run.status not in TERMINAL|{'cancelling'}:
            from .workflow_batches import require_open
            await require_open(db,run)
        if json.loads(run.plan_json).get('execution_contract') != step_handlers.CONTRACT:
            raise ExecutionConflict('recovery_required', 'Legacy workflow requires explicit migration to registered handlers')
        if expected_version is not None and run.version != expected_version:
            raise ExecutionConflict('state_conflict', 'Workflow version changed')
        steps = await scheduler.reconcile_locked(db, run, case, steps)
        if run.status not in TERMINAL and steps[0].status=='ready':
            steps = await scheduler.complete_preflight(db, run.id, expected_version=steps[0].version)
        for step in steps:
            if step.status=='ready' and step.handler not in {'engineering.confirm','case.preflight'}:
                handler = step_handlers.get_handler(step.handler, step.handler_version)
                parameters = {}
                publication_ids = {}
                plan_parameters=json.loads(run.plan_json).get('parameters',{})
                if step.handler=='udf.build.workflow':
                    parameters={'run_dir':run.run_dir,'name':plan_parameters['udf_name']}
                if step.handler in {'mesh.generate','solve.openfoam','solve.openfoam.mpi','solve.openfoam.udf','postprocess.run','report.build'}:
                    parameters['run_dir']=run.run_dir
                    upstream={'mesh.generate':'geometry','solve.openfoam':'mesh','solve.openfoam.mpi':'mesh','solve.openfoam.udf':'mesh','postprocess.run':'solve','report.build':'postprocess'}[step.handler]
                    if step.handler=='solve.openfoam.mpi':
                        parameters['subdomains']=json.loads(run.plan_json)['parameters']['subdomains']
                    if step.handler=='report.build':
                        parameters['mode']=json.loads(run.plan_json).get('parameters',{}).get('report_mode','compose')
                    producer=next((row for row in steps if row.step_key==upstream),None)
                    if producer is not None:
                        publication=json.loads(producer.output_manifest_json).get('publication')
                        if not publication or publication.get('status')!='committed':
                            raise ExecutionConflict('input_stale','This workflow has no committed '+upstream+' producer')
                        parameters[upstream+'_publication_id']=publication['id']
                        publication_ids[upstream]=publication['id']
                    if step.handler=='solve.openfoam.udf':
                        parameters['name']=plan_parameters['udf_name']
                        build=next((row for row in steps if row.step_key=='udf-build'),None)
                        publication=json.loads(build.output_manifest_json).get('publication') if build else None
                        if not publication or publication.get('status')!='committed':
                            raise ExecutionConflict('input_stale','This workflow has no committed UDF library producer')
                        parameters['udf_build_publication_id']=publication['id']
                        publication_ids['udf-build']=publication['id']
                if step.handler in workflow_step_inputs.STAGING:
                    staged_steps.append(workflow_step_inputs.ReadyStep.from_step(run,case,step,publication_ids,parameters))
                    continue
                prepared = await handler.prepare(db, case, await db.get(User,run.created_by),
                    parameters=parameters,
                    budget=json.loads(run.budget_json))
                if prepared.admission is not None:
                    preparations.append((prepared,case.id,run.created_by,run.version,step.id,step.version))
                    continue
                job, _ = await scheduler.queue_step(db, run.id, step.id, expected_version=step.version,
                    spec=prepared.spec, input_paths=prepared.input_paths,
                    input_publications=prepared.input_publications, prepared=prepared)
                launch_ids.append(job.id)
            elif run.status=='running' and step.status=='running' and step.active_attempt_id:
                attempt = await db.get(JobAttempt, step.active_attempt_id, populate_existing=True)
                if attempt and attempt.phase=='queued':
                    launch_ids.append(attempt.job_id)
    # The complete plan/attempt/input/publication reservation is durable before
    # any native start. Dispatch repeats are fenced by the existing atomic claim.
    await db.commit()
    for step in staged_steps:
        job_id=await workflow_step_inputs.submit(db,step,caller_id,request=request)
        if job_id is not None:
            launch_ids.append(job_id)
    for prepared,case_id,creator_id,run_version,step_id,step_version in preparations:
        async def invalidated():
            from ..db import SessionLocal
            async with SessionLocal() as session:
                current_run=await session.get(WorkflowRun,run_id)
                current_step=await session.get(WorkflowStep,step_id)
                return (current_run is None or current_step is None or current_run.version!=run_version
                    or current_run.status in TERMINAL|{'cancelling'} or current_step.version!=step_version
                    or current_step.status!='ready')
        prepared=await admission.complete(prepared,case_id,creator_id,request=request,invalidated=invalidated)
        _,user=await admission.authorize(db,case_id,caller_id)
        await owned(db,run_id,user)
        async with scheduler.locked_run(db,run_id) as (run,case,steps):
            await admission.check_access(db,case,user)
            step=next((item for item in steps if item.id==step_id),None)
            if run.version!=run_version or run.status in TERMINAL|{'cancelling'} or step is None:
                raise ExecutionConflict('state_conflict','Workflow changed during document preparation')
            if step.status=='running' and step.active_attempt_id:
                attempt=await db.get(JobAttempt,step.active_attempt_id,populate_existing=True)
                if attempt and attempt.phase=='queued':launch_ids.append(attempt.job_id)
            else:
                if step.version!=step_version or step.status!='ready':
                    raise ExecutionConflict('state_conflict','Workflow step changed during document preparation')
                await admission.validate(db,case,await db.get(User,creator_id),prepared)
                job,_=await scheduler.queue_step(db,run.id,step.id,expected_version=step_version,
                    spec=prepared.spec,input_paths=prepared.input_paths,
                    input_publications=prepared.input_publications,prepared=prepared)
                launch_ids.append(job.id)
        await db.commit()
    for job_id in dict.fromkeys(launch_ids):
        try:
            await step_handlers.launch(job_id)
        except job_execution.JobAlreadyClaimed:
            pass
    return launch_ids


async def cancel(db, run_id, user, *, expected_version):
    await owned(db, run_id, user)
    async with scheduler.locked_run(db, run_id) as (run, case, steps):
        if run.status not in TERMINAL and run.status != 'cancelling':
            steps = await scheduler.request_cancel(db, run_id, expected_version=expected_version)
            from . import audit
            await audit.case_event(db, case, user.id, 'workflow.cancel.requested', 'workflow_run', run_id)
        identifiers = []
        for step in steps:
            if step.active_attempt_id:
                attempt = await db.get(JobAttempt, step.active_attempt_id)
                if attempt and attempt.phase not in TERMINAL:
                    identifiers.append(attempt.job_id)
    await db.commit()
    for job_id in identifiers:
        proc = jobrunner.get_proc(job_id)
        if proc is not None:
            proc.cancel()
    return await detail(db, run_id, user)


def run_summary(run):
    return {'id':run.id,'case_id':run.case_id,'target':run.target,'status':run.status,
        'version':run.version,'run_dir':run.run_dir,'expected_revision':run.expected_revision,
        'error_code':run.error_code,'detail':run.detail,'created_at':run.created_at.isoformat(),
        'ended_at':run.ended_at.isoformat() if run.ended_at else None}


async def detail(db, run_id, user):
    from .workflow_queries import active_binding, object_record, plan_record
    run = await owned(db, run_id, user, write=False)
    result = run_summary(run)
    plan = plan_record(run)
    inputs = object_record(run.input_manifest_json, '工作流输入')
    result['parameters'] = plan.get('parameters', {})
    result['execution_contract'] = plan.get('execution_contract')
    result['recomputation'] = plan.get('recomputation')
    result['recomputation_reset'] = inputs.get('recomputation_reset')
    result['legacy_migration'] = plan.get('legacy_migration')
    result['resumption'] = plan.get('resumption')
    if plan.get('schema') == 'vibeflow.workflow-plan.v5':
        from .workflow_resume import make_plan, require_binding
        try:
            if plan != make_plan(run.target, plan['parameters'], plan['resumption']):
                raise ValueError('Resume graph changed')
            await require_binding(db, run, plan['resumption'])
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ExecutionConflict('evidence_invalid', '恢复流程计划无法核验，请保留原记录') from error
    result['legacy_migration_reset'] = inputs.get('legacy_migration_reset')
    rows = list(await db.scalars(select(WorkflowStep).where(WorkflowStep.run_id==run.id)
        .execution_options(populate_existing=True)))
    order = {definition['key']:index for index,definition in enumerate(plan['steps'])}
    if {row.step_key for row in rows} != set(order):
        raise ExecutionConflict('evidence_invalid', '工作流步骤记录与原计划不一致，请保留历史核查')
    result['steps'] = []
    for step in sorted(rows,key=lambda row:order.get(row.step_key,999)):
        await active_binding(db, run, step)
        item = {'id':step.id,'key':step.step_key,'handler':step.handler,'handler_version':step.handler_version,
            'status':step.status,'version':step.version,'error_code':step.error_code,'detail':step.detail,
            'active_attempt_id':step.active_attempt_id,
            'confirmation':object_record(step.output_manifest_json, '工作流步骤输出').get('confirmation'),
            'confirmation_state':step.confirmation_state,'confirmation_revision':step.confirmation_revision,
            'publication':object_record(step.output_manifest_json, '工作流步骤输出').get('publication'), 'attempts':[]}
        definition = next((d for d in plan['steps'] if d['key'] == step.step_key), {})
        item['execution_mode'] = 'reuse' if definition.get('reuse') else 'execute'
        item['reused_publication'] = definition.get('reuse')
        attempts = await db.scalars(select(JobAttempt).where(JobAttempt.step_id==step.id)
            .order_by(JobAttempt.number).execution_options(populate_existing=True))
        for attempt in attempts:
            document = object_record(attempt.input_manifest_json, '工作流尝试输入')
            job = await db.get(Job, attempt.job_id, populate_existing=True)
            if job is None or job.case_id != run.case_id:
                raise ExecutionConflict('evidence_invalid','工作流尝试与作业的算例归属不一致，请保留历史核查')
            proc = jobrunner.get_proc(attempt.job_id)
            item['attempts'].append({'id':attempt.id,'number':attempt.number,'job_id':attempt.job_id,
                'phase':attempt.phase,'reason_code':attempt.reason_code,'exit_code':attempt.exit_code,
                'job_status':job.status if job else None,'handler':document.get('handler'),
                'publication':object_record(attempt.output_manifest_json, '工作流尝试输出').get('publication'),
                'completion_error':proc.completion_error if proc else None})
        result['steps'].append(item)
    return result
