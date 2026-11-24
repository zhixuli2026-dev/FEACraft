"""Prepare an existing file journal before taking the caller's case lock."""
from functools import partial
from types import SimpleNamespace

from .. import storage
from ..db import SessionLocal
from ..models import Case
from . import case_copy_io, case_read_capacity, storage_operations
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict, assert_case_available, bounded_case_read, coordinate
from .durable_events import lock_case_execution


CODE = 'file_recovery'
LABEL = '文件事务恢复'


class DeferredFileRecovery(CaseWriteConflict):
    def __init__(self, case):
        super().__init__('file_recovery_required', '后台文件读取等待事务恢复')
        self.reference = SimpleNamespace(id=case.id, workspace_id=case.workspace_id, s3_prefix=case.s3_prefix)


def pending_local(case):
    from . import local_file_recovery, tree_transactions

    return tree_transactions.pending_reference(case) or local_file_recovery.pending_reference(case)


async def reject_pending(case):
    """Check for a recovery journal without mutating storage."""
    if storage._use_s3():
        from . import s3_file_transactions as remote

        if await remote._io(remote.pending_version, case.s3_prefix) is not None:
            raise DeferredFileRecovery(case)
    elif pending_local(case) is not None:
        raise DeferredFileRecovery(case)


async def require_settled(case, *, catalog_etag=None):
    """Case control can fence a catalog and defer journals without reading their bodies."""
    await reject_pending(case)
    if storage._use_s3():
        from . import s3_file_transactions as remote
        from .s3_live_store import fence

        # Preserve the catalog fence for ordinary reads. A legacy interrupted
        # catalog CAS may make a journal pointer visible during the fence.
        await remote._io(fence, case.s3_prefix, expected_etag=catalog_etag)
        await reject_pending(case)


async def check_operation_for_delivery(db, case):
    if storage._use_s3():
        operation = await storage_operations.status_locked(db, case.id)
        if operation is not None:
            raise DeferredFileRecovery(case)
    await storage_operations.admit_locked(db, case.id)


async def recover_local(checked, reference=None, *, session_factory=SessionLocal):
    from . import local_file_recovery, tree_transactions

    async with case_read_capacity.file_read_deadline(CODE, LABEL), session_factory() as db:
        if reference is None:
            case = await checked(db)
            reference = pending_local(case)
            if reference is not None:
                await finish_before_cancel(db.rollback())
        if reference is not None:
            if getattr(reference, 'kind', None) == 'files':
                with case_copy_io.admit(code=CODE+'_busy', label=LABEL):
                    return await local_file_recovery.recover(checked, reference, session_factory=session_factory)
            prepared = await bounded_case_read(tree_transactions.read_recovery, reference,
                                               code=CODE, label='目录事务校验')
            case = await checked(db)
            if case.s3_prefix != reference.s3_prefix:
                raise CaseWriteConflict('case_source_changed', '项目来源已变化，请先核对恢复目标')
            current_tree = tree_transactions.pending_reference(case)
            if current_tree is None and local_file_recovery.pending_reference(case) is None:
                return
            if current_tree is None or prepared is None:
                raise CaseWriteConflict('file_recovery_required', '文件事务在恢复准备期间变化，请重试')
            db.info['local_tree_recovery_read'] = prepared
        async with coordinate(db, case):
            pass
        await finish_before_cancel(db.commit())


async def recover_remote(checked):
    from .request_deadline import SANDBOX_RECOVERY_SECONDS
    from .s3_storage_recovery import recover

    async with case_read_capacity.file_read_deadline(CODE, LABEL, seconds=SANDBOX_RECOVERY_SECONDS):
        with case_copy_io.admit(code=CODE+'_busy', label=LABEL):
            return await recover(checked)


async def _check_bound_case(db, reference):
    await lock_case_execution(db, reference.id)
    case = await db.get(Case, reference.id, populate_existing=True)
    assert_case_available(case)
    if (case.workspace_id, case.s3_prefix) != (reference.workspace_id, reference.s3_prefix):
        raise CaseWriteConflict('case_source_changed', '文件事务关联的项目来源已变化')
    return case


async def recover_bound_case(reference, session_factory):
    checked = partial(_check_bound_case, reference=reference)
    if storage._use_s3():
        await recover_remote(checked)
    else:
        await recover_local(checked, session_factory=session_factory)
