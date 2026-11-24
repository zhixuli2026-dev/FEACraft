"""Submit a case's Code_Aster command study."""
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import native_solver_submission
from ..services.code_aster import code_aster_executable
from ..services.code_aster_artifacts import input_path


router = APIRouter(prefix='/api/v1/cases/{case_id}/code-aster',tags=['code-aster'])


class CodeAsterRestart(BaseModel):
    publication_id: str
    checkpoint_path: str


class CodeAsterInput(BaseModel):
    input_path: str
    restart: CodeAsterRestart | None = None


@router.post(':solve')
async def solve(case_id: int, body: CodeAsterInput, request: Request, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db),
                idempotency_key: str | None = Header(default=None,alias='Idempotency-Key')):
    return await native_solver_submission.solve(db,case_id,user.id,idempotency_key,body.model_dump(exclude_none=True),
        backend='code-aster',label='Code_Aster',raw_input=body.input_path,canonical_path=input_path,
        find_executable=code_aster_executable,request=request,
        extra={'restart':body.restart.model_dump()} if body.restart is not None else None)
