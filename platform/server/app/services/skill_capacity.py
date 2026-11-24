"""Current skill-root inventory and serialized personal storage admission."""
from contextlib import asynccontextmanager
from types import SimpleNamespace


def scan(settings, counter, *, user_id=None):
    from .skill_store import storage_root
    from .storage_usage import _scan_fs_tree
    root = storage_root(settings)
    if user_id is None:
        _scan_fs_tree(SimpleNamespace(storage_fs_root=str(root.parent)), (root.name,), counter)
    else:
        if type(user_id) is not int or user_id <= 0:
            raise ValueError('Invalid personal skill storage owner')
        _scan_fs_tree(SimpleNamespace(storage_fs_root=str(root)), ('users', str(user_id)), counter)


@asynccontextmanager
async def writing(db, user, scope, incoming):
    from .async_ops import finish_before_cancel
    from .quota import check_storage_quota
    try:
        if scope == 'user':
            await check_storage_quota(db, user, incoming)
        yield
        await finish_before_cancel(db.commit())
    except BaseException:
        await finish_before_cancel(db.rollback())
        raise
