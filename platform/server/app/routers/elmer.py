"""Submit a case's Elmer finite-element model."""
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import native_solver_submission
from ..services.elmer import elmer_executable
from ..services.elmer_artifacts import input_path


router = APIRouter(prefix='/api/v1/cases/{case_id}/elmer',tags=['elmer'])


class ElmerIn(BaseModel):
    input_path: str


@router.post(':solve')
async def solve(case_id: int, body: ElmerIn, request: Request, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db),
                idempotency_key: str | None = Header(default=None,alias='Idempotency-Key')):
    return await native_solver_submission.solve(db,case_id,user.id,idempotency_key,body.model_dump(),
        backend='elmer',label='Elmer',raw_input=body.input_path,canonical_path=input_path,
        find_executable=elmer_executable,request=request)
