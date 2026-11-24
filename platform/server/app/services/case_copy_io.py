"""Four concurrent case storage copy requests with dedicated I/O workers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, copy_context
from functools import partial
from threading import BoundedSemaphore

from fastapi import HTTPException

WORKERS = 4
_slots = BoundedSemaphore(WORKERS)
_executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='case-copy')
_admitted = ContextVar('case_copy_admitted', default=False)


@contextmanager
def _hold_slot():
    token = _admitted.set(True)
    try:
        yield
    finally:
        # Provision I/O drains before the request releases its slot.
        _admitted.reset(token)
        _slots.release()


@contextmanager
def admit(*, code='case_copy_busy', label='复制'):
    if _admitted.get():
        yield
        return
    if not _slots.acquire(blocking=False):
        raise HTTPException(429, {'code': code, 'message': label+'繁忙，请稍后重试'},
                            headers={'Retry-After': '1'})
    with _hold_slot():
        yield


@asynccontextmanager
async def wait_for_admission():
    """Use the same I/O slots under the caller's request deadline."""
    if _admitted.get():
        yield
        return
    while not _slots.acquire(blocking=False):
        await asyncio.sleep(.025)
    with _hold_slot():
        yield


def submit(function, *args, **kwargs):
    if not _admitted.get():
        return asyncio.to_thread(function, *args, **kwargs)
    operation = partial(function, *args, **kwargs)
    return asyncio.get_running_loop().run_in_executor(_executor, copy_context().run, operation)
