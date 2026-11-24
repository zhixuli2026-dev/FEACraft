"""arq entry point with the Windows event-loop/signal adaptation kept local."""
import asyncio
import os

from arq.worker import Worker, get_kwargs

from .distributed_queue import WorkerSettings


class PlatformWorker(Worker):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('job_completion_wait', 30)
        if os.name == 'nt':
            kwargs['handle_signals'] = False
        super().__init__(*args, **kwargs)

    async def close(self, *, grace_seconds=None):
        from .async_ops import finish_before_cancel
        await finish_before_cancel(self._close_drained(grace_seconds))

    async def _close_drained(self, grace_seconds):
        self.allow_pick_jobs = False
        if self.main_task is not None and not self.main_task.done():
            self.main_task.cancel()
            await asyncio.gather(self.main_task, return_exceptions=True)
        pending = {task for task in self.tasks.values() if not task.done()}
        grace = self._job_completion_wait if grace_seconds is None else max(0, grace_seconds)
        if pending and grace:
            _, pending = await asyncio.wait(pending, timeout=grace)
        for task in pending:
            task.cancel()
        # One failed delivery must not let gather fail early while a sibling
        # still holds a process or is committing its final state.
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        # Delivery cancellation drains the actual process and SQL finalization
        # before arq acknowledges/retries it. Skip arq's second cancellation
        # (and its unsupported Windows SIGUSR1 path).
        previous = self._handle_signals
        self._handle_signals = True
        try:
            await super().close()
        finally:
            self._handle_signals = previous
            from ..db import engine
            await engine.dispose()


def create_worker(**overrides):
    return PlatformWorker(**{**get_kwargs(WorkerSettings), **overrides})


if __name__ == '__main__':
    create_worker().run()
