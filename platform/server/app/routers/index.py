"""工作区索引路由：重建/状态/搜索（对标原版 index.rebuild/index.status/search.query）"""
from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import indexsvc
from ..services.async_ops import finish_before_cancel
from ..services.case_read_capacity import file_read_deadline, reading
from ..services.case_write import bounded_case_read
from ..services.request_lifecycle import wait
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1/cases/{case_id}", tags=["index"])


@router.post("/index:rebuild")
async def rebuild_index(case_id: int, request: Request, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    # Complete any required storage recovery under the usual case and owner
    # locks. The derived index then verifies its source inside its own writer
    # transaction without holding the business database through the scan.
    async with reading(db, case):
        prefix = case.s3_prefix
    await finish_before_cancel(db.rollback())
    try:
        result = await wait(bounded_case_read(indexsvc.rebuild, case_id, prefix, code='index_rebuild', label='索引重建'), request=request)
    except indexsvc.IndexBuildBusy as error:
        raise HTTPException(409, {'code': 'index_build_busy', 'message': str(error)}) from error
    except indexsvc.IndexSourceChanged as error:
        raise HTTPException(409, {'code': 'index_source_changed', 'message': str(error)}) from error
    return {"ok": True, **result}


@router.get("/index/status")
async def index_status(case_id: int, request: Request, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    async with file_read_deadline('index_status', '索引状态'):
        user_id = user.id
        case = await get_owned_case(case_id, user, db, write=False)
        async with reading(db, case):
            await get_owned_case(case_id, user, db, write=False)
            workspace_id, prefix = case.workspace_id, case.s3_prefix
        await finish_before_cancel(db.rollback())
        try:
            result = await wait(bounded_case_read(indexsvc.status, case_id, prefix, code='index_status', label='索引状态'), request=request)
        except indexsvc.IndexSourceChanged as error:
            raise HTTPException(409, {'code': 'index_source_changed', 'message': str(error)}) from error
        user = await db.get(User, user_id)
        if user is None or user.status != 'active':
            raise HTTPException(401, '用户不可用')
        case = await get_owned_case(case_id, user, db, write=False)
        if (case.workspace_id, case.s3_prefix) != (workspace_id, prefix):
            raise HTTPException(409, {'code': 'index_source_changed', 'message': '算例目录已变化，请重试'})
        return result


@router.get("/search")
async def search_case(case_id: int, request: Request, q: str = Query(..., min_length=1, max_length=120),
                      content: bool = Query(True), limit: int = Query(30, ge=1, le=100),
                      user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    async with file_read_deadline('case_search', '算例搜索'):
        user_id = user.id
        case = await get_owned_case(case_id, user, db, write=False)
        query = q.strip()
        if not query:
            raise HTTPException(400, "空查询")
        async with reading(db, case):
            await get_owned_case(case_id, user, db, write=False)
            workspace_id, prefix = case.workspace_id, case.s3_prefix
        await finish_before_cancel(db.rollback())
        try:
            run_search = partial(indexsvc.search, case_id, query, limit, content, s3_prefix=prefix)
            result = await wait(bounded_case_read(run_search, code='case_search', label='算例搜索'), request=request)
        except indexsvc.IndexSourceChanged as error:
            raise HTTPException(409, {'code': 'index_source_changed', 'message': str(error)}) from error
        user = await db.get(User, user_id)
        if user is None or user.status != 'active':
            raise HTTPException(401, "用户不可用")
        case = await get_owned_case(case_id, user, db, write=False)
        if (case.workspace_id, case.s3_prefix) != (workspace_id, prefix):
            raise HTTPException(409, {'code': 'index_source_changed', 'message': '算例目录已变化，请重试'})
        return result
