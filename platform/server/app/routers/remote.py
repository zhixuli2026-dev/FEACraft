"""C16：SSH executionProfile 远程命令执行。"""
import asyncssh
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import Membership, User
from ..services.remote_exec import run_remote

router = APIRouter(prefix="/api/v1/remote", tags=["remote"])


class RemoteIn(BaseModel):
    host: str
    port: int = 22
    username: str
    workdir: str = "."
    client_key: str
    known_hosts: str
    command: str
    timeout_seconds: float = 60


@router.post(":execute")
async def execute(body: RemoteIn, user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    admin = await db.scalar(select(Membership.id).where(
        Membership.user_id == user.id, Membership.role == "admin").limit(1))
    if admin is None:
        raise HTTPException(403, "仅组织 admin 可使用 executionProfile")
    try:
        return await run_remote(body.model_dump(), body.command)
    except ValueError as error:
        raise HTTPException(400, str(error))
    except (asyncssh.Error, OSError, TimeoutError) as error:
        raise HTTPException(502, f"远程 SSH 执行失败: {type(error).__name__}") from error
