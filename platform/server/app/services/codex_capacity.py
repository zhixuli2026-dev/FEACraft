"""Codex 临时容量错误的定向续跑；不重发原用户请求。"""
from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
CAPACITY_DELAYS = (5.0, 10.0, 20.0, 40.0, 60.0)
CAPACITY_CONTINUE_PROMPT = (
    "Continue the existing turn from its saved state. Do not repeat earlier operations."
)


def is_capacity_error(error: BaseException | str) -> bool:
    text = str(error)
    return "Selected model is at capacity" in text or "serverOverloaded" in text


def _default_jitter(_attempt: int) -> float:
    return random.uniform(0.0, 0.5)


async def _wait_delay(delay: float, cancel: asyncio.Event | None,
                      sleep: Callable[[float], Awaitable[None]]):
    if cancel is None:
        await sleep(delay)
        return
    if cancel.is_set():
        raise asyncio.CancelledError
    sleeper = asyncio.create_task(sleep(delay))
    cancelled = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait(
            (sleeper, cancelled), return_when=asyncio.FIRST_COMPLETED)
        if cancelled in done:
            raise asyncio.CancelledError
        await sleeper
    finally:
        for task in (sleeper, cancelled):
            if not task.done():
                task.cancel()
        for task in (sleeper, cancelled):
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def run_with_capacity_retry(
        operation: Callable[[], Awaitable[T]],
        continue_operation: Callable[[], Awaitable[T]], *,
        cancel: asyncio.Event | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[int], float] | None = _default_jitter,
        retryable: Callable[[BaseException], bool] = is_capacity_error,
        on_backoff: Callable[[int, float], Awaitable[None]] | None = None) -> T:
    """原始操作至多一次；容量失败后以短继续操作续跑，最多五次。"""
    retries = 0
    while True:
        if cancel is not None and cancel.is_set():
            raise asyncio.CancelledError
        try:
            return await (continue_operation() if retries else operation())
        except Exception as error:
            if retries >= len(CAPACITY_DELAYS) or not retryable(error):
                raise
            delay = CAPACITY_DELAYS[retries] + (jitter(retries) if jitter else 0.0)
            retries += 1
            if on_backoff is not None:
                await on_backoff(retries,delay)
            await _wait_delay(delay, cancel, sleep)
