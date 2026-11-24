"""Owned, immutable serial/MPI/UDF solver results and publication history."""
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import ArtifactPublication, User
from ..services.artifact_publication import current, read_file, read_manifest
from ..services.publication_inputs import resolve
from ..services.solve_artifacts import EVIDENCE, TOOLCHAIN
from ..services.case_read_capacity import reading
from .workspaces import get_owned_case

router = APIRouter(prefix='/api/v1', tags=['solve'])


@router.get('/cases/{case_id}/solve/package')
async def case_package(case_id: int, request: Request, run_dir: str = Query(min_length=1,max_length=512),
                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from starlette.responses import StreamingResponse

    from ..raw_download import begin
    from ..services.case_write import CaseWriteConflict, bounded_case_read
    from ..services.openfoam_case_package import build

    case = await get_owned_case(case_id,user,db,write=False)
    download = begin(request)
    try:
        async with reading(db,case):
            size = await bounded_case_read(build,case.s3_prefix,run_dir,download,
                code='openfoam_package',label='标准算例导出')
    except CaseWriteConflict:
        raise
    except (OSError,ValueError) as error:
        raise HTTPException(409,{'code':'invalid_source','message':str(error)}) from error
    return StreamingResponse(download.chunks(),media_type='application/zip',headers={
        'Content-Length':str(size),'Content-Disposition':f'attachment; filename="openfoam-case-{case_id}.zip"'})


@router.get('/cases/{case_id}/solve/results')
async def results(case_id: int, publication_id: str | None = None,
                  user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    async with reading(db, case):
        try:
            _, active = await current(db, case.id, 'solve')
            row = await db.get(ArtifactPublication, publication_id) if publication_id else active
            if row is None or row.case_id != case.id or row.kind != 'solve':
                raise HTTPException(404, '求解发布不存在')
            await resolve(db, case, [{'publication_id':row.id, 'paths':None, 'require_current':False}])
            try:
                await resolve(db, case, [{'publication_id':row.id, 'paths':None, 'require_current':True}])
                stale = False
            except (ValueError, OSError, KeyError):
                stale = True
            _, manifest = read_manifest(case.s3_prefix, row)
            run_dir = manifest['descriptor']['run_dir']
            descriptor=manifest['descriptor']
            native_evidence={'workflow-mpi.v1':'mpi-evidence.json','workflow-udf.v1':'udf-evidence.json'}
            if descriptor.get('handler') in native_evidence:
                result={'evidence':json.loads(read_file(case.s3_prefix,row,run_dir+'/'+native_evidence[descriptor['handler']])),
                    'toolchain':descriptor['native']['receipt']['native']}
            else:
                result = {name:json.loads(read_file(case.s3_prefix, row, run_dir+'/'+path))
                    for name,path in (('evidence',EVIDENCE), ('toolchain',TOOLCHAIN))}
            if 'initial_source' in descriptor:
                result['initial_source'] = descriptor['initial_source']
            result['publication'] = {'id':row.id, 'generation':row.generation, 'status':row.status,
                'current':active is not None and active.id == row.id, 'stale':stale}
        except (ValueError, OSError, KeyError, TypeError) as error:
            raise HTTPException(409, {'code':'evidence_invalid', 'message':'求解发布记录或文件损坏'}) from error
    return result


@router.get('/cases/{case_id}/solve/publications')
async def publications(case_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    async with reading(db, case):
        _, active = await current(db, case.id, 'solve')
        rows = await db.scalars(select(ArtifactPublication).where(ArtifactPublication.case_id == case.id,
            ArtifactPublication.kind == 'solve').order_by(ArtifactPublication.generation.desc()).limit(50))
        return [{'id':row.id, 'generation':row.generation, 'status':row.status, 'attempt_id':row.attempt_id,
            'current':active is not None and active.id == row.id} for row in rows]
