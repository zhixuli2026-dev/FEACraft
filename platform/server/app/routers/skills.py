"""技能注册表与用户/组织技能包路由。"""
import asyncio
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from ..upload_routes import UploadCleanupRoute
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import Membership, User
from ..services.skills import get_skill, list_skills
from ..services.skill_store import install_skill_zip, package_skill, write_skill,write_peak_bytes,install_peak_bytes
from ..services.async_ops import finish_before_cancel
from ..services.skill_capacity import writing as skill_capacity
from ..services.case_write import CaseWriteConflict

router = APIRouter(prefix="/api/v1/skills", tags=["skills"],route_class=UploadCleanupRoute)


def _org_id(db: AsyncSession, user_id: int) -> int | None:
    return db.scalar(select(Membership.org_id).where(Membership.user_id == user_id).limit(1))


async def _org_ids(db: AsyncSession, user_id: int) -> list[int]:
    rows = await db.scalars(select(Membership.org_id).where(Membership.user_id == user_id))
    return list(rows)


async def _scope_org(db: AsyncSession, user: User, scope: str) -> int | None:
    if scope == "user":
        return None
    if scope != "org":
        raise HTTPException(400, "scope 只能是 user/org")
    org_id = await _org_id(db, user.id)
    if org_id is None:
        raise HTTPException(404, "用户没有组织")
    role = await db.scalar(select(Membership.role).where(
        Membership.org_id == org_id, Membership.user_id == user.id))
    if role != "admin":
        raise HTTPException(404, "组织技能需要 admin 权限")
    return org_id


@router.get("")
async def list_all(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user_id = user.id
    org_ids = await _org_ids(db, user_id)
    await db.rollback()
    return {"skills": await finish_before_cancel(asyncio.to_thread(list_skills, user_id, org_ids))}


@router.get("/{name}")
async def get_one(name: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user_id = user.id
    org_ids = await _org_ids(db, user_id)
    await db.rollback()
    skill = await finish_before_cancel(asyncio.to_thread(get_skill, name, user_id, org_ids))
    if skill is None:
        raise HTTPException(404, f"技能 {name} 不存在")
    return skill


class SkillIn(BaseModel):
    name: str
    description: str
    body: str
    scope: str = "user"
    overwrite: bool = False
    display_name: str | None = None
    short_description: str | None = None
    default_prompt: str | None = None


@router.post("")
async def create_or_update(body: SkillIn, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    org_id = await _scope_org(db, user, body.scope)
    try:
        arguments=(body.scope,user.id,org_id,body.name,body.description,body.body)
        options={'interface':{key:value for key,value in {
            'display_name':body.display_name,'short_description':body.short_description,
            'default_prompt':body.default_prompt}.items() if value is not None},'overwrite':body.overwrite}
        incoming=await finish_before_cancel(asyncio.to_thread(write_peak_bytes,*arguments,**options))
        async with skill_capacity(db,user,body.scope,incoming):
            target=await finish_before_cancel(asyncio.to_thread(write_skill,*arguments,**options))
    except FileExistsError:
        raise HTTPException(409, "技能已存在；overwrite=true 才允许覆盖")
    except CaseWriteConflict:
        raise
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True, "name": body.name, "scope": body.scope, "path": str(target)}


@router.post("/{name}/package")
async def package(name: str, scope: str = "user", user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    org_id = await _scope_org(db, user, scope)
    try:
        content = await finish_before_cancel(asyncio.to_thread(package_skill, scope, user.id, org_id, name))
    except FileNotFoundError:
        raise HTTPException(404, "技能不存在") from None
    except CaseWriteConflict:
        raise
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return Response(content, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


@router.post("/install")
async def install(file: UploadFile = File(...), scope: str = Form("user"),
                  overwrite: bool = Form(False), user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    org_id = await _scope_org(db, user, scope)
    data = await file.read(2 * 1024 * 1024 + 1)
    try:
        incoming=await finish_before_cancel(asyncio.to_thread(install_peak_bytes,data))
        async with skill_capacity(db,user,scope,incoming):
            target = await finish_before_cancel(asyncio.to_thread(install_skill_zip, data, scope, user.id, org_id, overwrite))
    except FileExistsError:
        raise HTTPException(409, "技能已存在；overwrite=true 才允许覆盖")
    except CaseWriteConflict:
        raise
    except (ValueError, KeyError) as error:
        raise HTTPException(400, f"技能包无效: {error}") from error
    return {"ok": True, "scope": scope, "name": target.name}
