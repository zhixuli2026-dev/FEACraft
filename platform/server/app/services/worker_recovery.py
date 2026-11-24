"""Run existing owner reconciliation on the machine that can observe its exits."""
import asyncio

from ..config import get_settings
from .async_ops import finish_before_cancel


async def run(ctx, stop):
    from .job_recovery import reconcile_startup
    while not stop.is_set():
        try:
            ctx['recovery_outcomes'] = await reconcile_startup(local_worker=True)
            ctx['recovery_passes'] = ctx.get('recovery_passes', 0)+1
        except Exception as error:
            ctx['recovery_error'] = type(error).__name__
            print('[WARN] Worker reconciliation deferred: '+type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=get_settings().job_heartbeat_seconds)
        except asyncio.TimeoutError:
            pass


def start(ctx):
    stop=asyncio.Event()
    ctx['recovery_stop']=stop
    ctx['recovery_task']=asyncio.create_task(run(ctx,stop),name='worker-owner-recovery')


async def shutdown(ctx):
    stop=ctx.get('recovery_stop')
    if stop is not None:stop.set()
    task=ctx.get('recovery_task')
    if task is not None:await finish_before_cancel(task)
    # Reconciliation can schedule publication completion outside its scan.
    # Drain those operations before the worker closes its database engine.
    from .publication_finalizing import shutdown as stop_publications
    await finish_before_cancel(stop_publications())
