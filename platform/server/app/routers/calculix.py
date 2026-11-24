"""CalculiX ccx 真实求解作业。"""
from __future__ import annotations

import hashlib
from functools import partial

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import ArtifactPublication, Job, JobAttempt, User
from ..services import artifact_publication, calculix_modal, native_solver_submission
from ..services.calculix import calculix_executable
from ..services.calculix_artifacts import input_path, publication_channel
from ..services.calculix_pressure_transfer import PressureTransferRequest
from ..services.case_read_capacity import reading
from ..services.case_write import CaseWriteConflict, bounded_case_read
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1/cases/{case_id}/calculix", tags=["calculix"])


class CalculixIn(BaseModel):
    input_path: str
    pressure_transfer: PressureTransferRequest | None = None


@router.post(":solve")
async def solve(case_id: int, body: CalculixIn, request: Request, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db),
                idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    payload = body.model_dump(mode='json',exclude_none=True)
    extra = {'pressure_transfer':payload['pressure_transfer']} if body.pressure_transfer is not None else None
    return await native_solver_submission.solve(db,case_id,user.id,idempotency_key,payload,
        backend='calculix',label='CalculiX',raw_input=body.input_path,canonical_path=input_path,
        find_executable=calculix_executable,request=request,extra=extra)


def _read_results(prefix, publication, extract):
    _, manifest = artifact_publication.read_manifest(prefix, publication)
    source = manifest['descriptor']['input']
    if publication_channel(source) != publication.kind:
        raise ValueError('CalculiX publication input differs from its channel')
    path = source[:-4] + '.dat'
    data = artifact_publication.read_file(prefix, publication, path)
    return extract(data), path, hashlib.sha256(data).hexdigest()


async def _result(case_id, user, db, native_input, publication_id, extract, label):
    case = await get_owned_case(case_id, user, db, write=False)
    try:
        channel = publication_channel(native_input)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    async with reading(db, case):
        try:
            _, active = await artifact_publication.current(db, case.id, channel)
            row = await db.get(ArtifactPublication, publication_id) if publication_id else active
            if row is None or row.case_id != case.id or row.kind != channel:
                raise HTTPException(404, 'CalculiX 求解发布不存在')
            job = await db.scalar(select(Job).join(JobAttempt, Job.id == JobAttempt.job_id)
                                  .where(JobAttempt.id == row.attempt_id))
            if row.status not in {'committed','superseded'} or job is None or job.case_id != case.id or job.status != 'succeeded':
                raise ValueError('CalculiX publication has no successful owned job')
            steps,path,digest = await bounded_case_read(_read_results,case.s3_prefix,row,extract,
                                                       code='calculix_results',label=label)
        except CaseWriteConflict as error:
            raise HTTPException(409,{'code':error.code,'message':str(error)}) from error
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(409,{'code':'evidence_invalid','message':label+'无法核验'}) from error
        if not steps:
            raise HTTPException(404, '此发布没有所选'+label)
        return {'publication':{'id':row.id,'generation':row.generation,'current':active is not None and row.id == active.id},
                'source':{'path':path,'sha256':digest},'frequency_unit':'cycles per model time unit','steps':steps}


@router.get('/modal-results')
async def modal_results(case_id: int, input_path: str = Query(min_length=1,max_length=125),
                        publication_id: str | None = None, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    result = await _result(case_id,user,db,input_path,publication_id,calculix_modal.read_modes,'CalculiX 模态结果')
    return {**result,'angular_frequency_unit':'radians per model time unit'}


@router.get('/harmonic-results')
async def harmonic_results(case_id: int, input_path: str = Query(min_length=1,max_length=125),
                           node_set: str = Query(min_length=1,max_length=80), node: int = Query(ge=1),
                           publication_id: str | None = None, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    extract = partial(calculix_modal.read_harmonics,node_set=node_set,node=node)
    result = await _result(case_id,user,db,input_path,publication_id,extract,'CalculiX 谐响应')
    return {**result,'node_set':node_set,'node':node,'length_unit':'model length unit'}
