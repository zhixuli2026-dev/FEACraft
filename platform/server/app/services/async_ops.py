"""Wait for non-cancellable filesystem work before releasing its caller's locks."""
import asyncio


async def finish_before_cancel(awaitable):
    operation = asyncio.ensure_future(awaitable)
    cancelled = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            cancelled = True
    result = operation.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
