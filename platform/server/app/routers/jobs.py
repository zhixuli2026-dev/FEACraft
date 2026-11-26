"""作业路由：提交/状态/日志（轮询增量 + SSE 实时）/取消

对应桌面版 JobsDock + VFProcessSupervisor 语义。
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import sqlite3
import sys
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select,func
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from ..services.async_ops import finish_before_cancel
from ..services.case_read_capacity import file_read_deadline, reading, reading_before_action
from ..services.case_write_capacity import committing
from ..db import SessionLocal, get_db
from ..deps import get_current_user
from ..models import DurableEvent, Job, JobAttempt, User, WorkflowRun, WorkflowStep
from ..services import audit, batch_jobs, distributed_queue, jobrunner, job_execution
from ..services import job_log_io, job_log_reader
from ..services.command_safety import command_block_reason
from ..services.case_write import bounded_case_read, coordinate
from ..services.file_read import FileReadConflict
from ..services.logparse import LogReadLimit, LogRewriteError
from ..storage import _fs_path, _use_s3, exists, get_bytes
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1", tags=["jobs"])

JOB_TYPES = {"solve", "mesh", "pvpython", "geometry", "convert", "shell"}


class PublicationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    publication_id: str
    paths: list[str] | None = Field(default=None, min_length=1, max_length=256)
    require_current: bool = Field(default=True, strict=True)


class JobIn(BaseModel):
    type: str = "shell"
    command: str = Field(min_length=1, max_length=4000)
    runtime: str = "windows"   # windows | wsl
    timeout_seconds: float = Field(86400, gt=0, le=86400)
    max_log_bytes: int = Field(16 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    run_dir: str | None = None  # OpenFOAM solve input/output evidence; shell-only jobs omit it.
    input_paths: list[str] | None = Field(default=None, min_length=1, max_length=256)
    input_publications: list[PublicationInput] | None = Field(default=None, min_length=1, max_length=16)


def _public_status(j,proc,attempt=None):
    return job_execution.public_status(j,attempt,local_status=proc.public_status if proc else None)


def _record_float(text):
    number = float(text)
    if not math.isfinite(number):
        raise ValueError('Non-finite number in the stored record')
    return number


def _record_object(encoded,field,issues):
    try:
        value=json.loads(encoded,parse_float=_record_float,parse_constant=_record_float)
        if not isinstance(value,dict):raise ValueError('Expected a JSON object')
        return value
    except (ValueError,TypeError):
        issues.append({'field':field,'code':'record_unreadable','message':{
            'summary':'历史摘要无法核验，原记录已保留。',
            'inputs':'历史输入记录无法核验，不能据此重放原请求。',
            'outputs':'历史输出记录无法核验，不能据此确认停止或发布。',
        }[field]})
        return None


def _job_out(j: Job, proc: jobrunner.JobProcess | None = None,attempt=None) -> dict:
    pending=j.status=='succeeded' and job_execution.completion_visibility(attempt) in {'pending','invalid'}
    status = _public_status(j,proc,attempt)
    issues=[]
    summary=_record_object(j.parsed_summary,'summary',issues)
    return {
        "id": j.id, "case_id": j.case_id, "type": j.type, "command": j.command[:200],
        "status": status, "exit_code": j.exit_code,
        "log_path": j.log_s3_key, "created_at": j.created_at.isoformat(),
        "ended_at": job_execution.ended_timestamp(j),
        "parsed_summary": summary, "record_issues":issues,
        "phase": job_execution.public_phase(j,attempt),
        "run_id": None, "step_id": None, "step_key": None,
        "attempt": {"id": attempt.id, "number": attempt.number, "root_job_id": attempt.root_job_id,
                    "phase": attempt.phase, "reason_code": attempt.reason_code,
                    "retry_of_attempt_id": attempt.retry_of_attempt_id} if attempt else None,
        "completion_pending":pending,
        "completion_error": proc.completion_error if proc else None,
    }


async def _latest_attempt(db,job_id):
    return await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id)
        .order_by(JobAttempt.number.desc()).limit(1).execution_options(populate_existing=True))


async def _job_views(db,rows,*,attempts_by_id=None):
    if not rows:return []
    if attempts_by_id is None:
        latest=select(JobAttempt.job_id,func.max(JobAttempt.number).label('number')).where(
            JobAttempt.job_id.in_([job.id for job in rows])).group_by(JobAttempt.job_id).subquery()
        attempts=await db.scalars(select(JobAttempt).join(latest,(JobAttempt.job_id==latest.c.job_id)&
            (JobAttempt.number==latest.c.number)).execution_options(populate_existing=True))
        by_id={attempt.job_id:attempt for attempt in attempts}
    else:
        by_id=attempts_by_id
    step_ids={attempt.step_id for attempt in by_id.values() if attempt.step_id}
    links={}
    if step_ids:
        linked=await db.execute(select(WorkflowStep.id,WorkflowStep.step_key,WorkflowRun.id,WorkflowRun.case_id)
            .join(WorkflowRun,WorkflowStep.run_id==WorkflowRun.id).where(WorkflowStep.id.in_(step_ids)))
        links={step_id:(step_key,run_id,case_id) for step_id,step_key,run_id,case_id in linked}
    result=[]
    for job in rows:
        attempt=by_id.get(job.id)
        out=_job_out(job,jobrunner.get_proc(job.id),attempt)
        link=links.get(attempt.step_id) if attempt else None
        # A damaged historical link must never reveal another case's workflow.
        if link and link[2]==job.case_id:
            out.update(step_id=attempt.step_id,step_key=link[0],run_id=link[1])
        result.append(out)
    return result


@router.post("/cases/{case_id}/jobs")
async def submit_job(case_id: int, body: JobIn, request: Request, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db),
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    if body.type not in JOB_TYPES:
        raise HTTPException(400, f"未知作业类型 {body.type}，允许: {sorted(JOB_TYPES)}")
    if body.runtime not in ("windows", "wsl", "wsl-sandbox"):
        raise HTTPException(400, "runtime 只能是 windows、wsl 或 wsl-sandbox")
    case = await get_owned_case(case_id, user, db)
    if body.run_dir is not None:
        from ..services.case_evidence import safe_run_dir
        try:
            body.run_dir = safe_run_dir(body.run_dir)
        except ValueError as error:
            raise HTTPException(400, str(error))
        if body.type != "solve":
            raise HTTPException(400, "run_dir 证据仅用于 solve 作业")
    payload = body.model_dump(exclude={"input_paths",'input_publications'})
    publication_inputs = None
    if body.input_publications is not None:
        from ..services.publication_inputs import normalize
        try:
            publication_inputs = normalize([item.model_dump() for item in body.input_publications])
        except ValueError as error:
            raise HTTPException(400,str(error)) from error
        payload['input_publications'] = publication_inputs
    if body.input_paths is not None:
        payload["input_paths"] = body.input_paths
    if (body.input_paths is not None or publication_inputs is not None) and body.run_dir is not None:
        raise HTTPException(400, "隔离运行的专业求解证据尚需版本化处理器，不能与旧 run_dir 证据混用")
    digest = job_execution.request_hash(payload)
    try:
        replay = await job_execution.lookup_submission(db, case.id, user.id, idempotency_key, digest)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except job_execution.ExecutionConflict as error:
        raise HTTPException(409, {"code":error.code, "message":error.detail}) from error
    if replay is not None:
        return _job_out(replay, jobrunner.get_proc(replay.id),await _latest_attempt(db,replay.id))
    if body.run_dir is not None:
        required = ["system/controlDict", "constant/polyMesh/points", "constant/polyMesh/faces",
                    "constant/polyMesh/owner", "constant/polyMesh/boundary"]
        if any(not exists(case.s3_prefix, f"{body.run_dir}/{path}") for path in required):
            raise HTTPException(400, "求解证据需要 controlDict 和已生成的 OpenFOAM 网格")
    block_reason = command_block_reason(body.command)
    if block_reason:
        raise HTTPException(400, f"{block_reason}，已拒绝执行")
    spec = {"command":body.command, "runtime":body.runtime, "case_prefix":case.s3_prefix,
        "argv":None, "timeout_seconds":body.timeout_seconds, "max_log_bytes":body.max_log_bytes,
        "evidence_run_dir":body.run_dir}
    from contextlib import nullcontext
    from ..services.case_command_handler import prepare_remote_submission, uses_default_inputs
    from ..services.command_input_seal import stage
    from ..services.document_admission import authorize, check_access
    from ..services.quota import check_job_quota
    user_id = user.id
    remote = _use_s3()
    typed_default = uses_default_inputs(body.type, spec, body.input_paths, publication_inputs)
    shell_input = body.type == 'shell' and publication_inputs is None
    local_seal = not remote and (typed_default or shell_input)
    remote_shell = remote and shell_input
    publication_seal = publication_inputs is not None
    try:
        staging = nullcontext()
        if typed_default or shell_input or publication_seal:
            await check_job_quota(db, user, case_id=case_id)
            if remote and typed_default:
                await finish_before_cancel(db.commit())
                prepared = await prepare_remote_submission(case_id, body.type, spec)
                staging = stage(prepared, case_id, user_id)
            else:
                db.expunge(case)
                await finish_before_cancel(db.rollback())
                if local_seal:
                    from ..services.typed_command_submission import stage as stage_local

                    staging = stage_local(case,user_id,body.type,spec,input_paths=body.input_paths)
                elif publication_seal:
                    from ..services import publication_command_submission

                    if remote:
                        staging = publication_command_submission.stage_remote(db,case,user_id,idempotency_key,
                            digest,spec,body.input_paths,publication_inputs)
                    else:
                        staging = publication_command_submission.stage_local(case,user_id,spec,body.input_paths,
                            publication_inputs)

        async def commit(staged):
            selected, actor = case, user
            if staged is not None or remote_shell or publication_seal:
                selected, actor = await authorize(db,case_id,user_id)
            async with committing(db,selected):
                if staged is not None:
                    await check_access(db,selected,actor)
                    if shell_input and body.input_paths is None:
                        from ..services.attempt_workspace import require_safe_history

                        await require_safe_history(db,selected)
                await check_job_quota(db,actor,case_id=selected.id)
                job, created = await job_execution.submit(db,case_id=selected.id,user_id=actor.id,
                    key=idempotency_key,digest=digest,job_type=body.type,command=body.command,spec=spec,
                    input_paths=body.input_paths,input_publications=publication_inputs,isolate_default=True,
                    staged_remote_command=staged if remote and typed_default else None,
                    staged_workspace=staged if local_seal or remote_shell or publication_seal else None)
                if created:
                    from ..services.job_delivery import request as request_delivery
                    await request_delivery(db,job,user_id=actor.id,mode='redis' if distributed_queue.distributed_enabled() else 'local')
            return job, created, selected.s3_prefix

        async def persist():
            async with staging as staged:
                return await commit(staged)

        if local_seal or remote_shell or publication_seal:
            from ..services.request_lifecycle import wait

            async def commit_first(prepared):
                return await commit(prepared[0] if prepared is not None else None)

            operation = (_stage_remote_commands(db,case,user_id,idempotency_key,digest,body.type,[spec],commit_first,
                code='command_seal',label='封存作业输入',input_paths=body.input_paths,
                allow_legacy=not (sys.platform=='linux' and body.runtime=='windows'))
                if remote_shell else persist())
            j, created, prefix = await wait(operation,request=request)
        else:
            j, created, prefix = await persist()
    except job_execution.ExecutionConflict as error:
        raise HTTPException(409, {"code":error.code, "message":error.detail}) from error
    except ValueError as error:
        from ..services.case_write import CaseWriteConflict
        if isinstance(error, CaseWriteConflict):
            raise
        raise HTTPException(400, str(error)) from error
    if not created:
        return _job_out(j, jobrunner.get_proc(j.id),await _latest_attempt(db,j.id))
    if distributed_queue.distributed_enabled():
        from ..services.job_delivery import deliver
        try:
            await deliver(j.id)
        except Exception as error:
            print(f'[WARN] Job #{j.id} durable delivery deferred: {type(error).__name__}')
        proc = None
    else:
        try:
            proc = await jobrunner.start_job(j.id, body.command, prefix, runtime=body.runtime,
                                             timeout_seconds=body.timeout_seconds,
                                             max_log_bytes=body.max_log_bytes,
                                             evidence_run_dir=body.run_dir)
        except job_execution.JobAlreadyClaimed:
            proc = jobrunner.get_proc(j.id)
        except (OSError, ValueError) as error:
            raise HTTPException(409, f"作业启动失败（#{j.id} 已记录失败）：{error}") from error
        except job_execution.ExecutionConflict as error:
            from ..services.job_delivery import DeliveryDeferred
            if isinstance(error,DeliveryDeferred):
                proc=None  # The admitted request remains queued for recovery delivery.
            else:
                raise HTTPException(409, {'code':error.code,'message':error.detail,'job_id':j.id}) from error
    # Another API or the durable delivery scanner may have cancelled/finished
    # the admitted Job while this request was waiting for its claim.
    await db.refresh(j)
    return _job_out(j, proc,await _latest_attempt(db,j.id))


@router.get("/cases/{case_id}/jobs")
async def list_jobs(case_id: int, user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id, user, db,write=False)
    rows = (await db.scalars(select(Job).where(Job.case_id == case_id)
                             .order_by(Job.id.desc()).limit(50))).all()
    return await _job_views(db,rows)


async def _get_job(job_id: int, user: User, db: AsyncSession,*,write=True) -> Job:
    j = await db.get(Job, job_id)
    if j is None:
        raise HTTPException(404, "作业不存在")
    try:
        await get_owned_case(j.case_id, user, db, write=write)
    except HTTPException as error:
        if error.status_code != 404:
            raise
        raise HTTPException(404, "作业不存在") from None
    return j


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    j = await _get_job(job_id, user, db,write=False)
    proc = jobrunner.get_proc(job_id)
    attempt=await _latest_attempt(db,j.id)
    out = (await _job_views(db,[j],attempts_by_id={j.id:attempt} if attempt else {}))[0]
    from ..services.job_recovery_notice import public as public_recovery
    out['recovery'] = await public_recovery(db, j)
    from ..services.job_recovery_disposition import describe as describe_disposition, records as disposition_records
    disposition = await describe_disposition(db, await disposition_records(db, j.id, user.id))
    out['recovery_disposition'] = disposition if disposition['available_actions'] else None
    from ..services.job_input_recovery import links as input_recovery_links
    out['input_recoveries'] = await input_recovery_links(db,j.id)
    if attempt is not None:
        from ..services.process_record import public as public_process_identity
        from ..services.process_stop import public as public_process_stop
        from ..services.native_child_records import public as public_child_processes
        out["attempt"].update({
            "process_identity":public_process_identity(j,attempt),
            "process_stop":public_process_stop(j,attempt),
            "child_processes":public_child_processes(j,attempt)})
        from ..services.job_registration_recovery import public as public_registration
        out["attempt"]["registration"] = await public_registration(db,j,attempt)
        from ..services.execution_budget import WINDOWS_COMPUTE_USAGE
        compute_usage = await db.scalar(select(DurableEvent.payload_json).where(DurableEvent.attempt_id == attempt.id,
            DurableEvent.case_id == j.case_id, DurableEvent.type == WINDOWS_COMPUTE_USAGE))
        if compute_usage is not None:
            out['attempt']['windows_compute_usage'] = _record_object(compute_usage, 'windows_compute_usage', out['record_issues'])
        manifest = _record_object(attempt.input_manifest_json,'inputs',out['record_issues'])
        if manifest and isinstance(manifest.get('linux_compute_policy'),dict) and any(manifest['linux_compute_policy'].values()):
            from ..services.linux_gate_compute import CONTROL, validate_report
            linux_usage = await db.scalar(select(DurableEvent.payload_json).where(DurableEvent.attempt_id == attempt.id,
                DurableEvent.case_id == j.case_id, DurableEvent.type == CONTROL))
            if linux_usage is not None:
                try:
                    identity = json.loads(attempt.process_identity_json)
                    out['attempt']['linux_compute_usage'] = validate_report(json.loads(linux_usage),
                        identity['launch_nonce'],manifest['linux_compute_policy'])
                except (ValueError,TypeError,KeyError):
                    out['record_issues'].append({'field':'linux_compute_usage','code':'record_unreadable',
                        'message':'Linux 作业用量无法核验，原记录已保留。'})
        if manifest and isinstance(manifest.get('native_compute_policy'),dict) and any(manifest['native_compute_policy'].values()):
            from ..services.job_native_scopes import public_compute_usage
            try:
                native_usage=await public_compute_usage(db,j,attempt,manifest['native_compute_policy'])
                if native_usage is not None:
                    out['attempt']['wsl_compute_usage']=native_usage
            except (ValueError,TypeError,KeyError):
                out['record_issues'].append({'field':'wsl_compute_usage','code':'record_unreadable',
                    'message':'WSL 作业用量无法核验，原记录已保留。'})
        outputs = _record_object(attempt.output_manifest_json,'outputs',out['record_issues'])
        if manifest and isinstance(manifest.get('handler'),dict):
            out['attempt']['handler'] = manifest['handler']
        if manifest and isinstance(manifest.get('operation'),dict):
            out['attempt']['operation'] = manifest['operation']
        if manifest and isinstance(manifest.get("workspace"),dict):
            out["attempt"]["workspace"] = manifest["workspace"]
        if outputs is not None:out["attempt"]["outputs"] = outputs
    if proc is not None:
        out["log_backlog"] = proc.backlog()[-4000:]
    else:
        case = await get_owned_case(j.case_id, user, db,write=False)
        selected = job_log_reader.select(j, attempt, None, out['status'])
        prefix = case.s3_prefix
        await db.rollback()
        out['log_backlog'] = (await job_log_io.read(selected, prefix, 0, 4000, None, tail=4000))['chunk']
    return out


class RepeatJobIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action: str=Field(pattern='^(retry|rerun|resume)$')
    source_attempt_id: str=Field(min_length=36,max_length=36)
    source_version: int=Field(ge=0,strict=True)
    expected_revision: int|None=Field(default=None,ge=0,strict=True)
    expected_event_sequence: int|None=Field(default=None,ge=0,strict=True)
    parameters: dict|None=None
    reprepare_handler: dict|None=None


class RecoverInputsIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action: Literal['reprepare_current_inputs']
    source_attempt_id: str=Field(min_length=36,max_length=36)
    source_snapshot_sha256: str=Field(pattern='^[0-9a-f]{64}$')
    expected_revision: int=Field(ge=0,strict=True)
    expected_event_sequence: int=Field(ge=1,strict=True)
    handler: dict|None=None
    parameters: dict|None=None
    command: str|None=Field(default=None,min_length=1,max_length=4000)
    runtime: Literal['windows','wsl','wsl-sandbox']='windows'
    input_paths: list[str]|None=Field(default=None,min_length=1,max_length=256)
    timeout_seconds: float=Field(default=86400,gt=0,le=86400)
    max_log_bytes: int=Field(default=16*1024*1024,ge=1024,le=64*1024*1024)
    note: str=Field(default='',max_length=2000,pattern=r'^[^\x00]*$')


@router.get('/jobs/{job_id}/input-recovery')
async def input_recovery_plan(job_id:int,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    from ..services import job_input_recovery
    return await job_input_recovery.options(db,job_id,user)


@router.post('/jobs/{job_id}:recover-inputs',status_code=202)
async def recover_inputs(job_id:int,body:RecoverInputsIn,request:Request,user:User=Depends(get_current_user),
        db:AsyncSession=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key')):
    from ..services import job_input_recovery,job_delivery
    try:
        job,receipt,created=await job_input_recovery.perform(db,job_id,user,body.model_dump(),idempotency_key,request=request)
        if created:
            try:await job_delivery.deliver(job.id)
            except Exception as error:print('[WARN] Input replacement durable delivery deferred: '+type(error).__name__)
        await db.refresh(job)
        out=(await _job_views(db,[job]))[0]
        out.update(input_recovery=receipt,created=created)
        return out
    except job_execution.ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail,'job_id':error.job_id}) from error
    except OSError:
        raise HTTPException(409,{'code':'recovery_required','message':'当前输入或运行环境不可用，旧请求仍按原记录保留'}) from None
    except ValueError as error:
        from ..services.case_write import CaseWriteConflict
        if isinstance(error,CaseWriteConflict):raise
        raise HTTPException(400,str(error)) from error


@router.get('/jobs/{job_id}/operations')
async def job_operations(job_id:int,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    from ..services import job_repeat
    return await job_repeat.options(db,job_id,user)


@router.post('/jobs/{job_id}:repeat',status_code=202)
async def repeat_job(job_id:int,body:RepeatJobIn,request:Request,user:User=Depends(get_current_user),
                     db:AsyncSession=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key')):
    from ..services import job_repeat,job_delivery
    try:
        payload=body.model_dump()
        # Preserve digests of requests committed before this optional field
        # existed, including clients which explicitly send null.
        if payload['reprepare_handler'] is None:payload.pop('reprepare_handler')
        job,created=await job_repeat.perform(db,job_id,user,payload,idempotency_key,request=request)
        if created:await job_delivery.deliver(job.id)
        await db.refresh(job)
        out=(await _job_views(db,[job]))[0]
        attempt=await _latest_attempt(db,job.id)
        out['operation']=json.loads(attempt.input_manifest_json).get('operation')
        out['created']=created
        return out
    except job_execution.ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail,'job_id':error.job_id}) from error
    except OSError as error:
        raise HTTPException(409,{'code':'recovery_required','message':'封存输入、成果或运行环境不可用，请检查原尝试'}) from error
    except ValueError as error:
        from ..services.case_write import CaseWriteConflict
        if isinstance(error,CaseWriteConflict):raise
        raise HTTPException(400,str(error)) from error


@router.get("/jobs/{job_id}/publication-recovery")
async def job_publication_recovery(job_id: int, user: User = Depends(get_current_user),
                                   db: AsyncSession = Depends(get_db)):
    from ..services.publication_journal import diagnose
    await _get_job(job_id,user,db,write=False)
    user_id=user.id
    await db.rollback()
    return await diagnose(job_id,user_id)


class RecoverOutputsIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    attempt_id: str=Field(min_length=36,max_length=36)
    inventory_sha256: str=Field(pattern='^[0-9a-f]{64}$')


class RecoverPreparationIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    attempt_id: str=Field(min_length=36,max_length=36)
    stage_sha256: str=Field(pattern='^[0-9a-f]{64}$')


class RecoveryDispositionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attempt_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    action: str = Field(pattern=r'^retain_for_review$')


@router.get('/jobs/{job_id}/recovery-disposition')
async def recovery_disposition_options(job_id: int, user: User = Depends(get_current_user)):
    from ..services.job_recovery_disposition import options
    from ..services.workflow_state import ExecutionConflict
    try:
        return await options(job_id, user.id)
    except ExecutionConflict as error:
        raise HTTPException(409, {'code': error.code, 'message': error.detail}) from error


@router.post('/jobs/{job_id}:retain-for-review')
async def retain_job_for_review(job_id: int, body: RecoveryDispositionIn, user: User = Depends(get_current_user)):
    from ..services.job_recovery_disposition import retain
    from ..services.workflow_state import ExecutionConflict
    try:
        return await retain(job_id, user.id, body.attempt_id, body.snapshot_sha256)
    except ExecutionConflict as error:
        raise HTTPException(409, {'code': error.code, 'message': error.detail}) from error


@router.get('/jobs/{job_id}/preparation-recovery')
async def preparation_recovery_options(job_id:int,user:User=Depends(get_current_user)):
    from ..services.finalizing_stage_recovery import options
    try:return await options(job_id,user.id)
    except job_execution.ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except (ValueError,KeyError,TypeError,AttributeError,OSError) as error:
        raise HTTPException(409,{'code':'early_finalization_unavailable','message':'原收尾阶段、文件或物理停止证明无法核验；保留现有数据'}) from error


@router.post('/jobs/{job_id}:recover-preparation')
async def recover_preparation(job_id:int,body:RecoverPreparationIn,request:Request,user:User=Depends(get_current_user)):
    from ..services.finalizing_stage_recovery import recover
    from ..services.request_lifecycle import wait
    try:return await wait(recover(job_id,user.id,body.attempt_id,body.stage_sha256),request=request)
    except job_execution.ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except TimeoutError as error:
        raise HTTPException(408,{'code':'early_finalization_timeout','message':'收尾阶段接续期限已耗尽；保留原阶段和文件计划'}) from error
    except (ValueError,KeyError,TypeError,AttributeError,OSError) as error:
        raise HTTPException(409,{'code':'early_finalization_unavailable','message':'原收尾阶段、文件或物理停止证明无法核验；保留现有数据'}) from error


@router.get('/jobs/{job_id}/output-recovery')
async def output_recovery_options(job_id:int,user:User=Depends(get_current_user)):
    from ..services.attempt_output_recovery import options
    try:return await options(job_id,user.id)
    except job_execution.ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error


@router.post('/jobs/{job_id}:recover-outputs')
async def recover_outputs(job_id:int,body:RecoverOutputsIn,request:Request,user:User=Depends(get_current_user)):
    from ..services.attempt_output_recovery import recover
    from ..services.request_lifecycle import wait
    try:return await wait(recover(job_id,user.id,body.attempt_id,body.inventory_sha256),request=request)
    except job_execution.ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except TimeoutError as error:
        raise HTTPException(408,{'code':'output_recovery_timeout','message':'输出补传核验期限已耗尽'}) from error


@router.post('/jobs/{job_id}:restore-output-cache')
async def restore_output_cache(job_id:int,body:RecoverOutputsIn,request:Request,user:User=Depends(get_current_user)):
    from ..services.attempt_cache_recovery import restore
    from ..services.request_lifecycle import wait
    try:return await wait(restore(job_id,user.id,body.attempt_id,body.inventory_sha256),request=request)
    except job_execution.ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except TimeoutError as error:
        raise HTTPException(408,{'code':'output_recovery_timeout','message':'输出恢复期限已耗尽'}) from error


@router.post('/jobs/{job_id}:finalize-outputs')
async def finalize_outputs(job_id:int,body:RecoverOutputsIn,request:Request,user:User=Depends(get_current_user)):
    from ..services.job_output_finalizing import recover
    from ..services.request_lifecycle import wait
    try:return await wait(recover(job_id,user.id,body.attempt_id,body.inventory_sha256),request=request)
    except job_execution.ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except TimeoutError as error:
        raise HTTPException(408,{'code':'finalization_recovery_timeout','message':'收尾恢复期限已耗尽'}) from error


@router.get("/jobs/{job_id}/process")
async def job_process_identity(job_id: int, user: User = Depends(get_current_user),
                               db: AsyncSession = Depends(get_db)):
    from ..services.process_record import inspect
    job=await _get_job(job_id,user,db)
    attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id)
        .order_by(JobAttempt.number.desc()).limit(1))
    return {'job_id':job.id,'attempt_id':attempt.id if attempt else None,
        **await inspect(job,attempt)}


@router.get("/jobs/{job_id}/log")
async def job_log(job_id: int, offset: int = Query(0, ge=0),
                  limit: int | None = Query(None, ge=4, le=1048576),
                  end_offset: int | None = Query(None, ge=0),
                  user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    """Read raw-byte cursor pages, preserving the legacy unbounded GET default."""
    if end_offset is not None and end_offset < offset:
        raise HTTPException(422, 'end_offset must not precede offset')
    j = await _get_job(job_id, user, db, write=False)
    case_prefix = (await get_owned_case(j.case_id, user, db, write=False)).s3_prefix
    proc = jobrunner.get_proc(job_id)
    attempt=await _latest_attempt(db,job_id)
    selected = job_log_reader.select(j, attempt, proc, _public_status(j,proc,attempt))
    await db.rollback()
    return await job_log_io.read(selected, case_prefix, offset, limit, end_offset)


@router.get("/jobs/{job_id}/log/parse")
async def job_log_parse(job_id: int, offset: int = Query(0, ge=0), fingerprint: str | None = None,
                        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Read an authorized Job's complete log lines from a byte-and-fingerprint cursor."""
    job = await _get_job(job_id, user, db, write=False)
    prefix = (await get_owned_case(job.case_id, user, db, write=False)).s3_prefix
    proc = jobrunner.get_proc(job_id)
    attempt = await _latest_attempt(db, job_id)
    selected = job_log_reader.select(job, attempt, proc, _public_status(job, proc, attempt))
    await db.rollback()
    try:
        return await bounded_case_read(job_log_reader.parse, selected, prefix, offset, fingerprint,
                                       code='job_log_parse', label='作业日志解析')
    except LogRewriteError as error:
        raise HTTPException(409, {'code':'log_prefix_changed','message':str(error)}) from error
    except FileReadConflict as error:
        raise HTTPException(409, {'code':'file_changed_during_read','message':'日志读取期间发生变化'}) from error
    except LogReadLimit as error:
        raise HTTPException(413, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/jobs/{job_id}/log-stream")
async def job_log_stream(job_id: int, request: Request, user: User = Depends(get_current_user),
                         offset: int | None = Query(None, ge=0),
                         db: AsyncSession = Depends(get_db)):
    """Both SSE forms read bounded pages without a per-viewer output queue."""
    j = await _get_job(job_id, user, db, write=False)
    case_id, user_id = j.case_id, user.id
    await db.rollback()
    return StreamingResponse(_cursor_log_stream(job_id, case_id, user_id, offset or 0, request, legacy=offset is None),
        media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


async def _cursor_log_stream(job_id, case_id, user_id, offset, request, *, legacy=False):
    from ..db import SessionLocal
    from ..deps import bearer
    keepalive = asyncio.get_running_loop().time() + 5
    selected_attempt = None
    first = True
    diagnostic_cursor = 0
    while True:
        try:
            async with SessionLocal() as session:
                user = await get_current_user(request, await bearer(request), session)
                if user.id != user_id:
                    raise HTTPException(409, 'Account identity changed')
                job = await _get_job(job_id, user, session, write=False)
                case = await get_owned_case(job.case_id, user, session, write=False)
                attempt = await _latest_attempt(session, job_id)
                identity = attempt.id if attempt else None
                if job.case_id != case_id or (not first and identity != selected_attempt):
                    raise HTTPException(409, 'Log identity changed')
                selected_attempt = identity
                first = False
                proc = jobrunner.get_proc(job_id)
                selected = job_log_reader.select(job, attempt, proc, _public_status(job, proc, attempt))
                prefix = case.s3_prefix
            value = await job_log_io.read(selected, prefix, offset, 65536, None)
            if value['chunk'] or value['reset']:
                yield 'data: ' + json.dumps({'type': 'log', 'text': value.pop('chunk'), **value}, ensure_ascii=False) + '\n\n'
            offset = value['offset']
            if value['has_more']:
                continue
            if legacy and proc is not None:
                diagnostic_cursor, diagnostic = proc.legacy_diagnostics(diagnostic_cursor)
                if diagnostic:
                    yield 'data: ' + json.dumps({'type': 'log', 'text': diagnostic, 'diagnostic': True}, ensure_ascii=False) + '\n\n'
            if proc is not None and proc.completion_error:
                yield 'data: ' + json.dumps({'type': 'error', 'code': 'persistence_failed', 'detail': proc.completion_error}) + '\n\n'
                return
            completed, problem = await _stream_completion(job_id, case_id)
            if problem:
                yield 'data: ' + json.dumps({'type': 'error', 'code': problem}) + '\n\n'
                return
            if completed is not None and (proc is None or proc.finished):
                # This page may have reached EOF while the writer was still
                # running. Read again from its byte cursor after settlement.
                if not selected.settled:
                    continue
                if not value['available']:
                    yield 'data: ' + json.dumps({'type': 'error', 'code': 'log_missing'}) + '\n\n'
                    return
                yield 'data: ' + json.dumps({**completed.event(), 'job_id': job_id, 'case_id': case_id, 'attempt_id': selected_attempt}) + '\n\n'
                return
        except HTTPException as error:
            code = ('permission_denied' if error.status_code in {401, 403, 404} else
                error.detail['code'] if isinstance(error.detail, dict) and error.detail.get('code') in {'log_read_busy','log_read_timeout'} else 'state_conflict')
            yield 'data: ' + json.dumps({'type': 'error', 'code': code}) + '\n\n'
            return
        except Exception:
            yield 'data: ' + json.dumps({'type': 'error', 'code': 'log_read_failed'}) + '\n\n'
            return
        if asyncio.get_running_loop().time() >= keepalive:
            yield ': keepalive\n\n'
            keepalive = asyncio.get_running_loop().time() + 5
        await asyncio.sleep(.25)


async def _stream_completion(job_id,case_id):
    """Read-only cross-owner completion; accepted cancellation is still draining."""
    from ..db import SessionLocal
    from ..services.process_stop import public as public_stop
    async with SessionLocal() as session:
        job=await session.get(Job,job_id)
        if job is None or job.case_id!=case_id:
            return None,'state_conflict'
        attempt=await session.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id)
            .order_by(JobAttempt.number.desc()).limit(1))
        visibility=job_execution.completion_visibility(attempt)
        if visibility=='invalid':return None,'evidence_invalid'
        if visibility!='pending' and job.status in job_execution.TERMINAL and (attempt is None or attempt.phase in job_execution.TERMINAL):
            if attempt is None or attempt.process_identity_json=='{}':
                return job_execution.completion_snapshot(job,attempt),None
            output=json.loads(attempt.output_manifest_json)
            if 'process_stop' in output:
                if public_stop(job,attempt) is None:return None,'evidence_invalid'
                return job_execution.completion_snapshot(job,attempt),None
            if job.status=='interrupted':return None,'recovery_required'
        if attempt is not None and attempt.claim_token is not None and attempt.lease_expires_at:
            expires=attempt.lease_expires_at
            if (expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc))<=datetime.now(timezone.utc):
                return None,'recovery_required'
        return None,None


@router.post("/jobs/{job_id}:cancel")
async def cancel_job(job_id: int, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    j = await _get_job(job_id, user, db)
    if j.status not in {"queued", "running"}:
        if j.status=='succeeded' and job_execution.completion_visibility(await _latest_attempt(db,job_id))=='pending':
            return {'ok':False,'message':'结果已提交，正在完成产物收尾，不能撤销本次构建'}
        return {"ok": False, "message": f"作业不在运行中（{j.status}）"}
    # The database owns the decision, including finalization and jobs launched
    # by another API. A local successful process exit is not a committed result.
    try:
        await jobrunner.request_cancel(job_id, actor_user_id=user.id)
    except jobrunner.JobStateConflict as conflict:
        return {"ok": False, "message": f"作业已结束（{conflict.current}）"}
    return {"ok": True, "status": "cancelled"}


# ---------- C12 有界多工况批量队列 ----------
from ..services.job_batch_operations import OperationIn as BatchOperationIn


class BatchJobIn(BaseModel):
    commands: list[str] | None = Field(default=None, min_length=1, max_length=50)
    operations: list[BatchOperationIn] | None = Field(default=None, min_length=1, max_length=50)
    type: str = "shell"
    runtime: str = "windows"
    max_concurrent: int = Field(default=2, ge=1, le=4)


_BATCH_ID = re.compile(r"^[0-9a-f]{32}$")
_BATCH_DIR = ".vibeflow/job-batches"
_BATCH_COUNTS = ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")


def _batch_manifest_path(batch_id: str) -> str:
    if not _BATCH_ID.fullmatch(batch_id):
        raise HTTPException(404, "批次不存在")
    return f"{_BATCH_DIR}/{batch_id}.json"


def _read_batch(case, batch_id: str) -> dict:
    path = _batch_manifest_path(batch_id)
    if not exists(case.s3_prefix, path):
        raise HTTPException(404, "批次不存在")
    try:
        manifest = json.loads(get_bytes(case.s3_prefix, path))
        if manifest["case_id"] != case.id or not isinstance(manifest["job_ids"], list):
            raise ValueError
        return manifest
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise HTTPException(500, "批次清单损坏") from error


def _batch_index(prefix):
    path = f"{_BATCH_DIR}/index.json"
    original = get_bytes(prefix, path) if exists(prefix, path) else None
    try:
        ids = json.loads(original) if original is not None else []
        if not isinstance(ids, list) or any(not isinstance(item, str) or not _BATCH_ID.fullmatch(item) for item in ids):
            raise ValueError
    except (ValueError, UnicodeError) as error:
        raise HTTPException(500, "批次索引损坏；保留原文件，未提交新批次") from error
    return path, original, ids


async def _batch_out(db: AsyncSession, manifest: dict, rows: list[Job]) -> dict:
    from ..services.job_batch_operations import members
    operations = await members(db, manifest, rows)
    by_id = {job.id: job for job in rows}
    jobs = await _job_views(db, [by_id[job_id] for job_id in manifest['job_ids'] if job_id in by_id])
    counts = {status: 0 for status in _BATCH_COUNTS}
    for job in jobs:
        status = job["status"] if job["status"] in counts else "failed"
        counts[status] += 1
    active = counts["queued"] + counts["running"]
    if counts["running"]:
        status = "running"
    elif counts["queued"]:
        status = "queued"
    elif counts["failed"] or counts["interrupted"]:
        status = "failed"
    elif counts["cancelled"]:
        status = "cancelled"
    else:
        status = "succeeded"
    return {"id": manifest["id"], "case_id": manifest["case_id"],
            "created_at": manifest["created_at"], "status": status,
            "max_concurrent": manifest["max_concurrent"], "total": len(jobs),
            "active": active, "counts": counts, "jobs": jobs,
            **({'operations':operations} if operations is not None else {})}


async def _load_batch_rows(db: AsyncSession, manifest: dict) -> list[Job]:
    return list((await db.scalars(select(Job).where(
        Job.case_id == manifest["case_id"], Job.id.in_(manifest["job_ids"])))).all())


def _batch_manifests(case):
    _, _, ids = _batch_index(case.s3_prefix)
    for batch_id in ids[:50]:
        try:
            yield _read_batch(case, batch_id)
        except HTTPException:
            continue


@asynccontextmanager
async def _sqlite_batch_metadata_session():
    """Wait for a fresh writer transaction before reading the file journal."""
    while True:
        async with SessionLocal() as metadata_db:
            connection = await metadata_db.connection()
            previous_timeout = (await connection.exec_driver_sql('PRAGMA busy_timeout')).scalar_one()
            acquired = False
            try:
                await finish_before_cancel(connection.exec_driver_sql('PRAGMA busy_timeout=0'))
                await finish_before_cancel(connection.exec_driver_sql('BEGIN IMMEDIATE'))
                acquired = True
            except OperationalError as error:
                code = getattr(error.orig, 'sqlite_errorcode', None)
                if not isinstance(code, int) or code & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
            finally:
                await finish_before_cancel(connection.exec_driver_sql(f'PRAGMA busy_timeout={previous_timeout}'))
            if acquired:
                yield metadata_db
                return
        # A waiting poll must give back its pooled connection before retrying.
        await asyncio.sleep(.05)


async def _sqlite_batch_views(db, case_id, user, batch_id=None):
    from ..services.durable_events import ensure_parent_transaction

    async with file_read_deadline('job_batches_read', '作业批次读取'):
        # End the pre-admission authorization read before either new transaction.
        await finish_before_cancel(db.commit())
        try:
            async with _sqlite_batch_metadata_session() as metadata_db:
                case = await get_owned_case(case_id, user, metadata_db, write=False)
                async with reading(metadata_db, case):
                    manifests = (list(_batch_manifests(case)) if batch_id is None else [_read_batch(case, batch_id)])
                    if manifests:
                        # Pin the read-only WAL snapshot while the metadata fence
                        # is still held, then let workers advance during projection.
                        await ensure_parent_transaction(db)
                        await db.scalar(select(Job.id).where(Job.case_id == case_id).limit(1))
                await finish_before_cancel(metadata_db.commit())
            return [await _batch_out(db, manifest, await _load_batch_rows(db, manifest)) for manifest in manifests]
        finally:
            await finish_before_cancel(db.rollback())


@router.post("/cases/{case_id}/jobs:batch", status_code=202)
async def submit_batch(case_id: int, body: BatchJobIn, request: Request,
                       user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db),
                       idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    if (body.commands is None)==(body.operations is None):
        raise HTTPException(400, 'A batch requires exactly one of commands or operations')
    if body.operations is not None:
        if body.type!='shell' or body.runtime!='windows':
            raise HTTPException(400, 'Registered operations carry their own type and runtime')
        from ..services.job_batch_operations import validate
        validate(body.operations)
    if body.type not in JOB_TYPES:
        raise HTTPException(400, f"未知作业类型 {body.type}")
    if body.runtime not in ("windows", "wsl", "wsl-sandbox"):
        raise HTTPException(400, "runtime 只能是 windows、wsl 或 wsl-sandbox")
    commands = [command.strip() for command in (body.commands or [])]
    for command in commands:
        if not command or len(command) > 4000:
            raise HTTPException(400, "每项命令长度必须为 1–4000 字符")
        reason = command_block_reason(command)
        if reason:
            raise HTTPException(400, f"{reason}，整个批次未提交")
    from ..services import case_write, document_admission, job_delivery, legacy_job_admission as admission
    # Preserve historical command request hashes across this optional extension.
    payload = body.model_dump(exclude={'operations'}) if body.operations is None else body.model_dump(exclude_none=True)
    specs=[admission.execution_spec(case,command,runtime=body.runtime) for command in commands]
    user_id = user.id
    remote = _use_s3()
    try:
        job_execution.validate_key(idempotency_key)
        digest=job_execution.request_hash({'endpoint':'jobs.batch','payload':payload})
        replay=await job_execution.lookup_submission(db,case_id,user_id,idempotency_key,digest)
        if replay is None:
            from ..services.request_lifecycle import wait

            db.expunge(case)
            await finish_before_cancel(db.rollback())

            async def commit(staged=None, prepared=None):
                selected,actor=await document_admission.authorize(db,case_id,user_id)
                return await _commit_batch(db,selected,actor,idempotency_key,body,payload,specs,
                                           staged=staged,prepared_batch=prepared)

            if body.operations is None:
                operation=_stage_batch(db,case,user_id,idempotency_key,digest,body.type,specs,commit,remote=remote)
            else:
                from ..services import job_batch_operations

                async def prepare_and_commit():
                    staging=(job_batch_operations.stage_remote(db,case,user_id,idempotency_key,digest,body.operations)
                             if remote else job_batch_operations.stage_local(case,user_id,body.operations))
                    async with staging as prepared:
                        return await commit(prepared=prepared)

                operation=prepare_and_commit()
            manifest, rows, created=await wait(operation,request=request)
        else:
            manifest, rows, created=await _commit_batch(db,case,user,idempotency_key,body,payload,specs)
    except ValueError as error:
        if isinstance(error,case_write.CaseWriteConflict):
            raise
        raise HTTPException(400,str(error)) from error
    if not created:
        return await _batch_out(db,manifest,rows)
    batch_id=manifest['id']
    if distributed_queue.distributed_enabled():
        # Every job in this batch has the same case. Redis admits its head;
        # the durable recovery scan advances subsequent eligible jobs.
        head=rows[0]
        try:
            await job_delivery.deliver(head.id)
        except Exception as error:
            print(f'[WARN] Batch job #{head.id} durable delivery deferred: {type(error).__name__}')
    else:
        state = batch_jobs.BatchState(batch_id=batch_id, case_id=case_id, user_id=user_id,
                                      case_prefix=case.s3_prefix, runtime=body.runtime,
                                      job_ids=manifest["job_ids"], max_concurrent=body.max_concurrent)
        await batch_jobs.start_batch(state)
    return await _batch_out(db,manifest,rows)


async def _stage_batch(db,case,user_id,key,digest,job_type,specs,commit,*,remote):
    if not remote:
        from ..services.typed_command_submission import stage_batch

        async with stage_batch(case,user_id,job_type,specs) as staged:
            return await commit(staged)

    return await _stage_remote_commands(db,case,user_id,key,digest,job_type,specs,commit,
                                        code='batch_seal',label='封存批次输入')


async def _stage_remote_commands(db,case,user_id,key,digest,job_type,specs,commit,*,code,label,
                                 input_paths=None,allow_legacy=False):
    from ..services import command_input_seal
    from ..services.case_read_capacity import file_read_deadline
    from ..services.request_deadline import STAGING_CLEANUP_SECONDS

    async with file_read_deadline(code,label,seconds=STAGING_CLEANUP_SECONDS):
        async with command_input_seal.stage_submission(db,case,user_id,key,digest,code=code,label=label,
                probe_first=job_type=='shell',allow_legacy=allow_legacy) as (replay,lease,owner):
            if replay is not None or lease is None:
                return await commit(None)
            staged = await command_input_seal.upload_command_batch(lease,owner,job_type,specs,input_paths=input_paths)
            return await commit(staged)


async def _commit_batch(db,case,user,key,body,payload,specs,*,staged=None,prepared_batch=None):
    from ..services import legacy_job_admission as admission, job_delivery

    async with admission.prepare(db,case,user,key,'jobs.batch',payload) as request:
        if staged is not None or prepared_batch is not None:
            from ..services.document_admission import check_access
            await check_access(db,case,user)
        if request.replay is not None:
            attempt = await _latest_attempt(db, request.replay.id)
            if attempt is None:
                raise job_execution.ExecutionConflict('evidence_invalid', 'Batch submission has no attempt')
            delivery = await job_delivery.intent(db, request.replay, attempt)
            if delivery is None or delivery['batch'] is None:
                raise job_execution.ExecutionConflict('evidence_invalid', 'Batch submission has no delivery group')
            group = delivery['batch']
            manifest = _read_batch(case, group['id'])
            if manifest['job_ids'] != group['jobs'] or manifest['max_concurrent'] != group['max_concurrent']:
                raise job_execution.ExecutionConflict('evidence_invalid', 'Batch manifest differs from its admission')
            return manifest, await _load_batch_rows(db,manifest), False
        if staged is not None and body.type=='shell':
            from ..services.attempt_workspace import require_safe_history
            await require_safe_history(db,case)
        if prepared_batch is not None:
            from ..services.quota import check_job_quota

            await check_job_quota(db,user,case_id=case.id)
        index_path, _, index = _batch_index(case.s3_prefix)
        batch_id = uuid.uuid4().hex
        manifest_path = _batch_manifest_path(batch_id)
        if exists(case.s3_prefix, manifest_path):
            raise HTTPException(409, "批次标识已存在，请重新提交")
        now = datetime.now(timezone.utc)
        rows = []
        for index_in_batch,item in enumerate(body.operations if body.operations is not None else specs):
            prepared = None
            if body.operations is not None:
                if prepared_batch is None:
                    from ..services.job_batch_operations import prepare
                    prepared = await prepare(db,case,user,item)
                else:
                    prepared = prepared_batch.steps[index_in_batch]
                spec = prepared.spec
            else:
                spec = item
            # Persist unclaimed attempt/request identity even if this item is
            # cancelled before dispatch. A bare cancelled Job cannot prove that
            # it never acquired an uncoordinated native writer.
            options = {'input_paths':prepared.input_paths,'input_publications':prepared.input_publications,
                'generated_inputs':prepared.generated_inputs} if prepared else {}
            if staged is not None:
                name = 'staged_remote_command' if _use_s3() and body.type!='shell' else 'staged_workspace'
                options[name] = staged[index_in_batch]
            if prepared_batch is not None:
                options['staged_workspace'] = prepared_batch.workspaces[index_in_batch]
            job, _ = await job_execution.submit(db, case_id=case.id, user_id=user.id,
                key=key if not rows else None, digest=request.digest, job_type=prepared.job_type if prepared else body.type,
                command=spec['command'], spec=spec, isolate_default=prepared is None,**options)
            if prepared:
                from ..services.step_handlers import bind
                attempt=await _latest_attempt(db,job.id)
                await bind(db,case,job,attempt,prepared,deliver_intent=False)
            rows.append(job)
        if prepared_batch is not None:
            await prepared_batch.require_current()
        from ..services.job_delivery import request as request_delivery
        group={'id':batch_id,'jobs':[job.id for job in rows],'max_concurrent':body.max_concurrent}
        for job in rows:
            await request_delivery(db,job,user_id=user.id,batch=group,
                mode='redis' if distributed_queue.distributed_enabled() else 'local')
        manifest = {"schema": "vibeflow.job-batch.v1", "id": batch_id, "case_id": case.id,
                    "created_at": now.isoformat(), "runtime": body.runtime,
                    "max_concurrent": body.max_concurrent, "job_ids": [job.id for job in rows]}
        if body.operations is not None:
            from ..services.job_batch_operations import SCHEMA
            manifest.update(schema=SCHEMA,request=payload,request_hash=request.digest)
        from ..services import file_transactions
        await file_transactions.write(db, case, {
            manifest_path: json.dumps(manifest, ensure_ascii=False).encode('utf8'),
            index_path: json.dumps([batch_id, *index][:50]).encode('utf8')},
            content_types={manifest_path:'application/json', index_path:'application/json'})
        digest = hashlib.sha256('\n'.join(job.command for job in rows).encode()).hexdigest()
        await audit.case_event(db, case, user.id, 'job.batch.submitted', 'job_batch', batch_id,
                              {'count': len(rows), 'runtime': body.runtime,
                               'max_concurrent': body.max_concurrent, 'commands_sha256': digest})
        await db.flush()
    return manifest, rows, True


@router.get("/cases/{case_id}/job-batches/{batch_id}")
async def get_batch(case_id: int, batch_id: str, user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    if db.get_bind().dialect.name == 'sqlite' and not _use_s3():
        return (await _sqlite_batch_views(db, case_id, user, batch_id))[0]
    async with reading(db, case):
        manifest = _read_batch(case, batch_id)
        result = await _batch_out(db, manifest, await _load_batch_rows(db, manifest))
    await db.commit()
    return result


@router.get("/cases/{case_id}/job-batches")
async def list_batches(case_id: int, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    if db.get_bind().dialect.name == 'sqlite' and not _use_s3():
        return await _sqlite_batch_views(db, case_id, user)
    async with reading(db, case):
        out = [await _batch_out(db, manifest, await _load_batch_rows(db, manifest)) for manifest in _batch_manifests(case)]
    await db.commit()
    return out


@router.post("/cases/{case_id}/job-batches/{batch_id}:cancel")
async def cancel_batch(case_id: int, batch_id: str, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    async with reading_before_action(db, case):
        manifest = _read_batch(case, batch_id)
        rows = await _load_batch_rows(db, manifest)
    # The read scope released its case lock and credit before status writers.
    selected=await batch_jobs.cancel_durable(case.id,batch_id,manifest['job_ids'],actor_user_id=user.id)
    state = batch_jobs.active_batch(batch_id)
    if state is not None:
        batch_jobs.cancel_active(state)
    for job in rows:
        if job.id not in selected:continue
        proc = jobrunner.get_proc(job.id)
        if proc is not None and proc.status == "running":
            proc.cancel()
        if distributed_queue.distributed_enabled():
            try:
                await distributed_queue.cancel(job.id)
            except Exception as error:
                print(f'[WARN] Batch cancellation persisted; Redis wakeup failed: {type(error).__name__}')
    for job in rows:await db.refresh(job)
    return {"ok": bool(selected), **await _batch_out(db, manifest, rows)}


# ---------- 多工况派生（C-run/case-* 编排；原版为 agent 驱动目录复制，这里做成端点） ----------
class DeriveIn(BaseModel):
    base: str = "C-run/case-1"   # 基准求解目录
    name: str = Field(min_length=1, max_length=80)   # 新目录名（C-run/ 下）

@contextmanager
def _derive_source_errors():
    from ..services.case_source import SourceChanged
    from ..services.case_write import CaseWriteConflict
    from ..services.fs_directory import DirectoryMoveError

    try:
        yield
    except (DirectoryMoveError, SourceChanged) as error:
        raise HTTPException(409, str(error)) from error
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as error:
        raise HTTPException(409, '派生期间源工况已变化，请重试') from error
    except CaseWriteConflict:
        raise
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/cases/{case_id}/runs:derive")
async def derive_run(case_id: int, body: DeriveIn,
                     user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    """复制基准 case 目录为 C-run/<name>（跳过时刻目录/日志/.foam 桩，只搬设置与初始场）"""
    from ..config import get_settings
    seconds = get_settings().run_derive_timeout_seconds
    async with file_read_deadline('run_derive', '工况派生', seconds=seconds):
        if _use_s3():
            case = await get_owned_case(case_id, user, db)
        else:
            from .files import _case_for_file_request

            case = await _case_for_file_request(case_id, user, db)
        from ..services.run_derive import source_binding
        async with reading_before_action(db, case):
            await get_owned_case(case_id, user, db)
            selected = await source_binding(db, case)
        with _derive_source_errors():
            if _use_s3():
                return await _derive_s3(case, user, body, selected, seconds)
            return await _derive_fs(db, case, user, body, selected, seconds)


async def _verify_run_source(db, case, user, selected, target):
    from ..services.case_source import SourceChanged
    from ..services.fs_directory import DirectoryMoveError
    from ..services.run_derive import source_binding
    await get_owned_case(case.id, user, db)
    if target.exists():
        raise DirectoryMoveError("目标工况已存在，拒绝覆盖")
    if await source_binding(db, case) != selected:
        raise SourceChanged("派生期间源工况已变化，请重试")


async def _derive_io(operation, *args, seconds):
    return await bounded_case_read(operation, *args, code='run_derive', label='工况派生', seconds=seconds)


async def _derive_s3(case, user, body, selected, seconds):
    from ..services.case_source import SourceChanged
    from ..services.durable_events import lock_case_execution
    from ..services.run_derive import derive_remote, prepare_remote, require_remote_destination, source_binding
    from ..services.storage_operations import inside_control

    async def checked(db):
        await lock_case_execution(db, case.id)
        actual = await get_owned_case(case.id, user, db)
        observed = await source_binding(db, actual)
        # The reservation itself appends events. Compare the complete version
        # before acquisition, then retain the authorization and storage binding.
        if observed[:3] != selected[:3] or (not inside_control() and observed[3] != selected[3]):
            raise SourceChanged("派生期间源工况已变化，请重试")
        return actual

    base, target, records = await _derive_io(prepare_remote, case.s3_prefix, body.base, body.name, seconds=seconds)
    try:
        return await derive_remote(checked, case.s3_prefix, base, target, records)
    except SourceChanged:
        # A winning derivation changes the case event sequence too. Resolve
        # that conflict after releasing SQL and any storage reservation.
        await _derive_io(require_remote_destination, case.s3_prefix, target, seconds=seconds)
        raise


async def _derive_fs(db, case, user, body, selected, seconds):
    from ..services import file_transactions, owned_staging, owner_storage_budget
    from ..services.quota import check_storage_quota
    from ..services.run_derive import DerivedRun

    operation = await _derive_io(DerivedRun, case.s3_prefix, body.base, body.name, seconds=seconds)
    incoming = await _derive_io(operation.inventory, seconds=seconds)
    async with owner_storage_budget.hold_case(db, case) as owner:
        await check_storage_quota(db, owner, incoming)
        async with coordinate(db, case):
            await _verify_run_source(db, case, user, selected, operation.target)
        await finish_before_cancel(db.commit())

    mounted = operation.staging_root if operation.stage_is_billed else None
    async with owned_staging.batch(operation.staging_root, incoming+operation.JOURNAL_BYTES,
                                   operation.STAGE_SCHEMA, target_root=mounted) as directory:
        operation.stage = directory/'journal'
        prepared = await _derive_io(operation.prepare, seconds=seconds)
        async with owner_storage_budget.hold_case(db, case) as owner:
            async with coordinate(db, case):
                await _verify_run_source(db, case, user, selected, operation.target)
                await prepared.publish(db, case)
            await finish_before_cancel(db.commit())
            await file_transactions.finish_committed_edit(db, case, owner.id)
    return {'ok': True, 'created': operation.relative, 'files': len(operation.records)}
