"""Versioned state changes with transactional events; no implicit stage approval.

These functions flush but do not commit. The caller owns its unit of work. A
savepoint prevents a caught conflict from leaving a consumed sequence or event.
"""
from __future__ import annotations

from sqlalchemy import select, update

from ..models import Job, JobAttempt, JobExecutionHead, WorkflowRun, WorkflowStep
from .durable_events import append_event, ensure_parent_transaction, now, reserve_case_sequence

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
ERROR_CODES = frozenset({"invalid_request", "revision_conflict", "input_stale", "idempotency_conflict",
    "state_conflict", "dependency_failed", "confirmation_required", "process_start_failed", "process_exit_failed", "process_cleanup_failed",
    "evidence_invalid", "sync_failed", "publication_superseded", "publication_failed", "persistence_failed",
    "resource_limit", "cancel_requested", "lease_expired", "recovery_required", "runtime_unavailable"})
TRANSITIONS = {
    "run": {
        "queued":{"running","cancelling","failed","cancelled","interrupted"},
        "running":{"awaiting_confirmation","cancelling","succeeded","failed","interrupted"},
        "awaiting_confirmation":{"running","cancelling","failed","interrupted"},
        "cancelling":{"cancelled","failed","interrupted"}},
    "step": {
        "pending":{"ready","blocked","cancelled"},
        "ready":{"running","awaiting_confirmation","cancelled","blocked"},
        "running":{"finalizing","failed","cancelling","interrupted"},
        "finalizing":{"succeeded","failed","cancelling","interrupted"},
        "awaiting_confirmation":{"succeeded","cancelled","blocked","interrupted"},
        "cancelling":{"cancelled","failed","interrupted"}},
    "attempt": {
        "queued":{"claimed","cancelled","interrupted"},
        "claimed":{"starting","cancelled","failed","interrupted"},
        "starting":{"running","cancelling","failed","interrupted"},
        "running":{"finalizing","cancelling","failed","interrupted"},
        "finalizing":{"succeeded","cancelling","failed","interrupted"},
        "cancelling":{"cancelled","failed","interrupted"}},
}
MODELS = {"run":WorkflowRun, "step":WorkflowStep, "attempt":JobAttempt}


class ExecutionConflict(RuntimeError):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        self.job_id = None
        super().__init__(detail)


class InvalidStepInput(ExecutionConflict):
    """A deterministic input error for the prepared step's current revision."""

    def __init__(self, detail):
        super().__init__('invalid_request', detail)


async def entity_context(db, kind, identifier):
    model = MODELS.get(kind)
    if model is None:
        raise ValueError("Unknown workflow entity")
    entity = await db.get(model, identifier, populate_existing=True)
    if entity is None:
        raise ExecutionConflict("invalid_request", "Execution record does not exist")
    run_id = step_id = attempt_id = None
    if kind == "run":
        case_id, run_id = entity.case_id, entity.id
    elif kind == "step":
        run = await db.get(WorkflowRun, entity.run_id)
        if run is None:
            raise ExecutionConflict("invalid_request", "Step has no run")
        case_id, run_id, step_id = run.case_id, run.id, entity.id
    else:
        job = await db.get(Job, entity.job_id)
        if job is None:
            raise ExecutionConflict("invalid_request", "Attempt has no Job")
        case_id, step_id, attempt_id = job.case_id, entity.step_id, entity.id
        if step_id:
            step = await db.get(WorkflowStep, step_id)
            run = await db.get(WorkflowRun, step.run_id) if step else None
            if run is None or run.case_id != case_id:
                raise ExecutionConflict("invalid_request", "Attempt crosses case ownership")
            run_id = run.id
    return entity, case_id, {"run_id":run_id, "step_id":step_id, "attempt_id":attempt_id}


def check_transition(kind, current, target):
    if current == target:
        return
    if current in TERMINAL or current == "blocked" or target not in TRANSITIONS[kind].get(current, set()):
        raise ExecutionConflict("state_conflict", f"Illegal {kind} transition {current} -> {target}")


async def transition(db, kind, identifier, target, *, expected_version, reason_code="", detail="",
                     exit_code=None, claim_token=None):
    entity, case_id, links = await entity_context(db, kind, identifier)
    if expected_version < 0 or entity.version != expected_version:
        raise ExecutionConflict("state_conflict", "Execution version changed")
    field = "phase" if kind == "attempt" else "status"
    current = getattr(entity, field)
    check_transition(kind, current, target)
    if kind == "attempt" and entity.claim_token is not None and entity.claim_token != claim_token:
        raise ExecutionConflict("state_conflict", "Worker claim is stale")
    if kind == "attempt":
        head = await db.get(JobExecutionHead, entity.root_job_id, populate_existing=True)
        if head is not None and head.active_attempt_id != entity.id:
            raise ExecutionConflict("state_conflict", "Attempt has been superseded")
    if current == target:
        return entity
    if kind == "attempt" and target == "claimed":
        raise ExecutionConflict("state_conflict", "Use atomic claim to install worker ownership")
    if kind == "step" and current == "awaiting_confirmation" and target == "succeeded":
        raise ExecutionConflict("confirmation_required", "Complete the engineering confirmation gate first")
    if reason_code and reason_code not in ERROR_CODES:
        raise ValueError("Unknown execution reason code")
    if target in {"failed", "blocked"} and not reason_code:
        raise ValueError("Failed/blocked execution requires a reason")
    if target == "cancelled":
        reason_code = reason_code or "cancel_requested"
    if target == "interrupted":
        reason_code = reason_code or "recovery_required"
    if len(detail) > 4000:
        raise ValueError("Execution detail exceeds 4000 characters")
    if kind == "run" and target == "succeeded":
        statuses = list(await db.scalars(select(WorkflowStep.status).where(WorkflowStep.run_id == entity.id)))
        if not statuses or any(status != "succeeded" for status in statuses):
            raise ExecutionConflict("dependency_failed", "Run still has unfinished steps")
    at = now()
    values = {field:target, "version":expected_version+1, "updated_at":at,
              "reason_code" if kind == "attempt" else "error_code":reason_code, "detail":detail}
    if target in TERMINAL or target == "blocked":
        values["ended_at"] = at
    if kind == "attempt":
        if target == "running" and entity.started_at is None:
            values["started_at"] = at
        if exit_code is not None:
            values["exit_code"] = exit_code
    if kind != "step" and target in {"cancelling", "cancelled"} and entity.cancel_requested_at is None:
        values["cancel_requested_at"] = at
    model = MODELS[kind]
    await ensure_parent_transaction(db)
    async with db.begin_nested():
        seq = await reserve_case_sequence(db, case_id)
        if kind == "attempt":
            head = await db.get(JobExecutionHead, entity.root_job_id, populate_existing=True)
            if head is not None and head.active_attempt_id != entity.id:
                raise ExecutionConflict("state_conflict", "Attempt has been superseded")
        query = update(model).where(model.id == identifier, model.version == expected_version,
                                    getattr(model, field) == current)
        if kind == "attempt" and entity.claim_token is not None:
            query = query.where(model.claim_token == claim_token)
        result = await db.execute(query.values(**values))
        if result.rowcount != 1:
            raise ExecutionConflict("state_conflict", "Concurrent execution update won")
        event_type = {"run":"workflow.state_changed", "step":"step.state_changed",
                      "attempt":"attempt.state_changed"}[kind]
        await append_event(db, case_id, event_type, {"previous":current, "status":target,
            "version":expected_version+1, "reason_code":reason_code, "detail":detail,
            **({"exit_code":exit_code} if kind == "attempt" else {})}, reserved_seq=seq, **links)
    await db.refresh(entity)
    return entity
