"""Stage publication-backed workflow steps before the scheduler's write transaction."""
from dataclasses import dataclass
from functools import partial
import json

from ..db import SessionLocal
from ..models import JobAttempt, User, WorkflowRun, WorkflowStep
from . import document_admission, postprocess_submission, publication_command_submission, step_handlers, workflow_scheduler as scheduler
from .async_ops import finish_before_cancel
from .case_read_capacity import admitted, file_read_deadline
from .document_admission import authorize, check_access
from .quota import check_job_quota
from .request_deadline import STAGING_CLEANUP_SECONDS
from .request_lifecycle import wait
from .workflow_state import ExecutionConflict, InvalidStepInput, TERMINAL


STAGING = {
    'geometry.preview': ('geometry_seal','封存几何输入'),
    'geometry.export': ('geometry_seal','封存几何输入'),
    'mesh.generate': ('mesh_seal','封存网格输入'),
    'solve.openfoam': ('solve_seal','封存求解输入'),
    'solve.openfoam.mpi': ('mpi_solve_seal','封存 MPI 求解输入'),
    'udf.build.workflow': ('udf_build_seal','封存 UDF 编译输入'),
    'solve.openfoam.udf': ('udf_solve_seal','封存 UDF 求解输入'),
    'postprocess.run': (postprocess_submission.CODE,postprocess_submission.LABEL),
    'report.build': ('report_seal','封存报告输入'),
}


@dataclass(frozen=True)
class ReadyStep:
    case_id: int
    prefix: str
    revision: int
    run_id: str
    run_version: int
    run_dir: str
    step_id: str
    step_version: int
    handler: str
    handler_version: str
    creator_id: int
    publications: dict[str, str]
    parameters: dict
    budget: dict

    @classmethod
    def from_step(cls, run, case, step, publications, parameters):
        return cls(case.id,case.s3_prefix,case.dict_revision,run.id,run.version,run.run_dir,step.id,step.version,
                   step.handler,step.handler_version,run.created_by,publications,parameters,json.loads(run.budget_json))


def _changed():
    return ExecutionConflict('state_conflict','步骤输入准备期间工作流已变化')


class _AlreadyBound(Exception):
    def __init__(self, job_id):
        self.job_id=job_id


async def _queued_job(db,step):
    attempt=await db.get(JobAttempt,step.active_attempt_id,populate_existing=True)
    return attempt.job_id if attempt is not None and attempt.phase=='queued' else None


def _bound(step):
    return step.active_attempt_id is not None and step.status in {'running','finalizing','succeeded'}


async def _case(ready, caller_id):
    async with SessionLocal() as session:
        case,_=await authorize(session,ready.case_id,caller_id)
        if (case.s3_prefix,case.dict_revision)!=(ready.prefix,ready.revision):
            raise _changed()
        session.expunge(case)
        return case


def _require_ready(ready, run, case, step, steps):
    if ((run.version,case.s3_prefix,case.dict_revision,run.created_by,run.run_dir,json.loads(run.budget_json))
            !=(ready.run_version,ready.prefix,ready.revision,ready.creator_id,ready.run_dir,ready.budget)
            or step.status!='ready' or step.version!=ready.step_version):
        raise _changed()
    for key,identifier in ready.publications.items():
        producer=next((item for item in steps if item.step_key==key),None)
        publication=json.loads(producer.output_manifest_json).get('publication') if producer is not None else None
        if not publication or publication.get('status')!='committed' or publication.get('id')!=identifier:
            raise _changed()


async def _publish(db, ready, caller_id, prepare, staged):
    selected,actor=await authorize(db,ready.case_id,caller_id)
    async with admitted(db,selected):
        try:
            async with scheduler.locked_run(db,ready.run_id) as (run,case,steps):
                await check_access(db,case,actor)
                step=next((item for item in steps if item.id==ready.step_id),None)
                if (run.case_id!=ready.case_id or run.status in TERMINAL|{'cancelling'} or step is None
                        or (step.handler,step.handler_version)!=(ready.handler,ready.handler_version)):
                    raise _changed()
                if _bound(step):
                    job_id=await _queued_job(db,step)
                else:
                    _require_ready(ready,run,case,step,steps)
                    creator=await db.get(User,ready.creator_id)
                    if creator is None:
                        raise _changed()
                    workspace=None
                    if staged is None:
                        prepared=await prepare(db,case,creator)
                    else:
                        await check_job_quota(db,creator,case_id=case.id)
                        prepared,workspace=staged
                        if workspace is None and prepared.admission is not None:
                            await document_admission.validate(db,case,creator,prepared)
                    job,_=await scheduler.queue_step(db,run.id,step.id,expected_version=step.version,
                        spec=prepared.spec,input_paths=prepared.input_paths,input_publications=prepared.input_publications,
                        prepared=prepared,staged_workspace=workspace)
                    job_id=job.id
            await finish_before_cancel(db.commit())
            return job_id
        except BaseException:
            await finish_before_cancel(db.rollback())
            raise


async def _record_invalid_input(db, ready, caller_id, error):
    _,actor = await authorize(db,ready.case_id,caller_id)
    try:
        async with scheduler.locked_run(db,ready.run_id) as (run,case,steps):
            await check_access(db,case,actor)
            step = next((item for item in steps if item.id==ready.step_id),None)
            if step is not None and run.status not in TERMINAL|{'cancelling'}:
                _require_ready(ready,run,case,step,steps)
                await scheduler.move_step(db,step,'blocked',error.code,error.detail[:4000])
                await scheduler.reconcile_locked(db,run,case,steps)
        await finish_before_cancel(db.commit())
    except BaseException:
        await finish_before_cancel(db.rollback())
        raise


async def submit(db, ready, caller_id, *, request=None):
    handler=step_handlers.get_handler(ready.handler,ready.handler_version)
    prepare=partial(handler.prepare,parameters=ready.parameters,budget=ready.budget)
    committing=False

    async def invalidated():
        if committing:
            return False
        async with SessionLocal() as session:
            run=await session.get(WorkflowRun,ready.run_id)
            return run is None or run.status in TERMINAL|{'cancelling'}

    async def validate_remote_lease(session,case):
        # A second seal must never acquire the case lease after the first one
        # has queued this step. Its lease could block the job at native start.
        run=await session.get(WorkflowRun,ready.run_id,populate_existing=True)
        step=await session.get(WorkflowStep,ready.step_id,populate_existing=True)
        if (run is None or run.case_id!=case.id or run.status in TERMINAL|{'cancelling'}
                or step is None or step.run_id!=run.id):
            raise _changed()
        if _bound(step):
            raise _AlreadyBound(await _queued_job(session,step))
        if ((case.s3_prefix,case.dict_revision,run.version,step.version,step.status)
                !=(ready.prefix,ready.revision,ready.run_version,ready.step_version,'ready')):
            raise _changed()

    async def persist():
        nonlocal committing
        code,label=STAGING[ready.handler]
        async with file_read_deadline(code,label,seconds=STAGING_CLEANUP_SECONDS):
            case=await _case(ready,caller_id)
            try:
                async with publication_command_submission.stage_step(db,case,caller_id,prepare,registered=True,
                        job_user_id=ready.creator_id,wait_for_capacity=True,validate_locked=validate_remote_lease,
                        code=code,label=label) as staged:
                    if staged is None and ready.handler=='report.build':
                        # Older storage may still use the existing document path.
                        async with SessionLocal() as session:
                            creator=await session.get(User,ready.creator_id)
                            if creator is None:
                                raise _changed()
                            prepared=await prepare(session,case,creator)
                        staged=(await document_admission.complete(prepared,case.id,ready.creator_id),None)
                    committing=True
                    return await _publish(db,ready,caller_id,prepare,staged)
            except _AlreadyBound as existing:
                return existing.job_id

    try:
        return await wait(persist(),request=request,invalidated=invalidated)
    except InvalidStepInput as error:
        await _record_invalid_input(db,ready,caller_id,error)
