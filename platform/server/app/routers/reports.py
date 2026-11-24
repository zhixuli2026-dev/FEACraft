"""报告生成路由：pandoc 把 E-report/report.md 编译成 HTML/DOCX + LaTeX 编译链（WSL TeX Live）

pandoc 来源：原版安装包内提取的 OSS 二进制（GPL 进程调用，聚合不传染）。
LaTeX：WSL vibeflow-linux 的 latexmk（xelatex 配方，中文走 ctex/xeCJK）。
"""
from pathlib import Path
import asyncio
import json
import subprocess
import sys
import time
from urllib.parse import quote
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, Request, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_

from ..services.case_write_capacity import committing
from ..db import get_db, SessionLocal
from ..deps import get_current_user
from ..models import ArtifactPublication, Case, Job, JobAttempt, User
from ..services import jobrunner, legacy_job_admission as admission
from ..services.latex_table import latex_table
from ..services.latex_errors import parse_latex_errors
from ..services.pdf_pages import numbered_pdf_path, safe_pdf_relative
from ..services.ppt_builder import validate_ppt_document
from ..storage import exists, get_bytes, put_text
from ..services.case_write import CaseWriteConflict, coordinate
from ..services.workflow_state import ExecutionConflict, TERMINAL
from ..services.case_read_capacity import reading
from ..services.publication_result_reads import read_result
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1/cases/{case_id}", tags=["report"])

class ComposeIn(BaseModel):
    output: str = "E-report/report.md"
    overwrite: bool = False
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ArtifactConflictDetail(BaseModel):
    code: str
    message: str
    path: str | None = None
    current_revision: int | None = None


class ArtifactConflict(BaseModel):
    detail: ArtifactConflictDetail


ARTIFACT_CONFLICT_RESPONSE = {
    409: {"model": ArtifactConflict, "description": "产物版本、证据或读取状态发生冲突"}
}


@router.post("/report:compose")
async def compose(case_id: int, body: ComposeIn, request: Request, wait: bool=True, user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db),
                  idempotency_key: str | None=Header(default=None,alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    output = body.output.strip().strip("/")
    if output not in ("E-report/report.md", "E-report/report.generated.md"):
        raise HTTPException(400, "报告输出路径不受支持")
    body.output=output
    return await _registered_compose(db,case,user,body,idempotency_key,wait,request)


async def _registered_compose(db,case,user,body,key,wait,request):
    from ..services import job_execution,step_handlers
    from ..services import compose_input_preparation
    from ..services.async_ops import finish_before_cancel
    from ..services.case_read_capacity import file_read_deadline
    from ..services.case_write import bounded_case_read
    from ..services.compose_artifacts import OUTPUTS
    from ..services.request_lifecycle import wait as wait_for_request
    from ..storage import _use_s3
    try:
        job_execution.validate_key(key)
        case_id,user_id=case.id,user.id
        arguments={'handler':OUTPUTS[body.output][0],'output':body.output,'overwrite':body.overwrite,
            'expected_revision':body.expected_revision,'hash_present':'expected_sha256' in body.model_fields_set,
            'expected_sha256':body.expected_sha256}
        digest=job_execution.request_hash(arguments)
        job=await job_execution.lookup_submission(db,case_id,user_id,key,digest)
        replay=job is not None
        if job is None:
            prefix,revision=await _report_revision(db,case,user_id,code='compose_seal',label='封存报告草稿输入')
            if body.expected_revision is not None and body.expected_revision!=revision:
                raise CaseWriteConflict('revision_conflict','caseDict 已被其他操作修改，请刷新后重试',
                                        current_revision=revision)
            code,label='compose_preparation','报告草稿准备'
            async with file_read_deadline(code,label):
                inputs=await wait_for_request(bounded_case_read(compose_input_preparation.capture,prefix,
                    body.output,body.overwrite,code=code,label=label),request=request)
            if _use_s3():
                job,replay=await wait_for_request(_staged_compose(db,case_id,user_id,body,key,digest,
                    arguments['handler'],revision,inputs),request=request)
            else:
                job,replay=await wait_for_request(_staged_local_compose(db,case_id,user_id,body,key,digest,
                    arguments['handler'],revision,inputs),request=request)
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
        publication=await db.get(ArtifactPublication,json.loads(attempt.publication_json)['publication_id'])
        payload={'job_id':job.id,'status':job_execution.public_status(job,attempt),'publication_id':publication.id,
            'generation':publication.generation,'path':body.output}
        await finish_before_cancel(db.rollback())
        if not replay:
            proc=await step_handlers.launch(payload['job_id'])
            payload['status']=proc.public_status
        if not wait:
            return payload
        return await _await_composition(case_id,payload,replay)
    except ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except ValueError as error:
        if isinstance(error,CaseWriteConflict):
            raise
        raise HTTPException(400,str(error)) from error


def _compose_target_condition(body,inputs,revision):
    digest_before = inputs.previous['sha256'] if inputs.previous is not None else None
    if 'expected_sha256' in body.model_fields_set and body.expected_sha256!=digest_before:
        raise CaseWriteConflict('file_conflict','文件内容已变化，请重新读取后再保存',
                                path=body.output,current_revision=revision)


async def _report_revision(db,case,user_id,*,code,label):
    from ..services.case_read_capacity import reading_before_action
    from ..storage import _use_s3

    case_id=case.id
    try:
        async with reading_before_action(db,case) as snapshot:
            return case.s3_prefix,snapshot.dict_revision
    except CaseWriteConflict as error:
        if not _use_s3() or error.code not in {'file_sync_busy','file_recovery_required'}:
            raise
    from ..services import command_input_seal,document_admission
    async with command_input_seal.stage_case(case_id,user_id,code=code,label=label):
        pass
    case,_=await document_admission.authorize(db,case_id,user_id)
    async with reading_before_action(db,case) as snapshot:
        return case.s3_prefix,snapshot.dict_revision


async def _staged_compose(db,case_id,user_id,body,key,digest,handler,revision,inputs):
    from ..services import command_input_seal, job_execution, report_input_seal
    from ..services.async_ops import finish_before_cancel

    async with command_input_seal.stage_case(case_id,user_id,code='compose_seal',label='封存报告草稿输入') as (lease,owner_id):
        job=await job_execution.lookup_submission(db,case_id,user_id,key,digest)
        if job is not None:
            return job,True
        await finish_before_cancel(db.rollback())
        _compose_target_condition(body,inputs,revision)
        staged=await report_input_seal.prepare(lease,owner_id,user_id,revision,handler,inputs,body.overwrite)
        return await _commit_compose(db,case_id,user_id,body,key,digest,revision,inputs,staged=staged)


async def _staged_local_compose(db,case_id,user_id,body,key,digest,handler,revision,inputs):
    from functools import partial
    from ..services import document_admission, publication_command_submission, step_handlers
    from ..services.async_ops import finish_before_cancel
    from ..services.case_read_capacity import file_read_deadline
    from ..services.request_deadline import STAGING_CLEANUP_SECONDS
    from ..services.publication_inputs import PublicationInputStale

    case,_=await document_admission.authorize(db,case_id,user_id)
    if case.dict_revision!=revision:
        raise CaseWriteConflict('revision_conflict','caseDict 已被其他操作修改，请刷新后重试',
                                current_revision=case.dict_revision)
    db.expunge(case)
    await finish_before_cancel(db.rollback())
    code,label='compose_seal','封存报告草稿输入'
    prepare=partial(step_handlers.get_handler(handler).prepare,draft_inputs=inputs,
        parameters={'run_dir':inputs.run_dir,'overwrite':body.overwrite},
        budget={'timeout_seconds':120,'max_log_bytes':4*1024*1024})
    try:
        async with file_read_deadline(code,label,seconds=STAGING_CLEANUP_SECONDS):
            async with publication_command_submission.stage_step(db,case,user_id,prepare,registered=True,
                    code=code,label=label) as (step,workspace):
                return await _commit_compose(db,case_id,user_id,body,key,digest,revision,inputs,
                    staged=(step,workspace,None))
    except PublicationInputStale as error:
        raise ExecutionConflict('input_stale',str(error)) from error


async def _commit_compose(db,case_id,user_id,body,key,digest,revision,inputs,*,staged):
    from ..services import document_admission, job_execution, step_handlers
    from ..services.case_write import bounded_case_read
    from ..services.quota import check_job_quota
    from ..services.report_input_seal import verify_commit

    case,user=await document_admission.authorize(db,case_id,user_id)
    async with committing(db,case):
        await document_admission.check_access(db,case,user)
        job=await job_execution.lookup_submission(db,case_id,user_id,key,digest)
        if job is not None:
            return job,True
        if case.dict_revision!=revision:
            raise CaseWriteConflict('revision_conflict','caseDict 已被其他操作修改，请刷新后重试',
                                    current_revision=case.dict_revision)
        _compose_target_condition(body,inputs,revision)
        prepared,workspace,observations=staged
        await check_job_quota(db,user,case_id=case.id)
        await verify_commit(db,case,prepared,observations)
        await bounded_case_read(inputs.verify_versions,case.s3_prefix,code='compose_preparation',label='报告草稿准备')
        job,_=await job_execution.submit(db,case_id=case_id,user_id=user_id,key=key,digest=digest,
            job_type=prepared.job_type,command=prepared.spec['command'],spec=prepared.spec,
            input_paths=prepared.input_paths,input_publications=prepared.input_publications,staged_workspace=workspace)
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
        inputs.verify_seal(attempt,prepared)
        await step_handlers.bind(db,case,job,attempt,prepared)
        return job,False


async def _await_composition(case_id,payload,replay):
    from ..services import job_execution
    from ..services.compose_publication import result
    from ..services.artifact_publication import repair_aliases
    from types import SimpleNamespace
    deadline=time.monotonic()+20
    # Do not hold a case transaction while a worker needs to finalize. An HTTP
    # timeout/disconnect leaves the durable Job running and queryable.
    while True:
        proc=jobrunner.get_proc(payload['job_id'])
        async with SessionLocal() as session:
            job=await session.get(Job,payload['job_id'])
            attempt=await session.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
            payload['status']=job_execution.public_status(job,attempt)
            if payload['status'] in TERMINAL and (proc is None or proc.finished):
                if job.status!='succeeded':
                    raise HTTPException(409,{**payload,'code':'composition_failed','message':'报告草稿生成未成功，请查看作业记录'})
                owner=SimpleNamespace(attempt_id=attempt.id)
                break
        if time.monotonic()>=deadline:
            raise HTTPException(504,{**payload,'code':'composition_pending',
                'message':'报告草稿仍在生成，请按 job_id 查询；作业未因等待超时而取消'})
        await asyncio.sleep(.05)
    if proc is None:
        # A different API process can observe commit before the original mirror.
        # Guarded repair is idempotent and never replaces changed destination bytes.
        try:
            await repair_aliases(owner)
        except (ValueError,OSError):
            pass
    async with SessionLocal() as session:
        case=await session.get(Case,case_id)
        async with reading(session,case):
            _,document=await result(session,case,payload['publication_id'])
            if not replay and not document['publication']['applied']:
                raise HTTPException(409,{**payload,'code':'composition_not_applied',
                    'message':'草稿版本已保存，目标文件已变化或已有更新版本；未覆盖当前文件'})
            return document


@router.get('/report/compose-result')
async def compose_result(case_id: int,publication_id: str | None=None,output: str | None=None,
        user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db)):
    from ..services.compose_publication import result
    case=await get_owned_case(case_id,user,db,write=False)
    async with reading(db, case):
        _,document=await result(db,case,publication_id,output)
        return document


@router.get('/report/compositions')
async def compositions(case_id: int,user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db),
        output: str | None=None,before_id: str | None=Query(default=None,max_length=36),
        limit: int=Query(default=50,ge=1,le=100)):
    from ..services.report_history import page
    case=await get_owned_case(case_id,user,db,write=False)
    async with reading(db, case):
        return await page(db,case,selected='draft',output=output,before_id=before_id,limit=limit)


@router.get('/report/derivatives')
async def derivative_history(case_id: int,family: Literal['draft','pdf-number','ppt'] | None=None,
        before_id: str | None=Query(default=None,max_length=36),limit: int=Query(default=50,ge=1,le=100),
        user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db)):
    from ..services.report_history import page
    case=await get_owned_case(case_id,user,db,write=False)
    async with reading(db, case):
        return await page(db,case,selected=family,before_id=before_id,limit=limit)


@router.get('/report/derivative')
async def derivative_result(case_id: int,publication_id: str,
        user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db)):
    from ..services.report_history import result
    case=await get_owned_case(case_id,user,db,write=False)
    async with reading(db, case):
        _,document=await result(db,case,publication_id)
        return document


@router.get('/report/derivative-artifact', response_class=Response, responses=ARTIFACT_CONFLICT_RESPONSE)
@router.head('/report/derivative-artifact', response_class=Response)
async def derivative_artifact(case_id: int,publication_id: str,path: str,request: Request,
        range_header: str | None=Header(default=None,alias='Range'),
        file_version: str | None=Header(default=None,alias='X-Vibeflow-File-Version',pattern=r'^[0-9a-f]{64}$'),
        user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db)):
    case=await get_owned_case(case_id,user,db,write=False)
    return await _download_report_artifact(db,case,publication_id,path,request,range_header,file_version)


async def _download_report_artifact(db,case,publication_id,path,request,range_header,file_version,*,
                                    composition=False,output=None):
    from ..raw_download import begin
    from ..services.async_ops import finish_before_cancel
    from ..services.case_read_capacity import file_read_deadline
    from ..services.case_write import bounded_case_read
    from ..services.file_read import FileReadConflict
    from ..services.report_history import artifact_version

    code,label = ('report_compose_artifact','报告草稿原件读取') if composition else ('report_derivative_artifact','报告原件读取')
    download=begin(request) if request.method == 'GET' else None
    try:
        async with file_read_deadline(code,label):
            async with reading(db,case):
                prefix=case.s3_prefix
                row=await artifact_version(db,case,publication_id,composition=composition,output=output)
            await finish_before_cancel(db.rollback())
            return await bounded_case_read(_report_history_artifact_response,prefix,row,path,range_header,
                file_version,download,composition,output,code=code,label=label)
    except CaseWriteConflict:
        raise
    except (FileReadConflict,OSError,ValueError,KeyError,TypeError) as error:
        raise HTTPException(409,{'code':'evidence_invalid','message':'报告原件缺失或损坏'}) from error


def _report_history_artifact_response(prefix,row,path,range_header,file_version,download,composition,output):
    from ..services.report_history import artifact_reader
    from .files import _verified_publication_response

    response=_verified_publication_response(artifact_reader(prefix,row,path,output=output),path,range_header,file_version,
        download,version_message='报告原件版本不一致')
    filename = Path(path).name
    response.headers['Content-Disposition'] = (f'attachment; filename="{filename}"' if composition else
                                               "attachment; filename*=UTF-8''"+quote(filename))
    return response


@router.get('/report/compose-artifact', response_class=Response, responses=ARTIFACT_CONFLICT_RESPONSE)
@router.head('/report/compose-artifact', response_class=Response)
async def compose_artifact(case_id: int,path: str,request: Request,publication_id: str | None=None,output: str | None=None,
        range_header: str | None=Header(default=None,alias='Range'),
        file_version: str | None=Header(default=None,alias='X-Vibeflow-File-Version',pattern=r'^[0-9a-f]{64}$'),
        user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db)):
    case=await get_owned_case(case_id,user,db,write=False)
    return await _download_report_artifact(db,case,publication_id,path,request,range_header,file_version,
                                           composition=True,output=output)


@router.post("/report:build")
async def build_report(case_id: int, request: Request, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db),
                       idempotency_key: str | None=Header(default=None,alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    return await _registered_build(db,case,user,idempotency_key,request=request)


async def _registered_build(db,case,user,key,*,request=None):
    from ..services import job_execution
    try:
        job_execution.validate_key(key)
        digest=job_execution.request_hash({'handler':'report.v1','mode':'markdown'})
        return await _staged_report_build(db,case,user,key,digest,request)
    except ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except ValueError as error:
        if isinstance(error,CaseWriteConflict):
            raise
        raise HTTPException(400,str(error)) from error


async def _staged_report_build(db,case,user,key,digest,request):
    from ..services import job_execution, report_build_staging, step_handlers
    from ..services.async_ops import finish_before_cancel
    from ..services.case_read_capacity import file_read_deadline
    from ..services.request_lifecycle import wait as wait_for_request
    from ..storage import _use_s3

    job=await _lookup_report_submission(db,case,user,key,digest)
    replay=job is not None
    if job is None:
        case_id,user_id=case.id,user.id
        code,label='report_seal','封存正式报告输入'
        async with file_read_deadline(code,label,seconds=300):
            if _use_s3():
                prefix,revision=await _report_revision(db,case,user_id,code=code,label=label)
                prepared,observations=await wait_for_request(
                    report_build_staging.prepare(case_id,user_id,prefix,revision),request=request)
                job,replay=await wait_for_request(
                    _commit_staged_report(db,case_id,user_id,key,digest,revision,prepared,observations),request=request)
            else:
                db.expunge(case)
                await finish_before_cancel(db.rollback())
                job,replay=await wait_for_request(
                    _stage_local_report(db,case,user_id,key,digest,code=code,label=label),request=request)
    attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
    publication=await db.get(ArtifactPublication,json.loads(attempt.publication_json)['publication_id'])
    payload={'job_id':job.id,'status':job_execution.public_status(job,attempt),'manifest':'E-report/build/report-manifest.json',
             'publication_id':publication.id,'generation':publication.generation}
    await finish_before_cancel(db.rollback())
    if not replay:
        proc=await step_handlers.launch(payload['job_id'])
        payload['status']=proc.public_status
    return payload


async def _stage_local_report(db,case,user_id,key,digest,*,code,label):
    from ..services import document_admission, job_execution, publication_command_submission, report_build_staging, step_handlers
    from ..services.case_provision import io
    from ..services.quota import check_job_quota

    handler=step_handlers.get_handler('report.build')

    async def prepare(session,selected,actor,**options):
        run_dir,dictionary=await io(report_build_staging.configuration,selected.s3_prefix)
        prepared=await handler.prepare(session,selected,actor,
            parameters={'run_dir':run_dir,'mode':'markdown','allow_standalone':True},
            budget={'timeout_seconds':300,'max_log_bytes':16*1024*1024},**options)
        if prepared.admission['guard']['dictionary']!=dictionary:
            raise CaseWriteConflict('input_stale','Report dictionary changed while selecting inputs')
        return prepared

    async with publication_command_submission.stage_step(db,case,user_id,prepare,registered=True,code=code,label=label) as (prepared,workspace):
        selected,actor=await document_admission.authorize(db,case.id,user_id)
        async with committing(db,selected):
            await document_admission.check_access(db,selected,actor)
            replay=await _lookup_report_submission(db,selected,actor,key,digest)
            if replay is not None:
                return replay,True
            await check_job_quota(db,actor,case_id=selected.id)
            job,_=await job_execution.submit(db,case_id=selected.id,user_id=actor.id,key=key,digest=digest,
                job_type=prepared.job_type,command=prepared.spec['command'],spec=prepared.spec,
                input_paths=prepared.input_paths,input_publications=prepared.input_publications,staged_workspace=workspace)
            attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
            await step_handlers.bind(db,selected,job,attempt,prepared)
        return job,False


async def _commit_staged_report(db,case_id,user_id,key,digest,revision,prepared,observations):
    from ..services import command_input_seal, document_admission, job_execution, report_build_staging, step_handlers
    from ..services.async_ops import finish_before_cancel
    from ..services.quota import check_job_quota

    async with command_input_seal.stage_case(case_id,user_id,code='report_seal',label='封存正式报告输入') as (lease,owner_id):
        case,user=await document_admission.authorize(db,case_id,user_id)
        replay=await _lookup_report_submission(db,case,user,key,digest)
        if replay is not None:
            return replay,True
        db.expunge(case)
        await finish_before_cancel(db.rollback())
        workspace=await report_build_staging.upload(lease,owner_id,case,revision,prepared,observations)
        case,user=await document_admission.authorize(db,case_id,user_id)
        async with committing(db,case):
            await document_admission.check_access(db,case,user)
            await check_job_quota(db,user,case_id=case.id)
            await report_build_staging.verify_commit(db,case,prepared,revision,observations)
            job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=key,digest=digest,
                job_type=prepared.job_type,command=prepared.spec['command'],spec=prepared.spec,
                input_paths=prepared.input_paths,input_publications=prepared.input_publications,staged_workspace=workspace)
            attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
            await step_handlers.bind(db,case,job,attempt,prepared)
        return job,False


async def _lookup_report_submission(db,case,user,key,digest):
    """Replay before reading mutable files, including pre-unification request keys."""
    from ..models import JobSubmissionRequest
    from ..services import job_execution
    try:
        return await job_execution.lookup_submission(db,case.id,user.id,key,digest)
    except ExecutionConflict as error:
        if error.code!='idempotency_conflict':
            raise
        request=await db.scalar(select(JobSubmissionRequest).where(
            JobSubmissionRequest.case_id==case.id,JobSubmissionRequest.created_by==user.id,
            JobSubmissionRequest.idempotency_key==key))
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==request.job_id))
        descriptor=json.loads(attempt.publication_json).get('descriptor',{}) if attempt else {}
        if descriptor.get('handler')!='report.v1' or descriptor.get('mode')!='markdown':
            raise
        legacy=job_execution.request_hash({'handler':'report.v1','mode':'markdown','run_dir':descriptor['run_dir']})
        if request.request_hash!=legacy:
            raise
        return await job_execution.lookup_submission(db,case.id,user.id,key,legacy)


@router.get('/report/results')
async def report_results(case_id: int,request: Request,publication_id: str | None=None,
        user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db)):
    from ..services import report_publication

    return await read_result(db, case_id, user, request, report_publication.select_result,
        report_publication.read_result_archive, publication_id, code='report_result', title='报告')


@router.get('/report/publications')
async def report_publications(case_id: int,user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db),
        before_generation: int | None=Query(default=None,ge=1),limit: int=Query(default=50,ge=1,le=100)):
    from ..services.artifact_publication import current
    case=await get_owned_case(case_id,user,db,write=False)
    async with reading(db, case):
        _,active=await current(db,case.id,'report')
        query=select(ArtifactPublication).where(ArtifactPublication.case_id==case.id,ArtifactPublication.kind=='report')
        if before_generation is not None:
            query=query.where(ArtifactPublication.generation<before_generation)
        # Generation is unique within this case/channel. New publications cannot
        # shift an older page, unlike OFFSET pagination. Keep the legacy array.
        rows=await db.scalars(query.order_by(ArtifactPublication.generation.desc()).limit(limit))
        return [{'id':row.id,'generation':row.generation,'status':row.status,'attempt_id':row.attempt_id,
            'current':active is not None and active.id==row.id} for row in rows]


@router.get('/report/artifact')
async def report_artifact(case_id: int,path: str,request: Request,publication_id: str | None=None,
        user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db)):
    from ..raw_download import begin
    from ..services.async_ops import finish_before_cancel
    from ..services.case_read_capacity import file_read_deadline
    from ..services.case_write import bounded_case_read
    from ..services.file_read import FileReadConflict
    from ..services.report_publication import artifact_version

    case=await get_owned_case(case_id,user,db,write=False)
    download=begin(request)
    try:
        async with file_read_deadline('report_artifact','报告原件读取'):
            async with reading(db, case):
                prefix=case.s3_prefix
                row=await artifact_version(db,case,publication_id)
            await finish_before_cancel(db.rollback())
            return await bounded_case_read(_report_artifact_response,prefix,row,path,download,
                code='report_artifact',label='报告原件读取')
    except CaseWriteConflict:
        raise
    except (FileReadConflict,OSError,ValueError,KeyError,TypeError) as error:
        raise HTTPException(409,{'code':'evidence_invalid','message':'报告发布记录或文件损坏'}) from error


def _report_artifact_response(prefix,row,path,download):
    from contextlib import closing
    from starlette.responses import StreamingResponse
    from ..services.report_publication import artifact_reader
    from .files import _stage_verified_ranges

    with artifact_reader(prefix,row,path) as reader:
        size=reader.size
        with closing(reader.chunks()) as chunks:
            _stage_verified_ranges(download,chunks,[(None,None)],size,size)
    mime={'.html':'text/html','.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.md':'text/markdown'}.get(Path(path).suffix,'application/octet-stream')
    return StreamingResponse(download.chunks(),media_type=mime,headers={
        'Content-Length':str(size),'Content-Disposition':'attachment; filename="'+Path(path).name+'"'})


@router.get("/report/outputs")
async def list_outputs(case_id: int, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    from ..storage import list_tree
    return {"outputs": [e for e in list_tree(case.s3_prefix, "E-report/build")]}


class PptBuildIn(BaseModel):
    output: str = "E-report/build/vibeflow.pptx"
    title: str = "VibeFlow"
    template: str | None = None
    slides: list[dict]


@router.get("/ppt/presets")
async def list_ppt_presets(case_id: int, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    from ..services.ppt_builder import ppt_preset_catalog

    await get_owned_case(case_id, user, db, write=False)
    return {"presets": ppt_preset_catalog()}


@router.post("/ppt:build")
async def build_ppt(case_id: int, body: PptBuildIn, user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db),
                    idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    return await _registered_ppt(db,case,user,body,idempotency_key,'ppt.build')


class PptVideoIn(BaseModel):
    input: str = Field(min_length=1,max_length=500)
    output: str = Field(default='E-report/build/narrated.mp4',min_length=1,max_length=500)
    language: Literal['english','chinese'] = 'english'


@router.post('/ppt:video')
async def build_ppt_video(case_id: int, body: PptVideoIn, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db),
                          idempotency_key: str | None = Header(default=None,alias='Idempotency-Key')):
    case = await get_owned_case(case_id,user,db)
    return await _registered_ppt(db,case,user,body,idempotency_key,'ppt.video')


async def _registered_ppt(db,case,user,body,key,handler_key='ppt.build'):
    from ..services import job_execution,step_handlers
    try:
        job_execution.validate_key(key)
        request=body.model_dump()
        digest=job_execution.request_hash({'endpoint':handler_key,'payload':request})
        async with committing(db,case):
            job=await job_execution.lookup_submission(db,case.id,user.id,key,digest)
            replay=job is not None
            if not replay:
                prepared=await step_handlers.get_handler(handler_key).prepare(db,case,user,
                    parameters=request,budget={'timeout_seconds':86400,'max_log_bytes':16*1024*1024})
                job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=key,digest=digest,
                    job_type=prepared.job_type,command=prepared.spec['command'],spec=prepared.spec,
                    input_paths=prepared.input_paths,generated_inputs=prepared.generated_inputs)
                attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
                await step_handlers.bind(db,case,job,attempt,prepared)
        if not replay:
            from ..services.job_delivery import deliver,DeliveryDeferred
            try:await deliver(job.id)
            except DeliveryDeferred:pass
        if handler_key=='ppt.build':
            document=validate_ppt_document(request)
            return await admission.response(db,job,output=document['output'],slides=len(document['slides']))
        from ..services.ppt_media_handler import video_request
        return await admission.response(db,job,output=video_request(request)['output'])
    except ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except FileNotFoundError as error:
        raise HTTPException(404,'PPT 源文件、模板或图片资源不存在') from error
    except ValueError as error:
        if isinstance(error,CaseWriteConflict):raise
        raise HTTPException(400,str(error)) from error


class NumberPdfIn(BaseModel):
    input: str
    output: str | None = None


@router.post("/pdf:number")
async def number_pdf(case_id: int, body: NumberPdfIn, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db),
                     idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    return await _registered_pdf_number(db,case,user,body,idempotency_key)


async def _registered_pdf_number(db,case,user,body,key):
    from ..services import job_execution,step_handlers
    try:
        job_execution.validate_key(key)
        digest=job_execution.request_hash({'endpoint':'pdf.number','payload':body.model_dump()})
        async with committing(db,case):
            job=await job_execution.lookup_submission(db,case.id,user.id,key,digest)
            replay=job is not None
            if not replay:
                prepared=await step_handlers.get_handler('pdf.number').prepare(db,case,user,
                    parameters=body.model_dump(),budget={'timeout_seconds':86400,'max_log_bytes':16*1024*1024})
                job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=key,digest=digest,
                    job_type=prepared.job_type,command=prepared.spec['command'],spec=prepared.spec,input_paths=prepared.input_paths)
                attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
                await step_handlers.bind(db,case,job,attempt,prepared)
        if not replay:
            from ..services.job_delivery import deliver,DeliveryDeferred
            try:await deliver(job.id)
            except DeliveryDeferred:pass
        output=safe_pdf_relative(body.output) if body.output else numbered_pdf_path(safe_pdf_relative(body.input))
        return await admission.response(db,job,output=output)
    except ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except FileNotFoundError as error:
        raise HTTPException(404,'输入 PDF 不存在') from error
    except ValueError as error:
        if isinstance(error,CaseWriteConflict):raise
        raise HTTPException(400,str(error)) from error


class SynctexQueryIn(BaseModel):
    pdf: str
    page: int = Field(gt=0, le=10000)
    x: float = Field(gt=-100000, lt=100000)
    y: float = Field(gt=-100000, lt=100000)
    coordinate_space: Literal['page', 'pdf'] = 'page'
    publication_id: str | None = None
    expected_file_version: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_coordinate_space(self):
        if self.coordinate_space == 'page' and (self.x <= 0 or self.y <= 0):
            raise ValueError('SyncTeX 页面坐标必须大于 0')
        return self


@router.post("/synctex:query")
async def synctex_query(case_id: int, body: SynctexQueryIn, request: Request, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db,write=False)
    user_id=user.id
    try:
        pdf = safe_pdf_relative(body.pdf)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    try:
        from ..services import synctex_queries,document_admission
        from ..services.async_ops import finish_before_cancel
        from ..services.case_read_capacity import file_read_deadline
        from ..services.case_write import bounded_case_read
        from ..services.file_read import FileReadConflict
        from ..services.request_lifecycle import wait
        if body.expected_file_version is not None and body.publication_id is not None:
            raise HTTPException(422,'发布版本与工作区文件版本条件不可混用')
        async with file_read_deadline('synctex_preparation','SyncTeX 输入准备'):
            async with committing(db,case):
                selection=await synctex_queries.select(db,case,pdf,body.publication_id)
            try:
                selected=await wait(bounded_case_read(synctex_queries.prepare,selection,pdf,
                    body.expected_file_version,code='synctex_preparation',label='SyncTeX 输入准备'),request=request)
            except FileReadConflict as error:
                raise CaseWriteConflict('input_stale','SyncTeX 输入在读取时发生变化') from error
            case,user=await document_admission.authorize(db,case_id,user_id,write=False)
            async with reading(db,case):
                await document_admission.check_access(db,case,user,write=False)
                if selection!=await synctex_queries.select(db,case,pdf,body.publication_id):
                    raise CaseWriteConflict('input_stale','SyncTeX 所选输入在准备期间发生变化，请重试')
            await finish_before_cancel(db.rollback())
        output=await wait(synctex_queries.execute(selected,user_id,body.page,body.x,body.y,
            coordinate_space=body.coordinate_space),request=request)
        async with file_read_deadline('synctex_result','SyncTeX 来源核验'):
            case,user=await document_admission.authorize(db,case_id,user_id,write=False)
            async with reading(db,case):
                await document_admission.check_access(db,case,user,write=False)
                result_selection=await synctex_queries.select_result(db,case,selected)
            await finish_before_cancel(db.rollback())
            result=await wait(synctex_queries.result(db,result_selection,selected,output,body.page),request=request)
            await finish_before_cancel(db.rollback())
            case,user=await document_admission.authorize(db,case_id,user_id,write=False)
            async with reading(db,case):
                await document_admission.check_access(db,case,user,write=False)
                if result_selection!=await synctex_queries.select_result(db,case,selected):
                    raise CaseWriteConflict('input_stale','SyncTeX 来源在核验期间发生变化，请重试')
            await finish_before_cancel(db.rollback())
    except FileNotFoundError as error:
        raise HTTPException(409, str(error)) from error
    except CaseWriteConflict as error:
        raise HTTPException(409,error.detail()) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except (TimeoutError,subprocess.TimeoutExpired) as error:
        raise HTTPException(504, "SyncTeX 查询超时") from error
    except OSError as error:
        raise HTTPException(502,'SyncTeX 所属执行未完成：'+type(error).__name__) from error
    except RuntimeError as error:
        raise HTTPException(502, str(error)) from error
    return {**result, "page": body.page}


class LatexTableIn(BaseModel):
    headers: list[str]
    rows: list[list[object]]
    booktabs: bool = True
    borders: bool = False
    align: str | None = None


@router.post("/latex:table")
async def make_latex_table(case_id: int, body: LatexTableIn, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id, user, db)
    try:
        return {"latex": latex_table(body.headers, body.rows, body.booktabs, body.borders, body.align)}
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


# ---------- LaTeX 编译链（E-report/latex/；WSL TeX Live latexmk） ----------
@router.get("/latex/errors")
async def latex_errors(case_id: int, job_id: int, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    """读取属于当前算例的 LaTeX 作业日志，返回可定位源码的诊断。"""
    case = await get_owned_case(case_id, user, db, write=False)
    job = await db.get(Job, job_id)
    if job is None or job.case_id != case.id:
        raise HTTPException(404, "LaTeX 作业不存在")
    if job.type != "convert" or "latexmk" not in job.command:
        raise HTTPException(400, "该作业不是 LaTeX 编译")
    log_key = job.log_s3_key or f"{jobrunner.LOG_DIR_REL}/job-{job.id}.log"
    log = get_bytes(case.s3_prefix, log_key).decode("utf-8", "replace") if exists(case.s3_prefix, log_key) else ""
    proc = jobrunner.get_proc(job.id)
    items=parse_latex_errors(log)
    if job.command=='Registered latexmk':
        from ..services.latex_publication import diagnostic_path
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
        descriptor=json.loads(attempt.publication_json).get('descriptor',{}) if attempt else {}
        if descriptor.get('handler')=='latex.v1':
            for item in items: item['file']=diagnostic_path(item['file'],descriptor['main'],descriptor.get('input_aliases'))
            failure=json.loads(attempt.output_manifest_json).get('publication_error')
            if failure:
                items.append({'severity':'error','message':failure['message'],'file':None,'line':None,
                    'context':'Publication verification: '+failure['type']})
            if job.exit_code==124:
                items.append({'severity':'error','message':'LaTeX compilation timed out','file':None,
                    'line':None,'context':'native_compile_timeout'})
    return {"job_id": job.id, "status": proc.public_status if proc else job.status,"items":items}


class LatexBuildIn(BaseModel):
    main: str = "E-report/latex/main.tex"   # 主文档（算例相对路径）
    recipe: str = "xelatex"                 # xelatex|pdflatex|lualatex
    resources: list[str] = Field(default_factory=list,max_length=64)
    input_aliases: dict[str,str] = Field(default_factory=dict,max_length=32)


@router.post("/latex:build")
async def build_latex(case_id: int, body: LatexBuildIn, request: Request,
                      user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db),
                      idempotency_key: str | None=Header(default=None,alias='Idempotency-Key')):
    from ..services import job_execution, step_handlers, latex_artifacts
    from ..services.async_ops import finish_before_cancel
    from ..services.case_read_capacity import file_read_deadline
    from ..services.request_deadline import STAGING_CLEANUP_SECONDS
    from ..services.request_lifecycle import wait

    case=await get_owned_case(case_id,user,db)
    user_id=user.id
    try:
        main=latex_artifacts.main_path(body.main)
        resources=latex_artifacts.normalize_resources(body.resources)
        aliases=latex_artifacts.normalize_input_aliases(body.input_aliases)
        if body.recipe not in latex_artifacts.RECIPES:
            raise ValueError('Unsupported LaTeX recipe')
        job_execution.validate_key(idempotency_key)
        parameters={'main':main,'recipe':body.recipe,'resources':resources}
        if aliases:
            parameters['input_aliases']=aliases
        digest=job_execution.request_hash({'handler':'latex.v1',**parameters})
        job=await job_execution.lookup_submission(db,case.id,user_id,idempotency_key,digest)
        replay=job is not None
        if not replay:
            db.expunge(case)
            await finish_before_cancel(db.rollback())
            code,label='latex_seal','封存 LaTeX 输入'
            async with file_read_deadline(code,label,seconds=STAGING_CLEANUP_SECONDS):
                job,replay=await wait(_stage_latex(db,case,user_id,idempotency_key,digest,parameters,
                    code=code,label=label),request=request)
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
        publication=await db.get(ArtifactPublication,json.loads(attempt.publication_json)['publication_id'])
        result={'job_id':job.id,'status':job_execution.public_status(job,attempt),'pdf':latex_artifacts.paths(main)['pdf'],
                'publication_id':publication.id,'generation':publication.generation}
        await finish_before_cancel(db.rollback())
        if not replay:
            result['status']=(await step_handlers.launch(result['job_id'])).public_status
        return result
    except FileNotFoundError as error:
        raise HTTPException(404,str(error)) from error
    except ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except ValueError as error:
        if isinstance(error,CaseWriteConflict): raise
        raise HTTPException(400,str(error)) from error


async def _stage_latex(db,case,user_id,key,digest,parameters,*,code,label):
    from functools import partial
    from ..services import document_admission, job_execution, publication_command_submission, step_handlers
    from ..services.async_ops import finish_before_cancel
    from ..services.quota import check_job_quota

    handler=step_handlers.get_handler('latex.build')
    prepare=partial(handler.prepare,parameters=parameters,budget={'timeout_seconds':300,'max_log_bytes':16*1024*1024})
    async with publication_command_submission.stage_step(db,case,user_id,prepare,registered=True,
            code=code,label=label,key=key,digest=digest) as staged:
        if staged is None:
            replay=await job_execution.lookup_submission(db,case.id,user_id,key,digest)
            if replay is not None:
                return replay,True
            await finish_before_cancel(db.rollback())
            async with SessionLocal() as session:
                selected,actor=await document_admission.authorize(session,case.id,user_id)
                prepared=await prepare(session,selected,actor)
            prepared=await document_admission.complete(prepared,case.id,user_id)
            workspace=None
        else:
            prepared,workspace=staged
        selected,actor=await document_admission.authorize(db,case.id,user_id)
        async with committing(db,selected):
            await document_admission.check_access(db,selected,actor)
            replay=await job_execution.lookup_submission(db,selected.id,user_id,key,digest)
            if replay is not None:
                return replay,True
            await check_job_quota(db,actor,case_id=selected.id)
            if workspace is None:
                await document_admission.validate(db,selected,actor,prepared)
            job,_=await job_execution.submit(db,case_id=selected.id,user_id=user_id,key=key,digest=digest,
                job_type=prepared.job_type,command=prepared.spec['command'],spec=prepared.spec,
                input_paths=prepared.input_paths,input_publications=prepared.input_publications,staged_workspace=workspace)
            attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
            await step_handlers.bind(db,selected,job,attempt,prepared)
        return job,False


@router.get('/latex/results')
async def latex_results(case_id: int,request: Request,publication_id: str | None=None,main: str | None=None,
        user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db)):
    from ..services import latex_publication

    return await read_result(db, case_id, user, request, latex_publication.select_result,
        latex_publication.read_result_archive, publication_id, main, code='latex_result', title='LaTeX')


@router.get('/latex/publications')
async def latex_publications(case_id: int,main: str | None=None,
        user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db),
        before_id: str | None=Query(default=None,max_length=36),limit: int=Query(default=50,ge=1,le=100)):
    from ..services.latex_artifacts import channel
    from ..services.artifact_publication import current
    case=await get_owned_case(case_id,user,db,write=False)
    async with reading(db, case):
        try: kind=channel(main) if main is not None else None
        except ValueError as error: raise HTTPException(400,str(error)) from error
        query=select(ArtifactPublication).where(ArtifactPublication.case_id==case.id,
            ArtifactPublication.kind==kind if kind else ArtifactPublication.kind.startswith('latex:'))
        if before_id is not None:
            cursor=await db.get(ArtifactPublication,before_id)
            if cursor is None or cursor.case_id!=case.id or (cursor.kind!=kind if kind else not cursor.kind.startswith('latex:')):
                raise HTTPException(400,'历史游标不属于所选报告范围')
            query=query.where(or_(ArtifactPublication.created_at<cursor.created_at,
                and_(ArtifactPublication.created_at==cursor.created_at,ArtifactPublication.id<cursor.id)))
        rows=await db.scalars(query.order_by(ArtifactPublication.created_at.desc(),ArtifactPublication.id.desc()).limit(limit))
        result=[]
        for row in rows:
            _,active=await current(db,case.id,row.kind)
            attempt=await db.get(JobAttempt,row.attempt_id)
            descriptor=json.loads(attempt.publication_json)['descriptor']
            result.append({'id':row.id,'generation':row.generation,'status':row.status,'attempt_id':row.attempt_id,
                'main':descriptor['main'],'recipe':descriptor['recipe'],'current':active is not None and active.id==row.id})
        return result


@router.get('/latex/artifact', response_class=Response, responses=ARTIFACT_CONFLICT_RESPONSE)
@router.head('/latex/artifact', response_class=Response)
async def latex_artifact(case_id: int,path: str,request: Request,publication_id: str | None=None,main: str | None=None,
        user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db),
        range_header: str | None=Header(default=None,alias='Range'),
        file_version: str | None=Header(default=None,alias='X-Vibeflow-File-Version',pattern=r'^[0-9a-f]{64}$')):
    case = await get_owned_case(case_id,user,db,write=False)
    return await _download_latex_member(db,case,publication_id,main,path,request,range_header,file_version)


async def _download_latex_member(db,case,publication_id,main,path,request,range_header,file_version,*,source_copy=False):
    from ..raw_download import begin
    from ..services.async_ops import finish_before_cancel
    from ..services.case_read_capacity import file_read_deadline
    from ..services.case_write import bounded_case_read
    from ..services.file_read import FileReadConflict
    from ..services.latex_publication import artifact_version

    download = begin(request) if request.method == 'GET' else None
    code,label = ('latex_source','LaTeX 历史源文件读取') if source_copy else ('latex_artifact','LaTeX 原件读取')
    try:
        async with file_read_deadline(code,label):
            async with reading(db, case):
                prefix = case.s3_prefix
                row, main = await artifact_version(db,case,publication_id,main)
            await finish_before_cancel(db.rollback())
            return await bounded_case_read(_latex_member_response,prefix,row,main,path,range_header,
                file_version,download,source_copy,code=code,label=label)
    except CaseWriteConflict:
        raise
    except (FileReadConflict,OSError,ValueError,KeyError,TypeError) as error:
        raise HTTPException(409,{'code':'evidence_invalid','message':'所选发布原件缺失或已损坏'}) from error


def _latex_member_response(prefix,row,main,path,range_header,file_version,download,source_copy=False):
    from ..services.latex_publication import artifact_reader
    from .files import _verified_publication_response

    response = _verified_publication_response(artifact_reader(prefix,row,main,path,source_copy=source_copy),path,range_header,
        file_version,download,version_message='PDF 发布版本不一致，请重新读取')
    if source_copy:
        response.headers["Content-Type"] = "application/octet-stream"
        del response.headers["Accept-Ranges"]
    response.headers['Content-Disposition']="attachment; filename*=UTF-8''"+quote(Path(path).name)
    return response


@router.get('/latex/source')
async def latex_source(case_id: int,path: str,request: Request,publication_id: str | None=None,main: str | None=None,
        user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db)):
    case=await get_owned_case(case_id,user,db,write=False)
    return await _download_latex_member(db,case,publication_id,main,path,request,None,None,source_copy=True)


@router.get("/latex/outputs")
async def list_latex_outputs(case_id: int, main: str | None = None, user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    from ..storage import list_tree
    from ..services import latex_artifacts

    try:
        source = latex_artifacts.main_path(main or 'E-report/latex/main.tex')
    except ValueError as error:
        raise HTTPException(400,str(error)) from error
    folder = latex_artifacts.paths(source)['pdf'].rsplit('/',1)[0]
    return {"outputs": list(list_tree(case.s3_prefix, folder))}
