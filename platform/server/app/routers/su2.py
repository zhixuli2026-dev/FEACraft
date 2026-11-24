"""SU2_CFD 真实求解作业。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import native_solver_submission
from ..services.su2 import su2_executable
from ..services.su2_artifacts import input_path

router = APIRouter(prefix="/api/v1/cases/{case_id}/su2", tags=["su2"])


class Su2In(BaseModel):
    config_path: str


@router.post(":solve")
async def solve(case_id: int, body: Su2In, request: Request, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db),
                idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    return await native_solver_submission.solve(db,case_id,user.id,idempotency_key,body.model_dump(),
        backend='su2',label='SU2',raw_input=body.config_path,canonical_path=input_path,
        find_executable=su2_executable,request=request)
