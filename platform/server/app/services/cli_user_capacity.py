"""Private CLI writes fenced by the existing owner's quota transaction."""
import asyncio
from fastapi import HTTPException
from sqlalchemy import select
from ..models import User,Case
from .async_ops import finish_before_cancel
from .quota import check_locked_storage_quota,check_storage_quota
from .storage_admission import lock_owner


async def owner(db,user_id):
    await lock_owner(db,user_id)
    # Keep compatibility with earlier secret merges using the user row lock.
    user=await db.scalar(select(User).where(User.id==user_id).with_for_update().execution_options(populate_existing=True))
    if user is None or user.status!='active':raise HTTPException(403,'CLI 用户已不可用')
    return user


async def lock_startup_inventory(db, user_id, inventory=None):
    from .. import storage
    from ..db import SessionLocal
    from .cli_initialization_capacity import reconcile
    from .owner_storage_budget import recover_owner
    from .quota import prepare_storage_inventory
    from .request_deadline import current
    from .storage_admission import InventoryOutdated, quota_revision

    deadline = current()
    if deadline is None:
        raise ValueError('CLI startup inventory requires the request deadline')
    if inventory is not None and inventory.owner_id != user_id:
        raise ValueError('CLI startup inventory belongs to another user')
    remote_without_grant = inventory is None and storage._use_s3()
    while True:
        deadline.check()
        if inventory is None:
            if remote_without_grant:
                try:
                    revision = await recover_owner(user_id, wait=True, require_available=True)
                except HTTPException as error:
                    if (error.status_code != 409 or not isinstance(error.detail, dict)
                            or error.detail.get('code') != 'storage_quota_busy'):
                        raise
                    await asyncio.sleep(.05)
                    continue
            else:
                async with SessionLocal() as read:
                    revision = await quota_revision(read, user_id)
            inventory = await prepare_storage_inventory(user_id, revision)
        try:
            await lock_owner(db, user_id, wait=True, inventory_revision=inventory.revision)
        except InventoryOutdated:
            # This transaction has only attempted admission. Restart it before
            # any state or private file has been written.
            await finish_before_cancel(db.rollback())
            inventory = None
            await asyncio.sleep(.05)
        else:
            await reconcile(db, user_id)
            return inventory


async def initialize(db,user_id,identifier,credential,*,runtime,distro,node_admitted=False,inventory=None):
    from .codex_sessions import PRIVATE_ENVIRONMENT_BYTES, _private_environment, private_environment
    user=await owner(db,user_id)
    if inventory is None:
        await check_storage_quota(db,user,PRIVATE_ENVIRONMENT_BYTES)
    else:
        await check_locked_storage_quota(db,user,PRIVATE_ENVIRONMENT_BYTES,inventory=inventory)
    # The caller owns the SQL transaction. Drain the real writer before that
    # transaction can release its owner lock, including cancellation paths.
    writer = _private_environment if node_admitted else private_environment
    return await finish_before_cancel(asyncio.to_thread(writer,user_id,identifier,credential,
        runtime=runtime,distro=distro))


async def remember(db,user_id,vault,values):
    from ..config import get_settings
    from .cli_private_paths import account_directory
    user=await owner(db,user_id)
    await finish_before_cancel(asyncio.to_thread(account_directory,vault.folder,get_settings(),user_id=user_id))
    size=await finish_before_cancel(asyncio.to_thread(vault.update_size,values))
    if not size:return
    await check_storage_quota(db,user,size)
    await finish_before_cancel(asyncio.to_thread(vault.remember,values,maximum_bytes=size))


async def images(db,session,prepared):
    from .owner_storage_budget import owner_for_case
    size=sum(len(data) for _,data in prepared)
    if not size:return
    case=await db.get(Case,session.case_id)
    if case is None:raise ValueError('Image case disappeared')
    public_owner=await owner_for_case(db,case)
    amounts={session.user_id:size}
    amounts[public_owner.id]=amounts.get(public_owner.id,0)+size
    # Both copies count, even when their billing owners differ. Try-locks under
    # a case lock refuse contention instead of waiting with inverted lock order.
    for user_id,amount in sorted(amounts.items()):
        await check_storage_quota(db,await owner(db,user_id),amount)
