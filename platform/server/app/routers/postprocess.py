"""WP06 后处理作业与最近一次有效结果读取。"""
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import re
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import bearer, get_current_user
from ..models import ArtifactPublication,ArtifactPublicationHead,Job,JobSubmissionRequest,User
from ..services import file_transactions, job_execution, legacy_job_admission as admission
from ..services.case_write import coordinate, read_file_record
from ..services.case_read_capacity import admitted, reading, reading_before_action
from ..services.postprocess_observations import fetch_noaa, fetch_wind_tunnel, ObservationSourceError
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1", tags=["postprocess"])


class PostprocessIn(BaseModel):
    run_dir: str | None = None


class ObservationImportIn(BaseModel):
    model_config = ConfigDict(extra='forbid')

    station: str = Field(pattern=r'^[0-9]{7}$')
    begin: date
    end: date
    product: Literal['hourly_height', 'predictions'] = 'hourly_height'
    datum: Literal['MLLW', 'MHHW', 'MSL', 'NAVD'] = 'MLLW'
    units: Literal['metric', 'english'] = 'metric'


class WindTunnelImportIn(BaseModel):
    model_config = ConfigDict(extra='forbid')

    section: Literal[-4, 1, 4, 6, 10]


@router.post('/cases/{case_id}/postprocess/observations:import')
async def import_observation(case_id: int, body: ObservationImportIn, request: Request,
                             cred: HTTPAuthorizationCredentials | None = Depends(bearer),
                             user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id, user, db)
    await db.commit()  # The network transfer owns neither a DB transaction nor a case write lock.
    try:
        data, receipt = await fetch_noaa(**body.model_dump())
    except ObservationSourceError as error:
        raise HTTPException(502, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return await _publish_observation(case_id, request, cred, db, receipt,
                                      {receipt['path']: (data, 'text/csv; charset=utf-8')})


@router.post('/cases/{case_id}/postprocess/observations/wind-tunnel:import')
async def import_wind_tunnel(case_id: int, body: WindTunnelImportIn, request: Request,
                             cred: HTTPAuthorizationCredentials | None = Depends(bearer),
                             user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id, user, db)
    await db.commit()
    try:
        files, receipt = await fetch_wind_tunnel(body.section)
    except ObservationSourceError as error:
        raise HTTPException(502, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return await _publish_observation(case_id,request,cred,db,receipt,files)


async def _publish_observation(case_id, request, cred, db, receipt, members):
    from ..services.quota import check_storage_quota

    db.expire_all()
    user = await get_current_user(request, cred, db)
    case = await get_owned_case(case_id, user, db)
    receipt_bytes = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    files = {name: item[0] for name, item in members.items()}
    files[receipt['receipt']] = receipt_bytes
    content_types = {name: item[1] for name, item in members.items()}
    content_types[receipt['receipt']] = 'application/json'
    expected = {name: {'size': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
                for name, value in files.items()}
    async with admitted(db, case) as owner:
        async with coordinate(db, case):
            existing = {name: await read_file_record(case.s3_prefix, name) for name in files}
            replay = existing == expected
            if not replay:
                if any(record is not None for record in existing.values()):
                    raise HTTPException(409, '原始观测文件或回执已被修改，请先恢复该版本')
                await check_storage_quota(db, owner, sum(item['size'] for item in expected.values()))
                await file_transactions.write(db, case, files, expected_files=dict.fromkeys(files),
                    content_types=content_types)
        await db.commit()
        if not replay:
            await file_transactions.finish_committed_edit(db, case, owner.id)
    return {**receipt, 'replayed': replay}


@router.post("/cases/{case_id}/postprocess:run")
async def run_postprocess(case_id: int, body: PostprocessIn, request: Request,
                          user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                          idempotency_key: str | None=Header(default=None,alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    try:
        job_execution.validate_key(idempotency_key)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    async with reading_before_action(db, case):
        # A completed legacy request stays replayable after this case adopts
        # registered solve publications. Do not reroute it into a new handler.
        prior = await db.scalar(select(JobSubmissionRequest).where(
            JobSubmissionRequest.case_id == case.id, JobSubmissionRequest.created_by == user.id,
            JobSubmissionRequest.idempotency_key == idempotency_key)) if idempotency_key else None
        if prior is not None and prior.request_hash == job_execution.request_hash(
                {'endpoint': 'postprocess.run', 'payload': body.model_dump()}):
            return await admission.response(db, await db.get(Job, prior.job_id))
        registered = await db.get(ArtifactPublicationHead,(case.id,'solve')) is not None
    from ..services import postprocess_submission, step_handlers
    from ..services.workflow_state import ExecutionConflict

    try:
        job,publication=await postprocess_submission.submit(db,case,user,idempotency_key,body.model_dump(),
                                                            registered=registered,request=request)
        if registered:
            if publication is None:
                return {'job_id':job.id,'status':job.status}
            proc=await step_handlers.launch(job.id)
            return {'job_id':job.id,'status':proc.status,'publication_id':publication.id,'generation':publication.generation}
        if publication is not None:
            from ..services.job_delivery import deliver,DeliveryDeferred
            try:await deliver(job.id)
            except DeliveryDeferred:pass
        return await admission.response(db,job)
    except ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except (ValueError,OSError) as error:
        from ..services.case_write import CaseWriteConflict
        if isinstance(error,CaseWriteConflict) or (registered and not isinstance(error,ValueError)):
            raise
        raise HTTPException(400,str(error)) from error

@router.get("/cases/{case_id}/postprocess/results")
async def postprocess_results(case_id: int, request: Request, job_id: int | None = None,
                              publication_id: str | None = None,
                              user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from ..services.postprocess_publication import select_result, read_result_body
    from ..services.publication_result_reads import read_result

    return await read_result(db, case_id, user, request, select_result, read_result_body,
        publication_id, job_id, code='postprocess_result', title='后处理')


@router.get('/cases/{case_id}/postprocess/artifact')
@router.head('/cases/{case_id}/postprocess/artifact')
async def chart_artifact(case_id: int, request: Request, path: str = Query(min_length=1, max_length=256),
                         publication_id: str | None = None,
                         byte_range: str | None = Header(default=None, alias='Range'),
                         version: str | None = Header(default=None, alias='X-Vibeflow-File-Version', pattern=r'^[0-9a-f]{64}$'),
                         user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from ..raw_download import begin
    from ..services.async_ops import finish_before_cancel
    from ..services.case_read_capacity import file_read_deadline
    from ..services.case_write import CaseWriteConflict, bounded_case_read
    from ..services.file_read import FileReadConflict
    from ..services.postprocess_publication import select_result
    from ..services.publication_result_reads import Selection

    case = await get_owned_case(case_id, user, db, write=False)
    code, label = 'postprocess_artifact', '后处理图件读取'
    download = begin(request) if request.method == 'GET' else None
    try:
        async with file_read_deadline(code, label):
            async with reading(db, case):
                selected = await select_result(db, case, publication_id, None)
                if not isinstance(selected, Selection):
                    raise HTTPException(404, '后处理发布不存在')
            await finish_before_cancel(db.rollback())
            response = await bounded_case_read(_chart_response, selected, path, byte_range, version, download,
                                               code=code, label=label)
    except CaseWriteConflict:
        raise
    except (FileReadConflict, OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, {'code':'evidence_invalid', 'message':'所选后处理原件无法核验'}) from error
    response.headers['Content-Disposition'] = "attachment; filename*=UTF-8''" + quote(path.rsplit('/', 1)[-1], safe='')
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def _chart_response(selection, path, byte_range, version, download):
    from .files import _verified_publication_response

    return _verified_publication_response(_chart_member(selection, path), path, byte_range, version, download,
                                          version_message='图件下载版本不匹配，请重新核对')


@contextmanager
def _chart_member(selection, path):
    from ..services.file_read import ArchivedPublication
    from ..services.postprocess_artifacts import result_dir

    directory = result_dir(selection.job_id)
    name = path.removeprefix(directory + '/')
    if name == path or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}\.(?:pdf|svg|png|plot\.csv|vtu)', name):
        raise HTTPException(404, '所选后处理版本不包含此图件')
    with ArchivedPublication(selection.prefix, selection.publication) as archive:
        if path not in archive.manifest['files']:
            raise HTTPException(404, '所选后处理版本不包含此图件')
        yield archive.member(path)


@router.get('/cases/{case_id}/postprocess/publications')
async def publications(case_id: int,user: User=Depends(get_current_user),db: AsyncSession=Depends(get_db)):
    from ..services.artifact_publication import current
    case=await get_owned_case(case_id,user,db,write=False)
    async with reading(db, case):
        _,active=await current(db,case.id,'postprocess')
        rows=await db.scalars(select(ArtifactPublication).where(ArtifactPublication.case_id==case.id,
            ArtifactPublication.kind=='postprocess').order_by(ArtifactPublication.generation.desc()).limit(50))
        return [{'id':row.id,'generation':row.generation,'status':row.status,'attempt_id':row.attempt_id,
            'current':active is not None and active.id==row.id} for row in rows]
