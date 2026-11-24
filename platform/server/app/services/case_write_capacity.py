"""Commit an audited standalone submission before handing it to another owner."""
from contextlib import asynccontextmanager

from .. import storage
from .async_ops import finish_before_cancel
from .case_write import coordinate
from . import file_transactions, owner_storage_budget


@asynccontextmanager
async def committing(db, case, *, wait_for_quota=False, **conditions):
    """Own the complete transaction, including early-return replay paths.

    Only standalone API boundaries may use this helper. A caller composing a
    larger atomic workflow must retain its own transaction and capacity scope.
    Native execution and worker dispatch must happen after this scope exits.
    """
    remote = storage._use_s3()
    if wait_for_quota and remote:
        from .case_read_capacity import admitted
        capacity = admitted(db, case)
    else:
        capacity = owner_storage_budget.hold_case(db, case)
    async with capacity as owner:
        try:
            if not remote:
                # Recovery may install retained metadata before the new seal's
                # own peak check. Refuse an already exhausted owner first.
                from .quota import check_storage_quota
                if wait_for_quota:
                    from .storage_admission import lock_owner
                    await lock_owner(db, owner.id, wait=True)
                await check_storage_quota(db, owner, 0)
            async with coordinate(db, case, **conditions) as snapshot:
                yield snapshot
            pending = db.info.get('local_file_transactions', {}).get(case.id)
            written_here = pending is not None and pending[0] is db.sync_session.get_transaction()
            await finish_before_cancel(db.commit())
            if written_here:
                finished = (file_transactions.finish_committed_edit(db, case, owner.id) if wait_for_quota
                            else file_transactions.finish_committed(db, case))
                await finish_before_cancel(finished)
        except BaseException:
            await finish_before_cancel(db.rollback())
            raise
