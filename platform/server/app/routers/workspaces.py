"""工作区与算例路由"""
import asyncio
import json
from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from ..upload_routes import UploadCleanupRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..services.case_read_capacity import reading
from ..db import get_db
from ..deps import get_current_user
from ..models import Case, User, Workspace
from ..services.casedict import SCAFFOLD_FILES
from ..services.case_admin import (CaseAdminError, copy_case, copy_workspace, move_case,
                                   rename_case, rename_workspace, validate_name)
from ..services.case_copy_io import admit as admit_copy, wait_for_admission
from ..services.case_import import CaseImportError, import_case_zip
from ..services.case_write import coordinate, CaseWriteConflict
from ..services.workspace_write import coordinate_workspaces
from ..services import sharing

router = APIRouter(prefix="/api/v1", tags=["workspaces"], route_class=UploadCleanupRoute)


# ---------- workspaces ----------
@router.get("/workspaces")
async def list_workspaces(user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(Workspace))).all()
    from ..services.trash import is_case_deleted, is_workspace_deleted
    roles = await sharing.memberships(db,user.id)
    result = []
    for workspace in rows:
        if is_workspace_deleted(workspace.id):
            continue
        role = await sharing.access_role(db, workspace, user.id, member_roles=roles)
        case_only = role is None and any(not is_case_deleted(case_id)
            for case_id in await sharing.case_grant_ids(db,workspace,roles))
        if case_only:
            role = 'viewer'
        if role is not None:
            result.append({"id": workspace.id, "name": workspace.name,
                           "created_at": workspace.created_at.isoformat(),
                           "shared": role != "owner", "access_role": role, "case_only":case_only})
    return result


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _creation_name(value: str) -> str:
    try:
        return validate_name(value)
    except CaseAdminError as error:
        raise HTTPException(error.status_code, str(error)) from error


@router.post("/workspaces")
async def create_workspace(body: WorkspaceIn, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    w = Workspace(user_id=user.id, name=_creation_name(body.name))
    db.add(w)
    await db.commit()
    return {"id": w.id, "name": w.name, "created_at": w.created_at.isoformat(),
            "shared": False, "access_role": "owner"}


async def _owned_workspace_record(ws_id: int, user_id: int, db: AsyncSession) -> Workspace:
    w = await db.get(Workspace, ws_id)
    if w is None or w.user_id != user_id:
        raise HTTPException(404, "工作区不存在")
    return w


async def _owned_workspace(ws_id: int, user_id: int, db: AsyncSession) -> Workspace:
    workspace = await _owned_workspace_record(ws_id, user_id, db)
    from ..services.trash import is_workspace_deleted
    if is_workspace_deleted(ws_id):
        raise HTTPException(404, "工作区不存在")
    return workspace


async def _accessible_workspace(ws_id: int, user: User, db: AsyncSession,
                                write: bool = False) -> Workspace:
    workspace = await db.get(Workspace, ws_id)
    if workspace is None:
        raise HTTPException(404, "工作区不存在")
    role = await sharing.access_role(db, workspace, user.id, write=write)
    if role is None:
        raise HTTPException(404, "工作区不存在")
    from ..services.trash import is_workspace_deleted
    if is_workspace_deleted(ws_id):
        raise HTTPException(404, "工作区不存在")
    return workspace


class ShareIn(BaseModel):
    org_id: int = Field(gt=0,strict=True)
    role: Literal['viewer','member'] = "viewer"
    enabled: bool = True


@router.get("/workspaces/{ws_id}/sharing")
async def get_workspace_sharing(ws_id: int, user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    workspace = await _owned_workspace(ws_id, user.id, db)
    return await sharing.legacy_status(db,workspace) or {
        'schema':'vibeflow.workspace-share.v1','workspace_id':ws_id,'enabled':False}


@router.put("/workspaces/{ws_id}/sharing")
async def set_workspace_sharing(ws_id: int, body: ShareIn,
                                user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    workspace = await _owned_workspace(ws_id, user.id, db)
    async with coordinate_workspaces(db,[ws_id]):
        await _owned_workspace(ws_id, user.id, db)
        await sharing.update_grant(db,workspace,None,user,body.org_id,body.role,body.enabled,legacy=True)
        value = await sharing.legacy_status(db,workspace)
    await db.commit()
    return value


class GrantIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['viewer','member']
    enabled: bool
    delegated: bool | None = None
    expected_version: int | None = Field(default=None,ge=0,strict=True)


async def _grant_target(db, *, ws_id=None, case_id=None):
    from ..services.trash import is_case_deleted, is_workspace_deleted
    case = None
    if case_id is not None:
        case = await db.get(Case,case_id,populate_existing=True)
        if case is None or is_case_deleted(case_id):
            raise HTTPException(404,'算例不存在')
        ws_id = case.workspace_id
    workspace = await db.get(Workspace,ws_id,populate_existing=True)
    if workspace is None or is_workspace_deleted(ws_id):
        raise HTTPException(404,'工作区不存在')
    return workspace,case


async def _grant_view(db,user,**target):
    workspace,case = await _grant_target(db,**target)
    result = await sharing.view_grants(db,workspace,case,user.id)
    if not result['can_manage']:
        raise HTTPException(404,'授权不存在')
    return result


async def _grant_write(db,user,org_id,body,**target):
    workspace,_ = await _grant_target(db,**target)
    original = workspace.id
    async with coordinate_workspaces(db,[original]):
        workspace,case = await _grant_target(db,**target)
        if workspace.id!=original:
            raise HTTPException(409,'算例归属已变化')
        async with AsyncExitStack() as locks:
            if case is not None:
                await locks.enter_async_context(coordinate(db,case,content_write=False))
            result = await sharing.update_grant(db,workspace,case,user,org_id,body.role,body.enabled,
                delegated=body.delegated,expected=body.expected_version)
    await db.commit()
    return result


@router.get('/workspaces/{ws_id}/access-grants')
async def workspace_grants(ws_id:int,user:User=Depends(get_current_user),db=Depends(get_db)):
    return await _grant_view(db,user,ws_id=ws_id)


@router.put('/workspaces/{ws_id}/access-grants/{org_id}')
async def put_workspace_grant(ws_id:int,org_id:int,body:GrantIn,user:User=Depends(get_current_user),db=Depends(get_db)):
    return await _grant_write(db,user,org_id,body,ws_id=ws_id)


@router.get('/cases/{case_id}/access-grants')
async def case_grants(case_id:int,user:User=Depends(get_current_user),db=Depends(get_db)):
    return await _grant_view(db,user,case_id=case_id)


@router.put('/cases/{case_id}/access-grants/{org_id}')
async def put_case_grant(case_id:int,org_id:int,body:GrantIn,user:User=Depends(get_current_user),db=Depends(get_db)):
    return await _grant_write(db,user,org_id,body,case_id=case_id)


@router.get("/workspaces/{ws_id}/backup")
async def backup_workspace_route(ws_id: int, request: Request, user: User = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    workspace = await _owned_workspace(ws_id, user.id, db)
    from ..services.backup import BackupError, create_backup
    try:
        download = request.state.backup_download
        length = await create_backup(db, workspace, download)
    except (BackupError, CaseAdminError) as error:
        raise HTTPException(409, str(error))
    from starlette.responses import StreamingResponse
    return StreamingResponse(download.chunks(), media_type="application/zip", headers={
        "Content-Length":str(length),
        "Content-Disposition": f'attachment; filename="workspace-{ws_id}-backup.zip"'})


@router.post("/workspaces:restore")
async def restore_workspace_route(file: UploadFile, user: User = Depends(get_current_user),
                                  db: AsyncSession = Depends(get_db)):
    from ..services.backup import MAX_BACKUP_BYTES, BackupError, restore_backup
    from ..services.backup_manifest import BackupTooLarge
    if file.size is not None and file.size > MAX_BACKUP_BYTES:
        raise HTTPException(413, "备份超过 100 MiB")
    try:
        return await restore_backup(db, user, file.file)
    except BackupTooLarge as error:
        raise HTTPException(413, "备份超过 100 MiB") from error
    except BackupError as error:
        raise HTTPException(400, str(error))


# ---------- cases ----------
@router.get("/workspaces/{ws_id}/cases")
async def list_cases(ws_id: int, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    workspace, _ = await _grant_target(db,ws_id=ws_id)
    from ..services.trash import is_case_deleted
    allowed = await sharing.visible_case_ids(db,workspace,user.id)
    if allowed is not None:
        allowed = {case_id for case_id in allowed if not is_case_deleted(case_id)}
        if not allowed:
            raise HTTPException(404,'工作区不存在')
    statement = select(Case).where(Case.workspace_id==ws_id)
    if allowed is not None:
        statement = statement.where(Case.id.in_(allowed))
    rows = (await db.scalars(statement)).all()
    return [{
        "id": c.id, "name": c.name, "workflow_state": c.workflow_state,
        "dict_revision": c.dict_revision, "updated_at": c.updated_at.isoformat(),
    } for c in rows if not is_case_deleted(c.id)]


@router.get("/workspaces/{ws_id}/search")
async def search_workspace_route(ws_id: int, request: Request, q: str = Query(..., min_length=1, max_length=120),
                                 content: bool = Query(True), limit: int = Query(30, ge=1, le=100),
                                 user: User = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    from ..services import indexsvc
    from ..services.async_ops import finish_before_cancel
    from ..services.case_read_capacity import file_read_deadline
    from ..services.case_write import bounded_case_read
    from ..services.request_lifecycle import wait
    from ..services.trash import is_case_deleted

    code, label = 'workspace_search', '工作区搜索'
    async with file_read_deadline(code, label):
        workspace,_ = await _grant_target(db,ws_id=ws_id)
        allowed = await sharing.visible_case_ids(db,workspace,user.id)
        if allowed is not None:
            allowed = {case_id for case_id in allowed if not is_case_deleted(case_id)}
            if not allowed:
                raise HTTPException(404,'工作区不存在')
        query = q.strip()
        if not query:
            raise HTTPException(400, "空查询")
        user_id = user.id
        candidates = select(Case.id).where(Case.workspace_id == ws_id).order_by(Case.id)
        if allowed is not None:
            candidates = candidates.where(Case.id.in_(allowed))
        ids = [case_id for case_id in await db.scalars(candidates) if not is_case_deleted(case_id)]
        refs, prefixes = [], {}
        await finish_before_cancel(db.rollback())
        try:
            for case_id in ids:
                try:
                    case = await get_owned_case(case_id,SimpleNamespace(id=user_id),db,write=False)
                except HTTPException:
                    case = None
                selected = None
                if case is not None:
                    async with reading(db, case):
                        if case.workspace_id == ws_id:
                            selected = case.s3_prefix, case.name
                # FS retains a business transaction after the recovery context.
                # Complete it before accessing the separate index database.
                await finish_before_cancel(db.rollback())
                if selected is None:
                    continue
                prefix, name = selected
                await wait(bounded_case_read(indexsvc.reconcile, case_id, prefix, code=code, label=label), request=request)
                refs.append((case_id, name))
                prefixes[case_id] = prefix
            result = await wait(bounded_case_read(indexsvc.search_workspace, refs, query, limit, content,
                                                 code=code, label=label), request=request)
        except indexsvc.IndexSourceChanged as error:
            raise HTTPException(409, {'code':'index_source_changed','message':str(error)}) from error

        user = await db.get(User, user_id)
        if user is None or user.status != 'active':
            raise HTTPException(401, "用户不可用")
        workspace,_ = await _grant_target(db,ws_id=ws_id)
        allowed = await sharing.visible_case_ids(db,workspace,user.id)
        if allowed is not None and (not allowed or not set(prefixes).issubset(allowed)):
            raise HTTPException(404,'算例访问权限已变化')
        current = await db.execute(select(Case.id, Case.s3_prefix).where(Case.workspace_id == ws_id))
        current_prefixes = dict(current.all())
        if any(current_prefixes.get(case_id) != prefix or is_case_deleted(case_id)
               for case_id, prefix in prefixes.items()):
            raise HTTPException(409, {'code':'index_source_changed','message':'工作区算例已变化，请重试'})
        return result


class CaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@router.post("/workspaces/{ws_id}/cases")
async def create_case(body: CaseIn, ws_id: int, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    """创建算例：写入 StandardCase 脚手架到 S3（文件为唯一事实源）"""
    name = _creation_name(body.name)
    user_id = user.id
    await _owned_workspace(ws_id, user_id, db)
    from ..config import get_settings
    from ..services.owner_storage_budget import waiting
    from ..services.request_deadline import scope
    from ..services.storage_admission import busy
    async with AsyncExitStack() as capacity:
        # The first selection is read-only. Wait for the existing storage I/O
        # capacity before taking a database connection or any owner/workspace write lock.
        await db.commit()
        try:
            async with scope(get_settings().storage_measurement_timeout_seconds):
                await capacity.enter_async_context(wait_for_admission())
                await capacity.enter_async_context(waiting(db, user_id))
        except TimeoutError as error:
            raise busy() from error
        from ..services import case_provision
        from ..services.case_events import CASE_EVENTS_PATH, build_case_event
        from ..services.casedict import CASE_DICT_PATH, parse_casedict, set_block
        from ..services.provenance import PROVENANCE_PATH, prepare_case_revision
        try:
            await case_provision.recover_owner(db, user_id, wait=True)
            async with coordinate_workspaces(db, [ws_id]):
                await _owned_workspace(ws_id, user_id, db)
                dup = await db.scalar(select(Case).where(Case.workspace_id == ws_id, Case.name == name))
                if dup:
                    raise HTTPException(409, "同名算例已存在")
                case = Case(workspace_id=ws_id, name=name, s3_prefix="")
                db.add(case)
                await db.flush()
                files = {path: content.encode('utf8') for path, content in SCAFFOLD_FILES.items()}
                event = build_case_event(case, 'case.created', 'api')
                files[CASE_EVENTS_PATH] = (json.dumps(event, ensure_ascii=False) + '\n').encode('utf8')
                timestamp = json.dumps(event['created_at'])
                dictionary = set_block(SCAFFOLD_FILES[CASE_DICT_PATH], 'provenance', {
                    'createdBy': str(user_id), 'createdAt': timestamp, 'updatedAt': timestamp,
                    'sources': {'scaffold': {'kind': 'StandardCase'}}, 'assumptions': {}, 'decisions': {},
                    'artifactTrace': {'registry': 'casePublications'},
                })
                files[CASE_DICT_PATH] = dictionary.encode('utf-8')
                _, files[PROVENANCE_PATH] = prepare_case_revision(case, parse_casedict(dictionary), 'case-created',
                    case.workflow_state, files[CASE_DICT_PATH], existing=b'', actor_id=user_id)
                await case_provision.create_files(db, case, user_id, files)
            await db.commit()
        except BaseException:
            # A missing/unknown commit is resolved from the durable plan under the
            # owner lock; never recursively clear a destination based on an exception.
            await db.rollback()
            raise
        return {"id": case.id, "name": case.name, "workflow_state": case.workflow_state,
                "dict_revision": case.dict_revision, "updated_at": case.updated_at.isoformat()}


@router.post('/case-provisions:recover')
async def recover_case_provisions(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from ..services.owner_storage_budget import hold as owner_capacity
    async with owner_capacity(db, user.id):
        from ..services.case_provision import recover_owner
        results = await recover_owner(db, user.id)
        await db.commit()
        return {'results': results, 'recovery_required': sum(row['status'] == 'recovery_required' for row in results)}


@router.get('/case-provisions:maintenance')
async def diagnose_case_storage(cursor: str | None = Query(None, max_length=6000),
                                 limit: int = Query(128, ge=1, le=500),
                                 user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from ..services.case_provision_diagnostics import diagnose_owner
    return await diagnose_owner(db, user.id, cursor=cursor, limit=limit)


@router.post('/case-provisions:maintenance')
async def maintain_case_storage(after_case_id: int = Query(0, ge=0, le=2**63-1),
                                through_case_id: int | None = Query(None, ge=0, le=2**63-1),
                                user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from ..services.storage_maintenance import maintain_owner
    if (through_case_id is None and after_case_id) or (through_case_id is not None and through_case_id < after_case_id):
        raise HTTPException(422, '算例扫描游标无效')
    return await maintain_owner(db, user.id, after_case_id=after_case_id, through_case_id=through_case_id)


@router.post("/workspaces/{ws_id}/cases:import")
async def import_case(ws_id: int, file: UploadFile = File(...), name: str | None = Form(None),
                      user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    workspace = await _owned_workspace(ws_id, user.id, db)
    filename = (file.filename or "imported-case.zip").replace("\\", "/").split("/")[-1]
    default_name = filename[:-4] if filename.lower().endswith(".zip") else filename
    try:
        case, file_count, total_bytes = await import_case_zip(
            db, workspace, user, name or default_name, file)
    except CaseImportError as error:
        raise HTTPException(error.status_code, str(error)) from error
    return {"id": case.id, "name": case.name, "workflow_state": case.workflow_state,
            "dict_revision": case.dict_revision, "updated_at": case.updated_at.isoformat(),
            "files": file_count, "bytes": total_bytes}


@router.get('/cases/{case_id}/attempt-preparations')
async def diagnose_attempt_preparations(case_id: int, cursor: str | None = Query(None, max_length=6000),
                                        limit: int | None = Query(None, ge=1, le=32),
                                        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _seal_preparation_page(case_id, user, db, cursor, limit, apply=False)


@router.post('/cases/{case_id}/attempt-preparations:recover')
async def recover_attempt_preparations(case_id: int, cursor: str | None = Query(None, max_length=6000),
                                       limit: int | None = Query(None, ge=1, le=32),
                                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _seal_preparation_page(case_id, user, db, cursor, limit, apply=True)


async def _seal_preparation_page(case_id, user, db, cursor, limit, *, apply):
    from ..services import attempt_seal_maintenance as seals
    from ..services.async_ops import finish_before_cancel
    from ..services.case_read_capacity import admitted, file_read_deadline
    from ..services.case_write import bounded_case_read
    from ..services.storage_maintenance import SEAL_PAGE_SIZE

    case = await get_owned_case(case_id, user, db)
    user_id = user.id
    code = 'attempt_preparations'
    label = '封存恢复' if apply else '封存诊断'
    async with file_read_deadline(code, label):
        scope = seals.binding(case, user_id, apply=apply)
        selections, successor = await bounded_case_read(seals.select_page, case.s3_prefix, scope, cursor,
            SEAL_PAGE_SIZE if limit is None else limit, apply, code=code, label=label)
        if apply:
            async with admitted(db, case):
                async with coordinate(db, case, content_write=False):
                    await get_owned_case(case_id, user, db)
                    if seals.binding(case, user_id, apply=True) != scope:
                        raise HTTPException(409, '封存存储位置已变化，请重试')
                    result = await seals.execute(db, case, selections, apply=True)
                await finish_before_cancel(db.commit())
        else:
            await db.refresh(case)
            await get_owned_case(case_id, user, db)
            if seals.binding(case, user_id, apply=False) != scope:
                raise HTTPException(409, '封存存储位置已变化，请重试')
            result = await seals.execute(db, case, selections, apply=False)
    if cursor is not None or limit is not None or successor is not None:
        result.update(complete=cursor is None and successor is None, next_cursor=successor)
    return result


async def _owned_case_record(case_id: int, user: User, db: AsyncSession) -> Case:
    c = await db.get(Case, case_id)
    if c is None:
        raise HTTPException(404, "算例不存在")
    w = await db.get(Workspace, c.workspace_id)
    if w is None or w.user_id != user.id:
        raise HTTPException(404, "算例不存在")
    return c


async def _case_access(case_id: int, user: User, db: AsyncSession,
                       write: bool = True) -> tuple[Case,str]:
    case = await db.get(Case, case_id)
    if case is None:
        raise HTTPException(404, "算例不存在")
    workspace = await db.get(Workspace, case.workspace_id)
    if workspace is None:
        raise HTTPException(404, "算例不存在")
    role = await sharing.case_access_role(db, workspace, case, user.id, write=write)
    if role is None:
        raise HTTPException(404, "算例不存在")
    from ..services.trash import is_case_deleted, is_workspace_deleted
    if is_case_deleted(case_id) or is_workspace_deleted(case.workspace_id):
        raise HTTPException(404, "算例不存在")
    return case,role


async def get_owned_case(case_id: int, user: User, db: AsyncSession,
                         write: bool = True) -> Case:
    case,_ = await _case_access(case_id,user,db,write)
    return case


@router.get("/cases/{case_id}")
async def get_case(case_id: int, user: User = Depends(get_current_user),
                   db: AsyncSession = Depends(get_db)):
    c,role = await _case_access(case_id, user, db, write=False)
    return {
        "id": c.id, "name": c.name, "workspace_id": c.workspace_id, "storage_prefix": c.s3_prefix,
        "access_role": role,
        "workflow_state": c.workflow_state, "dict_revision": c.dict_revision,
        "case_meta": c.case_meta, "parse_status": c.parse_status,
        "created_at": c.created_at.isoformat(), "updated_at": c.updated_at.isoformat(),
    }


@router.get("/trash")
async def list_trash(user: User = Depends(get_current_user)):
    from ..services.trash import list_deleted
    return await asyncio.to_thread(list_deleted, user.id)


async def _wait_for_trash_write(db: AsyncSession):
    from ..services.trash import wait_for_mutation

    # Release the authorization reads and their connection before waiting on the inventory.
    await db.commit()
    await wait_for_mutation()


@router.delete("/cases/{case_id}")
async def delete_case_route(case_id: int, user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    await _owned_case_record(case_id, user, db)
    case = await get_owned_case(case_id, user, db)
    from ..services.case_admin import CaseAdminError, assert_no_case_activity
    from ..services.trash import TrashError, delete_case, mutate
    workspace_id = case.workspace_id
    try:
        await _wait_for_trash_write(db)
        async with coordinate_workspaces(db, [workspace_id]):
            async with coordinate(db, case):
                if case.workspace_id != workspace_id:
                    raise CaseWriteConflict("workspace_conflict", "算例归属已变化，请刷新后重试")
                await assert_no_case_activity(db, [case.id])
                entry = await mutate(delete_case, case, user.id)
        await db.commit()
    except (CaseAdminError, TrashError) as error:
        raise HTTPException(error.status_code, str(error)) from error
    return {"ok": True, "trashed": True, "id": case.id, "deleted_at": entry["deleted_at"]}


@router.post("/cases/{case_id}:restore")
async def restore_case_route(case_id: int, user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    case = await _owned_case_record(case_id, user, db)
    from ..services.trash import TrashError, restore_case, mutate
    from ..services.case_admin import assert_no_case_activity
    workspace_id = case.workspace_id
    try:
        await _wait_for_trash_write(db)
        async with coordinate_workspaces(db, [workspace_id]):
            async with coordinate(db, case, allow_deleted=True):
                if case.workspace_id != workspace_id:
                    raise CaseWriteConflict("workspace_conflict", "算例归属已变化，请刷新后重试")
                await assert_no_case_activity(db, [case.id])
                await mutate(restore_case, case, user.id)
        await db.commit()
    except (TrashError, CaseAdminError) as error:
        raise HTTPException(error.status_code, str(error)) from error
    return {"ok": True, "restored": True, "id": case.id}


@router.delete("/workspaces/{ws_id}")
async def delete_workspace_route(ws_id: int, user: User = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    workspace = await _owned_workspace(ws_id, user.id, db)
    from ..services.case_admin import CaseAdminError, assert_no_case_activity
    from ..services.trash import TrashError, delete_workspace, mutate
    try:
        await _wait_for_trash_write(db)
        async with coordinate_workspaces(db, [ws_id]):
            await db.refresh(workspace)
            cases = (await db.scalars(select(Case).where(Case.workspace_id == ws_id).order_by(Case.id))).all()
            async with AsyncExitStack() as locks:
                for case in cases:
                    await locks.enter_async_context(coordinate(db, case))
                await assert_no_case_activity(db, [case.id for case in cases])
                entry = await mutate(delete_workspace, workspace, cases, user.id)
        await db.commit()
    except (CaseAdminError, TrashError) as error:
        raise HTTPException(error.status_code, str(error)) from error
    return {"ok": True, "trashed": True, "id": ws_id, "deleted_at": entry["deleted_at"]}


@router.post("/workspaces/{ws_id}:restore")
async def restore_workspace_route(ws_id: int, user: User = Depends(get_current_user),
                                  db: AsyncSession = Depends(get_db)):
    workspace = await _owned_workspace_record(ws_id, user.id, db)
    from ..services.trash import TrashError, restore_workspace, mutate
    from ..services.case_admin import assert_no_case_activity
    try:
        await _wait_for_trash_write(db)
        async with coordinate_workspaces(db, [ws_id], allow_deleted=True):
            await db.refresh(workspace)
            cases = (await db.scalars(select(Case).where(Case.workspace_id == ws_id).order_by(Case.id))).all()
            async with AsyncExitStack() as locks:
                for case in cases:
                    await locks.enter_async_context(coordinate(db, case, allow_deleted=True))
                await assert_no_case_activity(db, [case.id for case in cases])
                await mutate(restore_workspace, workspace, user.id, cases)
        await db.commit()
    except (TrashError, CaseAdminError) as error:
        raise HTTPException(error.status_code, str(error)) from error
    return {"ok": True, "restored": True, "id": ws_id}


@router.get("/cases/{case_id}/events")
async def get_case_events(case_id: int, offset: int = 0, limit: int = 100,
                          user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    if offset < 0 or limit < 1 or limit > 200:
        raise HTTPException(400, "分页参数无效")
    case = await get_owned_case(case_id, user, db, write=False)
    from ..services.async_ops import finish_before_cancel
    from ..services.case_events import list_case_events, select_case_events
    from ..services.case_read_capacity import file_read_deadline
    from ..services.case_write import bounded_case_read
    from ..services.file_read import FileReadConflict

    code, label = "case_events", "算例历史读取"
    try:
        async with file_read_deadline(code, label):
            async with reading(db, case):
                prefix = case.s3_prefix
                reader = await bounded_case_read(select_case_events, prefix, code=code, label=label)
            await finish_before_cancel(db.rollback())
            return await bounded_case_read(list_case_events, prefix, reader, offset, limit, code=code, label=label)
    except FileReadConflict as error:
        raise HTTPException(409, {"code": "file_changed_during_read",
                                  "message": "算例历史在读取期间发生变化，请重新读取"}) from error


@router.get("/cases/{case_id}/provenance")
async def get_case_provenance(case_id: int, offset: int = 0, limit: int = 100,
                              revision: int | None = Query(default=None, ge=0),
                              sha256: str | None = Query(default=None, pattern=r'^[0-9a-f]{64}$'),
                              user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    if offset < 0 or limit < 1 or limit > 200:
        raise HTTPException(400, "分页参数无效")
    case = await get_owned_case(case_id, user, db, write=False)
    if (revision is None) != (sha256 is None):
        raise HTTPException(400, "查询指定修订时须同时提供修订号和 SHA-256")
    match = None if revision is None else (revision, sha256)
    from ..services.async_ops import finish_before_cancel
    from ..services.case_read_capacity import file_read_deadline
    from ..services.case_write import bounded_case_read
    from ..services.file_read import FileReadConflict
    from ..services.provenance import provenance_status, select_provenance

    code, label = "case_provenance", "算例修订读取"
    try:
        async with file_read_deadline(code, label):
            async with reading(db, case):
                prefix, current_revision = case.s3_prefix, case.dict_revision
                readers = await bounded_case_read(select_provenance, prefix, code=code, label=label)
            await finish_before_cancel(db.rollback())
            return await bounded_case_read(provenance_status, prefix, current_revision, readers, offset, limit, match,
                                           code=code, label=label)
    except FileReadConflict as error:
        raise HTTPException(409, {"code": "file_changed_during_read",
                                  "message": "算例修订在读取期间发生变化，请重新读取"}) from error


@router.get('/cases/{case_id}/provenance/artifacts')
async def list_case_artifact_trace(case_id: int, before_id: UUID | None = None,
                                   limit: int = Query(default=24, ge=1, le=100),
                                   user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id, user, db, write=False)
    from ..services.case_artifact_trace import page
    return await page(db, case_id, str(before_id) if before_id is not None else None, limit)


@router.get('/cases/{case_id}/provenance/artifacts/{publication_id}')
async def get_case_artifact_trace(case_id: int, publication_id: UUID,
                                  user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    from ..services.case_artifact_trace import detail
    return await detail(db, case, str(publication_id))


@router.get('/cases/{case_id}/provenance/artifacts/{publication_id}/file')
@router.head('/cases/{case_id}/provenance/artifacts/{publication_id}/file')
async def get_native_artifact_file(case_id: int, publication_id: UUID, request: Request,
                                   path: str = Query(min_length=1, max_length=1024),
                                   byte_range: str | None = Header(default=None, alias='Range'),
                                   version: str | None = Header(default=None, alias='X-Vibeflow-File-Version', pattern=r'^[0-9a-f]{64}$'),
                                   user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from contextlib import contextmanager
    from functools import partial
    from ..raw_download import begin
    from ..services.async_ops import finish_before_cancel
    from ..services.case_artifact_trace import native_file_identity
    from ..services.case_read_capacity import file_read_deadline
    from ..services.case_write import bounded_case_read
    from ..services.file_read import ArchivedPublication, FileReadConflict
    from .files import _verified_publication_response

    @contextmanager
    def member(prefix, identity):
        with ArchivedPublication(prefix, identity) as archive:
            if path not in archive.manifest['files']:
                raise HTTPException(404, '所选产物发布不包含此文件')
            yield archive.member(path)

    case = await get_owned_case(case_id, user, db, write=False)
    download = begin(request) if request.method == 'GET' else None
    code, label = 'native_artifact', '工程产物原件读取'
    try:
        async with file_read_deadline(code, label):
            selected = await native_file_identity(db, case.id, str(publication_id))
            prefix = case.s3_prefix
            await finish_before_cancel(db.rollback())
            prepare = partial(_verified_publication_response, version_message='产物下载版本不匹配，请重新核对')
            response = await bounded_case_read(prepare,
                member(prefix, selected), path, byte_range, version, download,
                code=code, label=label)
    except CaseWriteConflict:
        raise
    except (FileReadConflict, OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, {'code': 'evidence_invalid', 'message': '所选工程产物原件无法核验'}) from error
    response.headers['Content-Disposition'] = 'attachment'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@router.get("/cases/{case_id}/workflow-events")
async def get_workflow_events(case_id: int, after: int = Query(default=0, ge=0, le=2**53-1),
                              limit: int = Query(default=100, ge=1, le=200),
                              user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id, user, db, write=False)
    from ..services.event_replay import page
    try:
        return await page(db,case_id,after,limit)
    except ValueError as error:
        raise HTTPException(409, {'code':'event_cursor_ahead','message':str(error)}) from error


class MoveCaseIn(BaseModel):
    workspace_id: int


class CopyCaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    workspace_id: int | None = None


class CopyWorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    include_cases: bool = True


def _case_action_out(case: Case) -> dict:
    return {"id": case.id, "name": case.name, "workspace_id": case.workspace_id,
            "workflow_state": case.workflow_state, "dict_revision": case.dict_revision}


@router.patch("/workspaces/{ws_id}")
async def rename_workspace_route(ws_id: int, body: WorkspaceIn,
                                 user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    workspace = await _owned_workspace(ws_id, user.id, db)
    try:
        updated = await rename_workspace(db, workspace, user, body.name)
    except CaseAdminError as error:
        raise HTTPException(error.status_code, str(error)) from error
    return {"id": updated.id, "name": updated.name}


@router.patch("/cases/{case_id}")
async def rename_case_route(case_id: int, body: CaseIn,
                            user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    try:
        updated = await rename_case(db, case, body.name)
    except CaseAdminError as error:
        raise HTTPException(error.status_code, str(error)) from error
    return _case_action_out(updated)


@router.post("/cases/{case_id}:move")
async def move_case_route(case_id: int, body: MoveCaseIn,
                          user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _owned_case_record(case_id, user, db)
    case = await get_owned_case(case_id, user, db)
    target = await _owned_workspace(body.workspace_id, user.id, db)
    try:
        updated = await move_case(db, case, target)
    except CaseAdminError as error:
        raise HTTPException(error.status_code, str(error)) from error
    return _case_action_out(updated)


@router.post("/cases/{case_id}:copy")
async def copy_case_route(case_id: int, body: CopyCaseIn,
                          user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from ..config import get_settings
    from ..services.case_read_capacity import file_read_deadline
    async with file_read_deadline('case_copy', '算例复制', seconds=get_settings().case_copy_timeout_seconds):
        source = await get_owned_case(case_id, user, db)
        target = await _owned_workspace(body.workspace_id or source.workspace_id, user.id, db)
        with admit_copy():
            try:
                copied, _prefix, _total = await copy_case(db, source, target, user, body.name, prepare_source=True)
            except CaseAdminError as error:
                raise HTTPException(error.status_code, str(error)) from error
    return _case_action_out(copied)


@router.post("/workspaces/{ws_id}:copy")
async def copy_workspace_route(ws_id: int, body: CopyWorkspaceIn,
                               user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from ..config import get_settings
    from ..services.case_read_capacity import file_read_deadline
    async with file_read_deadline('workspace_copy', '工作区复制', seconds=get_settings().case_copy_timeout_seconds):
        source = await _owned_workspace(ws_id, user.id, db)
        with admit_copy():
            try:
                copied = await copy_workspace(db, source, user, body.name, body.include_cases)
            except CaseAdminError as error:
                raise HTTPException(error.status_code, str(error)) from error
    return {"id": copied.id, "name": copied.name}
