"""MPI 编排：decomposePar → mpirun → reconstructPar 与结果状态。"""
from decimal import Decimal
import re
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import legacy_job_admission as admission
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1", tags=["mpi"])
_SOLVER = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*$")
_TIME = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,4})?")
_PROCESSOR = re.compile(r"processor[0-9]+")
_DECOMPOSE_HEADER = """FoamFile
{
    version     2.0;
    format      ascii;
    class       dictionary;
    location    \"system\";
    object      decomposeParDict;
}
"""


class MpiIn(BaseModel):
    subdomains: int = Field(default=2, ge=1, le=64)
    run_dir: str = "C-run/case-1"
    solver: str = "icoFoam"
    force: bool | None = Field(default=None, strict=True)


def _run_dir(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/").lstrip("/")).as_posix()
    if ".." in path.split("/"):
        raise HTTPException(400, "非法求解目录")
    return path


@router.post("/cases/{case_id}/mpi:run")
async def run_mpi(case_id: int, body: MpiIn, request: Request,
                  user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                  idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    if not _SOLVER.fullmatch(body.solver):
        raise HTTPException(400, "只允许 OpenFOAM 求解器标识")
    case = await get_owned_case(case_id, user, db)
    return await _registered_mpi(db,case,user,body,idempotency_key,request)


async def _registered_mpi(db,case,user,body,key,request):
    from ..services import registered_input_seal,step_handlers
    from ..services.case_provision import io
    from ..services.case_write import CaseWriteConflict
    from ..services.workflow_state import ExecutionConflict
    from ..services.mpi_handler import run_path
    try:
        run_dir=run_path(body.run_dir)

        async def prepare(session,selected,actor):
            if not await io(storage.exists,selected.s3_prefix,run_dir+'/system/controlDict'):
                raise HTTPException(400,'Invalid OpenFOAM run directory')
            parameters = {'run_dir': run_dir, 'solver': body.solver, 'subdomains': body.subdomains}
            if body.force is not None:
                parameters['force'] = body.force
            return await step_handlers.get_handler('mpi.openfoam').prepare(session,selected,actor,
                parameters=parameters,
                budget={'timeout_seconds':86400,'max_log_bytes':16*1024*1024})

        job,created=await registered_input_seal.submit(db,case,user,key,'mpi.run',body.model_dump(exclude_none=True),prepare,
                                                       request=request)
        if created:
            from ..services.job_delivery import deliver,DeliveryDeferred
            try:await deliver(job.id)
            except DeliveryDeferred:pass
        return await admission.response(db,job,subdomains=body.subdomains,run_dir=run_dir,solver=body.solver)
    except ExecutionConflict as error:raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except FileNotFoundError as error:raise HTTPException(404,'MPI input does not exist: '+str(error)) from error
    except ValueError as error:
        if isinstance(error,CaseWriteConflict):raise
        raise HTTPException(400,str(error)) from error


@router.get("/cases/{case_id}/mpi:status")
async def mpi_status(case_id: int, run_dir: str = "C-run/case-1",
                     user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from .files import _read_file_tree

    case = await get_owned_case(case_id, user, db, write=False)
    run_dir = _run_dir(run_dir)
    result, _ = await _read_file_tree(db, case, user, _status_inventory, run_dir, code='mpi_status', label='MPI 状态读取')
    return result


def _status_inventory(prefix, run_dir):
    from ..services.storage_listing_budget import ListingBudget

    root = PurePosixPath(run_dir)
    times = set()
    reconstructed = set()
    processors = set()
    for row in storage.list_tree(prefix, run_dir):
        parts = PurePosixPath(row['path']).relative_to(root).parts
        if len(parts) < 2:
            continue
        parent = parts[0]
        if _PROCESSOR.fullmatch(parent):
            processors.add(parent)
        if _TIME.fullmatch(parent) and Decimal(parent) != 0:
            times.add(parent)
            if parts[1:] == ('U',):
                reconstructed.add(parent)
    if not storage._use_s3():
        base = storage._fs_path(prefix, run_dir)
        if base.is_dir():
            budget = ListingBudget()
            for child in base.iterdir():
                budget.visit()
                if child.is_dir() and _TIME.fullmatch(child.name) and Decimal(child.name) != 0:
                    times.add(child.name)
    return {'run_dir': run_dir, 'subdomains': len(processors),
            'processor_dirs': sorted(processors, key=lambda name: int(name[len('processor'):])),
            'times': sorted(times, key=lambda name: (Decimal(name), name)),
            'complete': bool(times) and times == reconstructed}
