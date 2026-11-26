"""Operator-provided Altair HyperMesh batch export."""
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import native_solver_submission
from ..services.commercial_batch import launcher_status
from ..services.hypermesh_handler import input_path, launcher
from .workspaces import get_owned_case


router = APIRouter(prefix='/api/v1/cases/{case_id}/hypermesh', tags=['hypermesh'])


class HypermeshIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input_path: str
    format: Literal['optistruct', 'abaqus'] = 'optistruct'


@router.get(':environment')
async def environment(case_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id, user, db, write=False)
    return launcher_status(launcher, 'hypermesh')


@router.post(':export')
async def export(case_id: int, body: HypermeshIn, request: Request, user: User = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db),
                 idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    return await native_solver_submission.submit(db, case_id, user.id, idempotency_key, body.model_dump(),
        backend='hypermesh', label='HyperMesh', raw_input=body.input_path, canonical_path=input_path,
        find_executable=launcher, request=request, extra={'format': body.format}, operation='export')
