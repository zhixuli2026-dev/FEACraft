"""Durable admission for direct executors that have no versioned step handler."""
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select

from ..models import JobAttempt
from . import job_delivery, job_execution, jobrunner
from .case_write import coordinate
from .case_write_capacity import committing


def execution_spec(case, command, *, runtime='windows', argv=None):
    return {'command': command, 'case_prefix': case.s3_prefix, 'runtime': runtime,
            'argv': argv, 'timeout_seconds': 86400, 'max_log_bytes': 16 * 1024 * 1024,
            'evidence_run_dir': None}


@dataclass
class Admission:
    db: object
    case: object
    user_id: int
    key: str | None
    digest: str
    replay: object = None

    async def submit(self, job_type, command='', *, runtime='windows', argv=None, spec_factory=None):
        job, created = await job_execution.submit(self.db, case_id=self.case.id,
            user_id=self.user_id, key=self.key, digest=self.digest, job_type=job_type,
            command=command, spec=execution_spec(self.case, command, runtime=runtime, argv=argv),
            spec_factory=spec_factory)
        if created:
            # These native executors retain their existing local execution policy.
            await job_delivery.request(self.db, job, user_id=self.user_id)
        return job


@asynccontextmanager
async def prepare(db, case, user, key, endpoint, payload):
    try:
        job_execution.validate_key(key)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    digest = job_execution.request_hash({'endpoint': endpoint, 'payload': payload})
    # Replay precedes mutable-input validation and new-job checks. Storage
    # recovery admission surrounds both replay reads and new preparation.
    # The outer case lock also serializes concurrent requests from other APIs.
    # All three public callers complete their whole submission in this scope.
    # Commit/recovery cleanup must finish before their launch path can run.
    async with committing(db, case, content_write=False):
        replay = await job_execution.lookup_submission(db, case.id, user.id, key, digest)
        admission = Admission(db, case, user.id, key, digest, replay)
        if replay is not None:
            yield admission
        else:
            async with coordinate(db, case):
                yield admission


async def response(db, job, **extra):
    await db.refresh(job)
    attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id)
        .order_by(JobAttempt.number.desc()).limit(1))
    proc = jobrunner.get_proc(job.id)
    return {'job_id': job.id, 'status': job_execution.public_status(job, attempt,
            local_status=proc.status if proc else None), **extra}


async def launch(db, job, **extra):
    # Admission and its outbox must commit before either API or scanner can claim.
    await db.commit()
    try:
        await job_delivery.deliver(job.id)
    except job_delivery.DeliveryDeferred:
        pass  # Capacity is rechecked by the durable scanner.
    except (OSError, ValueError) as error:
        raise HTTPException(409, {'code': 'process_start_failed', 'job_id': job.id,
                                 'message': str(error)}) from error
    return await response(db, job, **extra)
