"""Await request work and join cancellation before releasing its caller."""
import asyncio

from fastapi import HTTPException

from .async_ops import finish_before_cancel
from .workflow_state import ExecutionConflict


async def wait(awaitable,*,request=None,invalidated=None):
    operation=asyncio.ensure_future(awaitable)
    monitors=[];closing=asyncio.Event()
    async def disconnected():
        # Drain unused HTTP bodies, then block on the actual ASGI disconnect.
        while not closing.is_set():
            message=await request.receive()
            if message['type']=='http.disconnect':return 'disconnect'
    async def changed():
        while True:
            if await invalidated():return 'state_conflict'
            await asyncio.sleep(.1)
    try:
        if request is None and invalidated is None:return await operation
        if request is not None:monitors.append(asyncio.create_task(disconnected()))
        if invalidated is not None:monitors.append(asyncio.create_task(changed()))
        done,_=await asyncio.wait({operation,*monitors},return_when=asyncio.FIRST_COMPLETED)
        stopped=next((task for task in monitors if task in done),None)
        if stopped is not None:
            reason=stopped.result()
            if reason=='disconnect':raise HTTPException(499,'请求连接已断开')
            raise ExecutionConflict('state_conflict','Workflow changed during request execution')
        if request is not None and operation.cancelled():
            raise HTTPException(503,{'code':'request_interrupted','message':'服务端执行已中断'})
        return await operation
    finally:
        closing.set()
        if not operation.done():operation.cancel()
        for monitor in monitors:monitor.cancel()
        await finish_before_cancel(asyncio.gather(operation,*monitors,return_exceptions=True))
