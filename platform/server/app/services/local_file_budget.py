"""File journal peaks charged under the caller's owner transaction and node claim."""
import asyncio
from contextlib import asynccontextmanager


def replacement_peak(replacements,tail_bytes):
    """Additional bytes while serial atomic replacements keep old files alive."""
    if type(tail_bytes) is not int or tail_bytes<0:raise ValueError('Invalid journal metadata size')
    delta=peak=0
    for before,after in replacements:
        old=before['size'] if before is not None else 0
        new=after['size'] if after is not None else 0
        if any(type(size) is not int or size<0 for size in (old,new)):
            raise ValueError('Invalid journal file size')
        peak=max(peak,delta+new)
        delta+=new-old
    return max(peak,delta+tail_bytes)


async def lock_for_prepared_edit(db,owner_id,*,wait=False,session_factory=None):
    """Start a fresh write transaction only after the actual disk inventory."""
    from ..db import SessionLocal
    from .async_ops import finish_before_cancel
    from .quota import prepare_storage_inventory
    from .request_deadline import current
    from .storage_admission import InventoryOutdated,lock_owner,quota_revision

    if db.in_transaction() or current() is None:
        raise ValueError('Prepared file admission requires a fresh transaction and a deadline')
    if session_factory is None:session_factory=SessionLocal
    while True:
        async with session_factory() as read:
            revision=await quota_revision(read,owner_id)
        inventory=await prepare_storage_inventory(owner_id,revision)
        try:
            await lock_owner(db,owner_id,wait=wait,inventory_revision=revision)
        except InventoryOutdated:
            await finish_before_cancel(db.rollback())
            await asyncio.sleep(.05)
        else:return inventory


async def check_quota(db, case, size, *, inventory=None, staged=None):
    from ..models import User,Workspace
    from .quota import check_locked_storage_quota,check_storage_quota
    from .storage_admission import quota_revision
    workspace=await db.get(Workspace,case.workspace_id,populate_existing=True)
    if workspace is None:raise ValueError('File storage owner disappeared')
    owner=await db.get(User,workspace.user_id)
    if owner is None:raise ValueError('File storage owner disappeared')
    if staged is not None:
        from .. import storage

        path, materialized = staged
        owner_root = storage._fs_root().resolve() / 'workspaces' / str(owner.id)
        if path.is_relative_to(owner_root):
            size -= materialized
    # Same transaction, no independent writer and no commit. PostgreSQL uses a
    # try-lock because callers already hold the case lock; SQLite serializes
    # writers in this parent transaction. Both retain the lock until caller end.
    if inventory is not None and inventory.owner_id!=owner.id:
        from .case_write import CaseWriteConflict
        raise CaseWriteConflict('case_source_changed','项目存储账户已变化，请刷新后重试')
    if inventory is not None and await quota_revision(db,owner.id)==inventory.revision+1:
        await check_locked_storage_quota(db,owner,size,inventory=inventory)
    else:
        # Compatibility recovery may have mutated the account inside this
        # very transaction after we took its epoch. Do not reuse that scan.
        await check_storage_quota(db,owner,size)


@asynccontextmanager
async def allocation(db,case,size,*,target_root=None):
    from .node_storage_budget import allocation as node_allocation

    await check_quota(db, case, size)
    async with node_allocation(size, target_root=target_root):yield
