"""Operator-provided COMSOL batch solver."""
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import native_solver_submission
from ..services.commercial_batch import launcher_status
from ..services.comsol_handler import STUDY_TAG, input_path, launcher
from .workspaces import get_owned_case


router = APIRouter(prefix='/api/v1/cases/{case_id}/comsol', tags=['comsol'])


class ComsolIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input_path: str
    cpus: int = Field(default=1, ge=1, le=16, strict=True)
    study: str | None = Field(default=None, pattern='^' + STUDY_TAG + '$')


@router.get(':environment')
async def environment(case_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id, user, db, write=False)
    return launcher_status(launcher, 'comsol')


@router.post(':solve')
async def solve(case_id: int, body: ComsolIn, request: Request, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db),
                idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    return await native_solver_submission.solve(db, case_id, user.id, idempotency_key, body.model_dump(),
        backend='comsol', label='COMSOL', raw_input=body.input_path, canonical_path=input_path,
        find_executable=launcher, request=request, extra={'cpus': body.cpus, 'study': body.study})
