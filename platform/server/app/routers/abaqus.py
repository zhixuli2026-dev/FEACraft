"""Operator-provided Abaqus batch solver and runtime availability."""
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import native_solver_submission
from ..services.abaqus_handler import input_path, launcher
from ..services.commercial_batch import launcher_status
from .workspaces import get_owned_case


router = APIRouter(prefix='/api/v1/cases/{case_id}/abaqus', tags=['abaqus'])


class AbaqusIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input_path: str
    cpus: int = Field(default=1, ge=1, le=16, strict=True)


@router.get(':environment')
async def environment(case_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id, user, db, write=False)
    return launcher_status(launcher, 'abaqus')


@router.post(':solve')
async def solve(case_id: int, body: AbaqusIn, request: Request, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db),
                idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    return await native_solver_submission.solve(db, case_id, user.id, idempotency_key, body.model_dump(),
        backend='abaqus', label='Abaqus', raw_input=body.input_path, canonical_path=input_path,
        find_executable=launcher, request=request, extra={'cpus': body.cpus})
