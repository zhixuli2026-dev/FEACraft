"""网格质量评估端点：checkMesh.log → B-mesh/quality.json。"""
from datetime import datetime, timezone
from pathlib import PurePosixPath
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..db import get_db
from ..deps import get_current_user
from ..models import ArtifactPublication, User
from ..services.casedict import parse_casedict
from ..services.case_evidence import case_input_fingerprint, file_record, run_inventory, safe_run_dir
import hashlib
from ..services.mesh_quality import evaluate_mesh_quality, gate_decision, mesh_block_hash, QUALITY_REPORT
from ..services.mesh_presets import effective_targets, selected as quality_preset
from ..services import file_transactions, owner_storage_budget
from ..services.async_ops import finish_before_cancel
from ..services.case_write import coordinate
from ..services.case_read_capacity import reading
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1", tags=["mesh"])


class EvaluateIn(BaseModel):
    log_path: str = "C-run/case-1/checkMesh.log"
    run_dir: str = "C-run/case-1"


class IndependenceIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    postprocess_publication_ids: list[UUID] = Field(min_length=3, max_length=8)
    metric: str = Field(min_length=1, max_length=160, strict=True)
    absolute_tolerance: float = Field(ge=0, allow_inf_nan=False, strict=True)
    relative_tolerance: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)


@router.post("/cases/{case_id}/mesh:evaluate")
async def evaluate(case_id: int, body: EvaluateIn,
                   user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    async with owner_storage_budget.hold_case(db, case):
        async with coordinate(db, case):
            from ..services.artifact_publication import current, read_file
            from ..services.mesh_publication import gate_reason
            head,published=await current(db,case.id,'mesh')
            if head is not None:
                reason=await gate_reason(db,case)
                if reason:
                    raise HTTPException(409,{'code':'evidence_invalid','message':reason})
                result=json.loads(read_file(case.s3_prefix,published,QUALITY_REPORT))
                if (('log_path' in body.model_fields_set and body.log_path.replace('\\','/')!=result['log_path']) or
                        ('run_dir' in body.model_fields_set and body.run_dir.replace('\\','/')!=result['run_dir'])):
                    raise HTTPException(409,{'code':'evidence_invalid','message':'请求与当前网格发布不匹配'})
            else:
                result=_evaluate_locked(case,body)
                await file_transactions.write(db,case,{QUALITY_REPORT:json.dumps(result,ensure_ascii=False).encode('utf8')},
                                              content_types={QUALITY_REPORT:'application/json'})
        await finish_before_cancel(db.commit())
        await finish_before_cancel(file_transactions.finish_committed(db, case))
    return result


def _evaluate_locked(case, body):
    path = PurePosixPath(body.log_path.replace("\\", "/").lstrip("/")).as_posix()
    if ".." in path.split("/"):
        raise HTTPException(400, "非法日志路径")
    if not storage.exists(case.s3_prefix, path):
        raise HTTPException(404, f"checkMesh 日志不存在: {path}")
    log_bytes = storage.get_bytes(case.s3_prefix, path)
    log_text = log_bytes.decode("utf-8", "replace")
    try:
        run_dir = safe_run_dir(body.run_dir)
    except ValueError as error:
        raise HTTPException(400, str(error))
    case_dict = storage.get_bytes(case.s3_prefix, "0-caseDict/caseDict").decode("utf-8")
    parsed = parse_casedict(case_dict)
    mesh = parsed.get("mesh", {})
    targets = effective_targets(mesh)
    report = evaluate_mesh_quality(log_text, targets)
    report['gate']=gate_decision(report,targets)
    preset=quality_preset(mesh) if isinstance(mesh,dict) else None
    if preset is not None:
        report['quality_preset']=preset
    report.update({
        "case_dict_mesh_hash": mesh_block_hash(mesh),
        "quality_targets": targets,
        "run_dir": run_dir,
        "case_input_sha256": case_input_fingerprint(case_dict),
        "log_sha256": hashlib.sha256(log_bytes).hexdigest(),
        "log_path": path,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    if not storage._use_s3():
        root = storage._fs_path(case.s3_prefix, ".")
        report["inputs"] = [file_record(root, item)
                            for item in run_inventory(root, run_dir, results=False)]
    return report


@router.get('/cases/{case_id}/mesh/results')
async def results(case_id: int, publication_id: str | None=None,
                  user: User=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    from ..services.artifact_publication import current, read_file
    from ..services.publication_inputs import resolve
    from ..services.mesh_artifacts import ARTIFACTS, QUALITY, BOUNDARIES, TOOLCHAIN
    case=await get_owned_case(case_id,user,db,write=False)
    async with reading(db, case):
        try:
            _,active=await current(db,case.id,'mesh')
            row=await db.get(ArtifactPublication,publication_id) if publication_id else active
            if row is None or row.case_id!=case.id or row.kind!='mesh':
                raise HTTPException(404,'网格发布不存在')
            archived = (await resolve(db,case,[{'publication_id':row.id,'paths':None,'require_current':False}]))[0]
            try:
                await resolve(db,case,[{'publication_id':row.id,'paths':None,'require_current':True}])
                stale=False
            except (ValueError,OSError,KeyError):
                stale=True
            result={name:json.loads(read_file(case.s3_prefix,row,path)) for name,path in
                (('quality',QUALITY),('boundaries',BOUNDARIES),('toolchain',TOOLCHAIN))}
            if ARTIFACTS in archived.files:
                result['artifacts']=json.loads(read_file(case.s3_prefix,row,ARTIFACTS))
            result['publication']={'id':row.id,'generation':row.generation,'status':row.status,
                'current':active is not None and active.id==row.id,'stale':stale}
        except (ValueError,OSError,KeyError,TypeError) as error:
            raise HTTPException(409,{'code':'evidence_invalid','message':'网格发布记录或文件损坏'}) from error
    return result


@router.get('/cases/{case_id}/mesh/publications')
async def publications(case_id: int, user: User=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    from ..services.artifact_publication import current
    case=await get_owned_case(case_id,user,db,write=False)
    async with reading(db, case):
        _,active=await current(db,case.id,'mesh')
        rows=await db.scalars(select(ArtifactPublication).where(ArtifactPublication.case_id==case.id,
            ArtifactPublication.kind=='mesh').order_by(ArtifactPublication.generation.desc()).limit(50))
        return [{'id':row.id,'generation':row.generation,'status':row.status,'attempt_id':row.attempt_id,
            'current':active is not None and active.id==row.id} for row in rows]


@router.post('/cases/{case_id}/mesh/independence')
async def independence(case_id:int,body:IndependenceIn,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    from ..services.case_write import CaseWriteConflict
    from ..services.mesh_study import MeshStudyInput, compare
    case=await get_owned_case(case_id,user,db,write=False)
    async with reading(db,case):
        try:
            return await compare(db,case,[str(value) for value in body.postprocess_publication_ids],body.metric,
                                 body.absolute_tolerance,body.relative_tolerance)
        except CaseWriteConflict:
            raise
        except MeshStudyInput as error:
            raise HTTPException(409,{'code':'invalid_study','message':str(error)}) from error
        except (OSError,ValueError,KeyError,TypeError,AttributeError) as error:
            raise HTTPException(409,{'code':'evidence_invalid','message':'网格比较的发布证据无法核验'}) from error
