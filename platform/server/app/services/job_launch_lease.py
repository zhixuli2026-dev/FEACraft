"""Keep the committed claim alive until JobProcess takes over its heartbeat.

Cancellation interrupts only this launch task. Non-cancellable filesystem work
must finish before the task records its outcome or gives up its owned files.
"""
import asyncio

from ..config import get_settings
from . import job_execution
from .async_ops import finish_before_cancel


class LaunchLease:
    def __init__(self, owner):
        self.owner = owner
        self.failure = None
        self.operation = None

    def translate(self, error):
        return self.failure if isinstance(error, asyncio.CancelledError) and self.failure is not None else error

    def cancel_launch(self):
        if self.operation is not None and not self.operation.done() and self.failure is None:
            self.failure = job_execution.JobLaunchCancelled(self.owner.job_id)
            self.operation.cancel()

    def accept(self, status):
        if status == 'cancelled':
            raise job_execution.JobLaunchCancelled(self.owner.job_id)
        if status not in {'queued', 'running'}:
            raise job_execution.ExecutionConflict('state_conflict', 'Launch claim is no longer active')

    async def checkpoint(self):
        if self.failure is not None:
            raise self.failure
        self.accept(await job_execution.heartbeat(self.owner))
        from .execution_budget import current
        budget=current(self.owner)
        if budget is not None:budget.check()

    async def watch(self):
        try:
            while not self.operation.done():
                from .execution_budget import current
                budget=current(self.owner)
                delay=get_settings().job_heartbeat_seconds
                if budget is not None:delay=min(delay,budget.remaining())
                await asyncio.sleep(delay)
                if not self.operation.done():
                    await self.checkpoint()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self.operation.done() and self.failure is None:
                from .execution_budget import BudgetExceeded
                self.failure = error if isinstance(error, (job_execution.JobLaunchCancelled,BudgetExceeded)) else (
                    job_execution.ExecutionConflict('state_conflict',
                        'Launch ownership heartbeat failed: '+type(error).__name__))
                self.operation.cancel()

    async def run(self, launch):
        self.operation = asyncio.create_task(launch(self))
        watcher = asyncio.create_task(self.watch())
        try:
            return await self.operation
        finally:
            watcher.cancel()
            await finish_before_cancel(asyncio.gather(watcher, return_exceptions=True))
