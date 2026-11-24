"""Registered professional workflows; existing caseDict API owns confirmation."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import User, WorkflowBatch, WorkflowRun
from ..services import workflow_execution as execution, workflow_scheduler as scheduler
from ..services.case_write import CaseWriteConflict
from ..services.workflow_state import ExecutionConflict
from .workspaces import get_owned_case

router = APIRouter(prefix='/api/v1', tags=['workflows'])


@router.get('/cases/{case_id}/dependencies')
async def dependencies(case_id: int, run_dir: str | None = None,
                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from ..models import Case
    from ..services import dependency_status
    from ..services.case_evidence import safe_run_dir
    from ..services.durable_events import lock_case_execution
    if run_dir is not None:
        try:
            run_dir = safe_run_dir(run_dir)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
    await get_owned_case(case_id, user, db, write=False)
    await lock_case_execution(db, case_id)
    case = await db.get(Case, case_id, populate_existing=True)
    before = await dependency_status.checkpoint(db, case)
    # Retain fully loaded identities, release the control lock and transaction
    # before hashing artifacts or running native source validators.
    db.expunge_all()
    await db.rollback()
    result, reads = await dependency_status.inspect(case, before, run_dir=run_dir)
    await lock_case_execution(db, case_id)
    current = await get_owned_case(case_id, user, db, write=False)
    after = await dependency_status.checkpoint(db, current)
    if before != after:
        raise CaseWriteConflict('dependency_changed', 'Inputs or publications changed during inspection; refresh the graph')
    await reads.require_current(db)
    await db.rollback()
    return result


class Budget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    timeout_seconds: float = Field(default=86400, gt=0, le=86400)
    max_log_bytes: int = Field(default=16*1024*1024, ge=1024, le=64*1024*1024)


class Parameters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    geometry_mode: Literal['preview','export'] = 'preview'
    report_mode: Literal['compose','markdown'] | None = None
    solve_mode: Literal['serial','mpi','udf'] | None = None
    placement: Literal['remote'] | None = None
    subdomains: int | None = Field(default=None,ge=1,le=64,strict=True)
    udf_name: str | None = Field(default=None,pattern='^[A-Za-z][A-Za-z0-9_]{0,39}$',strict=True)


class RunIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: Literal['geometry','mesh','solve','postprocess','report'] = 'geometry'
    expected_revision: int = Field(ge=0, strict=True)
    run_dir: str = 'C-run/case-1'
    parameters: Parameters = Field(default_factory=Parameters)
    budget: Budget = Field(default_factory=Budget)


class VersionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0, strict=True)


class BatchMemberIn(RunIn):
    key: str = Field(pattern='^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$',strict=True)
    case_id: int = Field(gt=0,strict=True)


class WorkflowStudyIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['parametric','optimization','UQ']
    parameter_path: str = Field(min_length=1,max_length=160)
    parameter_unit: str = Field(min_length=1,max_length=80)
    metric: str = Field(min_length=1,max_length=160)
    metric_unit: str = Field(min_length=1,max_length=80)
    goal: Literal['minimize','maximize'] | None = None
    weights: dict[str,Annotated[float,Field(strict=True,ge=0,allow_inf_nan=False)]] | None = Field(default=None,max_length=50)


class WorkflowBatchIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    members: list[BatchMemberIn] = Field(min_length=1,max_length=50)
    max_concurrent: int = Field(default=1,ge=1,le=4,strict=True)
    study: WorkflowStudyIn | None = None


class RecomputeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0, strict=True)
    expected_revision: int = Field(ge=0, strict=True)
    confirmed_reset: bool = Field(strict=True)
    target: Literal['geometry','mesh','solve','postprocess','report'] | None = None
    run_dir: str | None = None
    parameters: Parameters | None = None
    budget: Budget | None = None


class MigrateIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0,strict=True)
    expected_revision: int = Field(ge=0,strict=True)
    expected_event_sequence: int = Field(ge=0,strict=True)
    expected_source_plan_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    confirmed_reset: bool = Field(strict=True)
    target: Literal['geometry','mesh','solve','postprocess','report']
    run_dir: str
    parameters: Parameters
    budget: Budget


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0, strict=True)
    expected_revision: int = Field(ge=0, strict=True)
    source_step_id: str = Field(pattern='^[0-9a-f-]{36}$')
    expected_step_version: int = Field(ge=0, strict=True)
    source_attempt_id: str = Field(pattern='^[0-9a-f-]{36}$')
    expected_attempt_version: int = Field(ge=0, strict=True)
    confirmed: bool = Field(strict=True)
    mode: Literal['retry_step','checkpoint']
    checkpoint_time: str | None = Field(default=None, min_length=1, max_length=80)


async def perform(operation):
    try:
        return await operation
    except ExecutionConflict as error:
        raise HTTPException(409, {'code':error.code,'message':error.detail}) from error
    except ValueError as error:
        if isinstance(error, CaseWriteConflict):
            raise
        raise HTTPException(400,str(error)) from error


@router.post('/cases/{case_id}/workflow-runs')
async def create(case_id: int, body: RunIn, request: Request, user: User=Depends(get_current_user),
                 db: AsyncSession=Depends(get_db), idempotency_key: str | None=Header(default=None,alias='Idempotency-Key')):
    return await perform(execution.start(db,case_id=case_id,user=user,target=body.target,
        expected_revision=body.expected_revision,run_dir=body.run_dir,parameters=body.parameters.model_dump(exclude_none=True),
        budget=body.budget.model_dump(),key=idempotency_key,request=request))


@router.get('/cases/{case_id}/workflow-runs')
async def list_runs(case_id: int, user: User=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    await get_owned_case(case_id,user,db,write=False)
    rows = await db.scalars(select(WorkflowRun).where(WorkflowRun.case_id==case_id)
        .order_by(WorkflowRun.created_at.desc(),WorkflowRun.id.desc()).limit(50))
    return [execution.run_summary(row) for row in rows]


@router.get('/cases/{case_id}/workflow-runs-page')
async def runs_page(case_id:int,limit:int=Query(default=50,ge=1,le=100),cursor:str|None=Query(default=None,max_length=2048),
        status:Literal['queued','running','awaiting_confirmation','cancelling','succeeded','failed','cancelled','interrupted']|None=None,
        target:Literal['geometry','mesh','solve','postprocess','report']|None=None,
        user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    from ..services.workflow_queries import runs_page
    return await perform(runs_page(db,case_id,user,limit=limit,cursor=cursor,status=status,target=target))


@router.post('/workflow-batches',status_code=202)
async def create_workflow_batch(body:WorkflowBatchIn,user:User=Depends(get_current_user),
                                db:AsyncSession=Depends(get_db),idempotency_key:str=Header(alias='Idempotency-Key')):
    from ..services.workflow_batches import start
    return await perform(start(db,user=user,members=[m.model_dump(exclude_none=True) for m in body.members],
        max_concurrent=body.max_concurrent,key=idempotency_key,
        study=body.study.model_dump(exclude_none=True) if body.study is not None else None))


@router.get('/workflow-batches')
async def list_workflow_batches(user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    from ..services.workflow_batches import detail
    identifiers=list(await db.scalars(select(WorkflowBatch.id).where(WorkflowBatch.created_by==user.id)
        .order_by(WorkflowBatch.created_at.desc(),WorkflowBatch.id.desc()).limit(50)))
    result=[]
    for identifier in identifiers:
        try:result.append(await perform(detail(db,identifier,user)))
        except HTTPException as error:
            if error.status_code!=404:raise
    return result


@router.get('/workflow-batches/{batch_id}')
async def get_workflow_batch(batch_id:str,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    from ..services.workflow_batches import detail
    return await perform(detail(db,batch_id,user))


@router.get('/workflow-batches/{batch_id}/study-results')
async def workflow_study_results(batch_id:str,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    from ..services.workflow_studies import results
    return await perform(results(db,batch_id,user))


@router.post('/workflow-batches/{batch_id}:cancel')
async def cancel_workflow_batch(batch_id:str,body:VersionIn,user:User=Depends(get_current_user),
                                db:AsyncSession=Depends(get_db)):
    from ..services.workflow_batches import cancel
    return await perform(cancel(db,batch_id,user,expected_version=body.expected_version))


@router.get('/workflow-runs/{run_id}')
async def get_run(run_id: str, user: User=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    return await execution.detail(db,run_id,user)


@router.get('/workflow-runs/{run_id}/events')
async def run_events(run_id:str,after:int=Query(default=0,ge=0,le=2**53-1),limit:int=Query(default=100,ge=1,le=200),
        user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    from ..services.workflow_queries import events
    return await perform(events(db,run_id,user,after=after,limit=limit))


@router.get('/workflow-runs/{run_id}/recovery')
async def recovery_context(run_id: str, before_seq: int | None = Query(default=None, ge=1),
        limit: int = Query(default=20, ge=1, le=100), user: User=Depends(get_current_user),
        db: AsyncSession=Depends(get_db)):
    from ..services import workflow_recovery
    return await perform(workflow_recovery.context(db,run_id,user,before_seq=before_seq,limit=limit))


class RecoveryLookupIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1,max_length=128)
    action: Literal['recompute','migrate','resume']
    payload: dict


@router.post('/workflow-runs/{run_id}/recovery-request')
async def recovery_request(run_id: str, body: RecoveryLookupIn, user: User=Depends(get_current_user),
        db: AsyncSession=Depends(get_db)):
    from ..services import workflow_recovery
    try:
        values = {'recompute':RecomputeIn,'migrate':MigrateIn,'resume':ResumeIn}[body.action].model_validate(body.payload)
    except ValidationError:
        raise HTTPException(422,'原确认参数无法核验，请保留本机记录') from None
    return await perform(workflow_recovery.lookup(db,run_id,user,key=body.key,action=body.action,
        values=values.model_dump(exclude_none=True)))


@router.get('/workflow-runs/{run_id}/resume-plan')
async def resume_plan(run_id: str, user: User=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    from ..services import workflow_resume
    return await perform(workflow_resume.preview(db, run_id, user))


@router.post('/workflow-runs/{run_id}:resume', status_code=202)
async def resume(run_id: str, body: ResumeIn, request: Request, user: User=Depends(get_current_user),
        db: AsyncSession=Depends(get_db), idempotency_key: str=Header(alias='Idempotency-Key')):
    from ..services import workflow_resume
    return await perform(workflow_resume.start(db, run_id, user, key=idempotency_key, request=request,
        **body.model_dump(exclude_none=True)))


@router.post('/workflow-runs/{run_id}:dispatch')
async def dispatch(run_id: str, body: VersionIn, request: Request, user: User=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    await perform(execution.dispatch(db,run_id,user,expected_version=body.expected_version,request=request))
    return await execution.detail(db,run_id,user)


@router.post('/workflow-runs/{run_id}:cancel')
async def cancel(run_id: str, body: VersionIn, user: User=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    return await perform(execution.cancel(db,run_id,user,expected_version=body.expected_version))


@router.post('/workflow-runs/{run_id}:recompute')
async def recompute(run_id: str, body: RecomputeIn, request: Request,
                    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                    idempotency_key: str = Header(alias='Idempotency-Key')):
    from ..services.workflow_recompute import start
    values = body.model_dump(exclude_none=True)
    return await perform(start(db, run_id, user, key=idempotency_key, request=request, **values))


@router.post('/workflow-runs/{run_id}/steps/{step_id}:refresh-confirmation')
async def refresh(run_id: str, step_id: str, body: VersionIn,
                  user: User=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    await execution.owned(db,run_id,user)
    await perform(scheduler.refresh_confirmation(db,run_id,step_id,expected_version=body.expected_version))
    await db.commit()
    return await execution.detail(db,run_id,user)


@router.get('/workflow-runs/{run_id}/migration')
async def migration_preview(run_id:str,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    from ..services.workflow_plan_migration import preview
    return await perform(preview(db,run_id,user))


@router.post('/workflow-runs/{run_id}:migrate')
async def migrate(run_id:str,body:MigrateIn,request:Request,user:User=Depends(get_current_user),
                  db:AsyncSession=Depends(get_db),idempotency_key:str=Header(alias='Idempotency-Key')):
    from ..services.workflow_plan_migration import start
    return await perform(start(db,run_id,user,key=idempotency_key,request=request,
        **body.model_dump(exclude_none=True)))
