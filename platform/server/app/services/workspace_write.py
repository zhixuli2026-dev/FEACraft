"""Workspace membership locks, acquired before case control locks in ID order."""
from contextlib import asynccontextmanager

from sqlalchemy import update

from ..models import Workspace
from .case_write import CaseWriteConflict
from .durable_events import ensure_parent_transaction


@asynccontextmanager
async def coordinate_workspaces(db, workspace_ids, *, allow_deleted=False):
    await ensure_parent_transaction(db)
    async with db.begin_nested():
        for workspace_id in sorted(set(workspace_ids)):
            result = await db.execute(update(Workspace).where(Workspace.id == workspace_id)
                .values(name=Workspace.name).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                raise CaseWriteConflict("workspace_unavailable", "工作区已不存在，请刷新后重试")
            if not allow_deleted:
                from .trash import is_workspace_deleted
                if is_workspace_deleted(workspace_id):
                    raise CaseWriteConflict("workspace_unavailable", "工作区已在回收站，请先恢复")
        yield
        await db.flush()
