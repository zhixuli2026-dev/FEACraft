"""J14：Redis/arq 分布式作业 worker、入队与取消广播。"""
from __future__ import annotations

import asyncio
import os

from arq import create_pool
from arq.connections import RedisSettings
from . import jobrunner

ACTIVE: dict[int, jobrunner.JobProcess] = {}
CANCEL_CHANNEL = "vibeflow:cancel"
QUEUE_NAME = "vibeflow:jobs"

redis_settings = RedisSettings(host=os.getenv("VIBEFLOW_REDIS_HOST", "127.0.0.1"),
    port=int(os.getenv("VIBEFLOW_REDIS_PORT", "6380")),
    password=os.getenv("VIBEFLOW_REDIS_PASSWORD") or None,
    conn_timeout=5, conn_retries=3)


def distributed_enabled() -> bool:
    return os.getenv("VIBEFLOW_DISTRIBUTED_JOBS", "false").lower() == "true"


async def enqueue(job_id: int, case_prefix: str, command: str, runtime: str, **options):
    pool = await create_pool(redis_settings)
    try:
        await pool.enqueue_job("run_job", job_id, case_prefix, command, runtime, **options,
                               _queue_name=QUEUE_NAME)
    finally:
        await pool.close()


async def cancel(job_id: int):
    pool = await create_pool(redis_settings)
    try:
        await pool.publish(CANCEL_CHANNEL, str(job_id))
    finally:
        await pool.close()


async def enqueue_delivery(job_id, attempt_id, *, target_host=None):
    from .job_execution_hosts import queue_name
    pool = await create_pool(redis_settings)
    try:
        await pool.enqueue_job('run_delivery',job_id,_job_id='vf-delivery-'+attempt_id,
            _queue_name=queue_name(target_host) if target_host is not None else QUEUE_NAME)
    finally:
        await pool.close()


async def run_delivery(ctx, job_id):
    from .job_delivery import deliver
    # SQL intent survives Redis outages and failed/expired transport deliveries.
    # No result retention: an unclaimed intent can be redelivered on the next scan.
    return await deliver(job_id,redis_worker=True)


async def run_job(ctx, job_id: int, case_prefix: str, command: str, runtime: str,
                  timeout_seconds: float = 86400, max_log_bytes: int = 16 * 1024 * 1024,
                  evidence_run_dir: str | None = None):
    import json
    from sqlalchemy import select
    from ..db import SessionLocal
    from ..models import Job, JobAttempt
    from . import job_delivery, job_execution
    if await job_delivery.refuse_unsealed(job_id):return 'input_recovery_required'
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        if job is None or job.status != 'queued':return 'already_owned_or_terminal'
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id)
            .order_by(JobAttempt.number.desc()).limit(1))
        if attempt is None or await job_delivery.intent(db,job,attempt) is None:
            return 'legacy_delivery_unverified'
        spec={'command':command,'case_prefix':case_prefix,'runtime':runtime,'argv':None,
            'timeout_seconds':timeout_seconds,'max_log_bytes':max_log_bytes,'evidence_run_dir':evidence_run_dir}
        if json.loads(attempt.input_manifest_json)['execution_spec']!=spec:
            raise job_execution.ExecutionConflict('evidence_invalid','Legacy Redis message differs from its durable request')
    # Old messages may deliver only an already admitted sealed Redis request.
    # SQL ownership, capacity and input checks are revalidated at atomic claim.
    return await job_delivery.deliver(job_id,redis_worker=True)


async def _cancel_listener(redis):
    pubsub = redis.pubsub()
    await pubsub.subscribe(CANCEL_CHANNEL)
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                job_id = int(message["data"])
            except (TypeError, ValueError):
                continue
            proc = ACTIVE.get(job_id)
            if proc is not None:
                proc.cancel()
    finally:
        await pubsub.unsubscribe(CANCEL_CHANNEL)
        await pubsub.close()


async def on_startup(ctx):
    ctx["cancel_task"] = asyncio.create_task(_cancel_listener(ctx["redis"]))
    from .worker_recovery import start
    start(ctx)


async def on_shutdown(ctx):
    from .worker_recovery import shutdown
    await shutdown(ctx)
    task = ctx.get("cancel_task")
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class WorkerSettings:
    from arq.worker import func
    from .job_execution_hosts import worker_queue
    functions = [run_job,func(run_delivery,keep_result=0)]
    on_startup = on_startup
    on_shutdown = on_shutdown
    queue_name = worker_queue()
    redis_settings = redis_settings
    max_jobs = 4
