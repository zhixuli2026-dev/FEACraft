"""Local process callbacks and durable job activity across runtime owners."""
from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime, timezone
import threading
import inspect

from sqlalchemy import select
from sqlalchemy.orm import load_only

from ..models import Job, JobAttempt, JobExecutionHead
from . import job_worker_identity

Cancel = Callable[[], None]
Lock = threading.RLock()
_processes: dict[str, dict] = {}


def register(kind: str, resource_id: str | int, case_id: int, cancel: Cancel) -> str:
    key = f"{kind}:{resource_id}"
    with Lock:
        _processes[key] = {"kind": kind, "case_id": case_id, "cancel": cancel}
    return key


def unregister(key: str):
    with Lock:
        _processes.pop(key, None)


def active(case_id: int | None = None) -> list[dict]:
    with Lock:
        return [{"id": key, "kind": value["kind"], "case_id": value["case_id"]}
                for key, value in _processes.items()
                if case_id is None or value["case_id"] == case_id]


async def active_jobs(db, case_ids: Collection[int] | None = None) -> list[dict]:
    """Project claimed attempts, including expired owners and cancellation drain."""
    if case_ids is not None and not case_ids:
        return []
    query = (select(Job, JobAttempt).join(JobAttempt, JobAttempt.job_id == Job.id)
             .join(JobExecutionHead, (JobExecutionHead.job_id == JobAttempt.root_job_id)
                   & (JobExecutionHead.active_attempt_id == JobAttempt.id))
             .where(JobAttempt.claim_token.is_not(None),
                    JobAttempt.phase.in_({'claimed', 'starting', 'running', 'finalizing', 'cancelling'}))
             .options(load_only(Job.id, Job.case_id), load_only(
                 JobAttempt.id, JobAttempt.job_id, JobAttempt.phase, JobAttempt.worker_id,
                 JobAttempt.worker_identity_json, JobAttempt.claim_token, JobAttempt.heartbeat_at,
                 JobAttempt.lease_expires_at)).order_by(Job.id))
    if case_ids is not None:
        query = query.where(Job.case_id.in_(case_ids))

    now = datetime.now(timezone.utc)
    processes = []
    for job, attempt in (await db.execute(query)).all():
        lease_state = 'unverified'
        try:
            job_worker_identity.document(job, attempt)
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        else:
            heartbeat, expiry = attempt.heartbeat_at, attempt.lease_expires_at
            if heartbeat is not None and expiry is not None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc) if heartbeat.tzinfo is None else heartbeat
                expiry = expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry
                if heartbeat < expiry:
                    lease_state = 'current' if now < expiry else 'expired'
        processes.append({'id': f'job:{job.id}', 'kind': 'job', 'case_id': job.case_id,
                          'phase': attempt.phase, 'lease_state': lease_state})
    return processes


async def active_processes(db, case_ids: Collection[int] | None = None) -> list[dict]:
    processes = {row['id']: row for row in active()
                 if case_ids is None or row['case_id'] in case_ids}
    processes.update((row['id'], row) for row in await active_jobs(db, case_ids))
    return list(processes.values())


def cancel_case(case_id: int) -> dict:
    with Lock:
        selected = [(key, value) for key, value in _processes.items()
                    if value["case_id"] == case_id]
    counts: dict[str, int] = {}
    errors = []
    for key, value in selected:
        try:
            value["cancel"]()
            counts[value["kind"]] = counts.get(value["kind"], 0) + 1
        except Exception as error:
            errors.append(f"{key}: {type(error).__name__}")
        finally:
            unregister(key)
    return {"ok": not errors, "case_id": case_id, "cancelled": counts,
            "remaining": active(case_id), "errors": errors}


async def cancel_case_wait(case_id: int, *, excluded: Collection[str] = ()) -> dict:
    """Await interactive cleanup; failed callbacks retain their visible entry."""
    with Lock:
        selected=[(key,value) for key,value in _processes.items()
                  if value['case_id']==case_id and key not in excluded]
    counts={};errors=[]
    for key,value in selected:
        try:
            result=value['cancel']()
            if inspect.isawaitable(result):await result
            counts[value['kind']]=counts.get(value['kind'],0)+1
            unregister(key)
        except Exception as error:errors.append(key+': '+type(error).__name__)
    return {'ok':not errors,'case_id':case_id,'cancelled':counts,'remaining':active(case_id),'errors':errors}
