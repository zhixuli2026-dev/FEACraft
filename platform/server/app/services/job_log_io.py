"""Finite log-read workers with no admission queue; cancellation drains real I/O."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from threading import BoundedSemaphore

from fastapi import HTTPException
from .async_ops import finish_before_cancel
from .file_read import FileReadConflict
from .job_log_reader import page
from .request_deadline import scope

WORKERS = 4
SECONDS = 30
_slots = BoundedSemaphore(WORKERS)
_executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='job-log')


async def read(*args, **kwargs):
    if not _slots.acquire(blocking=False):
        raise HTTPException(429, {'code': 'log_read_busy', 'message': '日志读取繁忙，请稍后重试'},
            headers={'Retry-After': '1'})
    try:
        async with scope(SECONDS) as deadline:
            operation = partial(page, *args, **kwargs)
            future = asyncio.get_running_loop().run_in_executor(_executor, copy_context().run, operation)
            result = await finish_before_cancel(future)
            deadline.check()
            return result
    except TimeoutError as error:
        raise HTTPException(504, {'code': 'log_read_timeout', 'message': '日志读取超时，请稍后重试'}) from error
    except FileReadConflict as error:
        raise HTTPException(409, 'Log changed during read; retry the requested page') from error
    finally:
        # A disconnected request cannot make its still-running thread's slot
        # available. finish_before_cancel waits for it even after cancellation.
        _slots.release()
