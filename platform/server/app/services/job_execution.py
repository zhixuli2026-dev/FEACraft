"""Durable submission, exclusive launch ownership and immutable attempt lineage.

Service functions taking a session never commit. Runtime wrappers own a short
transaction. Claims are never automatically reclaimed: expired leases require
the recovery policy to establish that the old process cannot publish again.
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import socket
import uuid

from sqlalchemy import select, update

from ..models import Case, DurableEvent, Job, JobAttempt, JobExecutionHead, JobSubmissionRequest, WorkflowRun, WorkflowStep
from .durable_events import append_event, ensure_parent_transaction, json_text, lock_case_execution, now
from .workflow_state import ExecutionConflict, TERMINAL, transition
from .case_write import assert_case_available

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"[:128]


class JobAlreadyClaimed(ExecutionConflict):
    def __init__(self, job_id):
        super().__init__("state_conflict", f"Job {job_id} is already claimed or terminal")


class JobLaunchCancelled(JobAlreadyClaimed):
    """A committed cancellation won before the native release boundary."""


@dataclass(frozen=True)
class Claim:
    job_id: int
    attempt_id: str
    root_job_id: int
    number: int
    token: str


@dataclass(frozen=True)
class Completion:
    status: str
    exit_code: int | None
    ended_at: float | None
    cleanup_verified: bool | None

    def event(self):
        return {'type':'done','status':self.status,'exit_code':self.exit_code,
            'ended_at':self.ended_at,'cleanup_verified':self.cleanup_verified}


def ended_timestamp(job):
    value=job.ended_at
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp() if value else None


def completion_visibility(attempt):
    if attempt is None:return None
    try:
        output=json.loads(attempt.output_manifest_json)
        if not isinstance(output,dict):return 'invalid'
        value=output.get('completion')
    except (ValueError,TypeError):return 'invalid'
    if value is None:return None
    if (not isinstance(value,dict) or set(value)!={'schema','state','alias_error','settled_at'}
            or value['schema']!='vibeflow.job-completion.v1' or value['state'] not in {'pending','settled'}):
        return 'invalid'
    if value['alias_error'] is not None and (not isinstance(value['alias_error'],str)
            or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,79}',value['alias_error'])):return 'invalid'
    if value['state']=='pending':
        if value['settled_at'] is not None or value['alias_error'] is not None:return 'invalid'
    else:
        try:
            if not isinstance(value['settled_at'],str) or len(value['settled_at'])>64 or datetime.fromisoformat(value['settled_at']).utcoffset()!=timedelta(0):
                return 'invalid'
        except ValueError:return 'invalid'
    return value['state']


def public_status(job,attempt=None,*,local_status=None):
    if job.status=='succeeded' and completion_visibility(attempt) in {'pending','invalid'}:return 'running'
    if job.status in TERMINAL:return job.status
    return local_status if local_status is not None and local_status not in TERMINAL else job.status


def public_phase(job,attempt=None):
    """Expose the durable execution phase consistently across API owners.

    Local process lifecycle (for example ``completed``) is not the Job result.
    Incomplete/invalid completion evidence still fences a recorded success.
    """
    if job.status=='succeeded' and completion_visibility(attempt) in {'pending','invalid'}:
        return 'finalizing'
    return attempt.phase if attempt is not None else job.status


def completion_snapshot(job,attempt):
    if job.status not in TERMINAL or (job.ended_at is None and attempt is not None and attempt.process_identity_json!='{}'):
        raise ExecutionConflict('state_conflict','Job has no committed terminal result')
    cleanup=None
    if attempt is not None and attempt.process_identity_json!='{}':
        from .process_stop import public as public_stop
        from .native_child_records import public as public_children
        stop=public_stop(job,attempt);children=public_children(job,attempt)
        cleanup=bool(stop and stop['verified'] and children is not None and all(
            child['stop'] and child['stop']['verified'] for child in children['children'].values()))
    return Completion(job.status,job.exit_code,ended_timestamp(job),cleanup)


def database_exit_code(code):
    """Store native uint32 exit codes as signed int32; raw OS evidence is retained."""
    if code is None:return None
    if type(code) is not int or not -0x80000000<=code<=0xffffffff:
        raise ValueError('Native exit code is outside the supported 32-bit range')
    return code-0x100000000 if code>0x7fffffff else code


def validate_key(key):
    if key is not None and not re.fullmatch(r"[!-~]{1,128}", key):
        raise ValueError("Idempotency-Key must contain 1-128 visible ASCII characters")
    return key


def request_hash(request):
    return hashlib.sha256(json_text(request).encode("utf-8")).hexdigest()


async def lookup_submission(db, case_id, user_id, key, digest):
    validate_key(key)
    if key is None:
        return None
    row = await db.scalar(select(JobSubmissionRequest).where(
        JobSubmissionRequest.case_id == case_id, JobSubmissionRequest.created_by == user_id,
        JobSubmissionRequest.idempotency_key == key))
    if row is None:
        return None
    if row.request_hash != digest:
        raise ExecutionConflict("idempotency_conflict", "Idempotency-Key already identifies a different request")
    return await db.get(Job, row.job_id, populate_existing=True)


async def attempt_event(db, job, attempt, event_type, payload):
    links = {"attempt_id":attempt.id}
    if attempt.step_id:
        step = await db.get(WorkflowStep, attempt.step_id)
        run = await db.get(WorkflowRun, step.run_id) if step else None
        if run is None or run.case_id != job.case_id:
            raise ExecutionConflict("invalid_request", "Attempt crosses workflow ownership")
        links.update(step_id=step.id, run_id=run.id)
    return await append_event(db, job.case_id, event_type, payload, **links)


async def _new_attempt(db, job, spec, *, head=None, previous=None, step_id=None, submission_digest=None, attempt_id=None):
    if head is None:
        head = JobExecutionHead(job_id=job.id, next_number=1, version=0)
        db.add(head)
        await db.flush()
    from .execution_storage import policy as storage_policy
    document={"execution_spec":spec,"storage_policy":storage_policy(previous)}
    if spec.get('runtime') == 'windows':
        from .execution_budget import linux_compute_policy, windows_compute_policy
        document['windows_compute_policy'] = windows_compute_policy(previous)
        document['linux_compute_policy'] = linux_compute_policy(previous)
    runtime=spec.get('runtime')
    from .native_execution import NativeExecutor
    native_command=(spec.get('argv') is None and (runtime=='wsl' or (runtime=='wsl-sandbox' and job.type=='shell')))
    native_tool=runtime=='windows' and NativeExecutor.applies(spec.get('argv')) and NativeExecutor.uses_wsl(spec['argv'])
    if native_command or native_tool:
        from .execution_budget import native_compute_policy
        document['native_compute_policy'] = native_compute_policy(previous)
    if submission_digest is not None:
        document['submission_request_hash']=submission_digest
    attempt = JobAttempt(id=attempt_id or str(uuid.uuid4()), job_id=job.id, root_job_id=head.job_id,
        number=head.next_number, retry_of_attempt_id=previous.id if previous else None, step_id=step_id,
        input_manifest_json=json_text(document))
    db.add(attempt)
    await db.flush()
    head.next_number += 1
    head.version += 1
    head.active_attempt_id = attempt.id
    await db.flush()
    await attempt_event(db, job, attempt, "attempt.created", {"job_id":job.id,
        "root_job_id":head.job_id, "number":attempt.number, "status":"queued",
        "retry_of_attempt_id":attempt.retry_of_attempt_id})
    return attempt


async def submit(db, *, case_id, user_id, key, digest, job_type, command, spec, step_id=None,
                 input_paths=None, input_publications=None, spec_factory=None, isolate_default=False,
                 generated_inputs=None, staged_remote_command=None, staged_workspace=None):
    """Return (Job, created). Persist request + Job + attempt + event atomically."""
    validate_key(key)
    await ensure_parent_transaction(db)
    async with db.begin_nested():
        await lock_case_execution(db, case_id)
        case = await db.get(Case, case_id, populate_existing=True)
        assert_case_available(case)
        replay = await lookup_submission(db, case_id, user_id, key, digest)
        if replay is not None:
            return replay, False
        from .case_command_handler import uses_default_inputs
        typed_default = isolate_default and uses_default_inputs(job_type,spec,input_paths,input_publications)
        default_workspace = (isolate_default and job_type == 'shell' and input_paths is None and input_publications is None
            and not spec.get('evidence_run_dir'))
        local_command = getattr(staged_workspace, 'case_command', None)
        if staged_remote_command is not None:
            if (staged_workspace is not None or not isolate_default or spec_factory is not None or generated_inputs
                    or not uses_default_inputs(job_type, spec, input_paths, input_publications)):
                raise ValueError('Prepared command requires its original default-input request')
            await staged_remote_command.command.verify(case, job_type, spec)
        if staged_workspace is not None:
            if spec_factory is not None or typed_default != (local_command is not None):
                raise ValueError('Prepared workspace requires its original input request')
            from .case_validation import assert_writable_locked

            await assert_writable_locked(db,case.id)
            if local_command is not None:
                if (local_command.job_type, local_command.spec, local_command.descriptor['case_id']) != (job_type,spec,case.id):
                    raise ValueError('Prepared local command differs from its original request')
                input_paths = local_command.input_paths
            staged_workspace.require_submission(case,spec,input_paths,input_publications,generated_inputs,
                                                all_live=default_workspace)
        job = Job(case_id=case_id, type=job_type, command=command, status="queued")
        db.add(job)
        await db.flush()
        if spec_factory is not None:
            # Legacy executors embed the allocated Job ID in their argv/config.
            # Build once, after the idempotency lookup, inside this transaction.
            spec = spec_factory(job.id)
            job.command = spec['command']
        job.log_s3_key = f".vibeflow/jobs/job-{job.id}.log"
        staged = staged_remote_command or staged_workspace
        attempt_id = staged.id if staged is not None else None
        attempt = await _new_attempt(db, job, spec, step_id=step_id, submission_digest=digest, attempt_id=attempt_id)
        prepared_command = local_command
        if staged_remote_command is not None:
            from .command_input_seal import bind_prepared
            prepared_command = staged_remote_command.command.step
            manifest = await bind_prepared(db, case, attempt, staged_remote_command)
            await staged_remote_command.command.verify_seal(case, manifest)
        elif typed_default and local_command is None:
            from ..models import User
            from .step_handlers import get_handler
            prepared_command = await get_handler('command.'+job_type).prepare(db,case,await db.get(User,user_id),
                parameters={'spec':spec},budget={'timeout_seconds':spec['timeout_seconds'],'max_log_bytes':spec['max_log_bytes']})
            input_paths = prepared_command.input_paths
        if staged_workspace is not None:
            await staged_workspace.bind_submission(db,case,attempt)
        elif input_paths is not None or input_publications is not None or default_workspace or generated_inputs:
            from ..models import User
            from .attempt_workspace import seal
            if spec.get("evidence_run_dir") and prepared_command is None:
                raise ValueError("Isolated solve evidence requires the versioned professional handler")
            await seal(db, case, attempt, await db.get(User, user_id), input_paths,
                input_publications=input_publications, all_live=default_workspace,generated_inputs=generated_inputs)
        db.add(JobSubmissionRequest(case_id=case_id, created_by=user_id, idempotency_key=key,
            request_hash=digest, job_id=job.id, execution_spec_json=json_text(spec)))
        await db.flush()
        if prepared_command is not None:
            from .step_handlers import bind
            await bind(db,case,job,attempt,prepared_command,deliver_intent=False)
        from . import audit
        await audit.case_event(db, case, user_id, 'job.submitted', 'job', job.id,
            {'job_type': job_type, 'runtime': spec.get('runtime'),
             'command_sha256': hashlib.sha256(job.command.encode()).hexdigest()})
    return job, True


def require_retry_head(job,previous,head):
    if (head is None or head.active_attempt_id!=previous.id or previous.phase not in
            {'failed','cancelled','interrupted'} or job.status!=previous.phase):
        raise ExecutionConflict('state_conflict','Retry requires the current terminal attempt')


async def allocate_retry(db, previous_id, *, registered=False, user_id=None, key=None, digest=None,continuation=None,
                         step_id=None,staged_workspace=None):
    """Allocate only; never replay or dispatch automatically.

    Plain shell is the default. Registered requests must first pass the explicit
    repeat operation's source, drain and handler-specific recovery gates.
    """
    previous = await db.get(JobAttempt, previous_id)
    if previous is None:
        raise ExecutionConflict("invalid_request", "Attempt does not exist")
    job = await db.get(Job, previous.job_id)
    await ensure_parent_transaction(db)
    async with db.begin_nested():
        await lock_case_execution(db, job.case_id)
        assert_case_available(await db.get(Case, job.case_id, populate_existing=True))
        await db.refresh(previous)
        await db.refresh(job)
        head = await db.get(JobExecutionHead, previous.root_job_id, populate_existing=True)
        submission = await db.scalar(select(JobSubmissionRequest).where(JobSubmissionRequest.job_id == job.id))
        require_retry_head(job,previous,head)
        spec = json.loads(previous.input_manifest_json).get("execution_spec", {})
        from .step_handlers import validate_binding
        handler=validate_binding(job,previous)
        if (submission is None or (not registered and (job.type!="shell" or spec.get('evidence_run_dir') or previous.step_id))
                or (registered and handler is None and (job.type!='shell' or previous.step_id or spec.get('evidence_run_dir')))):
            raise ExecutionConflict("recovery_required", "Handler-specific retry validation is required")
        prepared,checkpoint=continuation if continuation is not None else (None,None)
        if prepared is not None:
            if not registered or handler is None or handler.key not in {'solve.openfoam','solve.openfoam.resume'} or prepared.key!='solve.openfoam.resume':
                raise ExecutionConflict('invalid_request','Continuation requires a registered OpenFOAM checkpoint')
            spec=prepared.spec
        if step_id is not None:
            step = await db.get(WorkflowStep, step_id, populate_existing=True)
            run = await db.get(WorkflowRun, step.run_id, populate_existing=True) if step else None
            target_handler = prepared if prepared is not None else handler
            if (run is None or run.case_id != job.case_id or run.status != 'running'
                    or step.status != 'ready' or step.active_attempt_id is not None or target_handler is None
                    or (step.handler, step.handler_version) != (target_handler.key, target_handler.version)):
                raise ExecutionConflict('evidence_invalid', 'Retry must belong to its ready registered workflow step')
        if staged_workspace is not None:
            original=json.loads(previous.input_manifest_json).get('workspace')
            checkpoint_sha=prepared.descriptor['checkpoint_sha256'] if prepared is not None else None
            if (original is None or staged_workspace.retry_source!=original
                    or getattr(staged_workspace,'checkpoint_sha256',None)!=checkpoint_sha):
                raise ExecutionConflict('evidence_invalid','Prepared retry no longer matches its original inputs')
            if prepared is not None:
                from .checkpoint_seal import require_parent
                require_parent(job.case_id,previous,checkpoint)
        retry = Job(case_id=job.case_id, type=prepared.job_type if prepared else job.type,
            command=spec['command'], status="queued")
        db.add(retry)
        await db.flush()
        retry.log_s3_key = f".vibeflow/jobs/job-{retry.id}.log"
        attempt = await _new_attempt(db, retry, spec, head=head, previous=previous,
            submission_digest=digest or submission.request_hash, step_id=step_id,
            attempt_id=staged_workspace.id if staged_workspace is not None else None)
        if staged_workspace is not None:
            from .case_validation import assert_writable_locked
            await assert_writable_locked(db,job.case_id)
            await staged_workspace.bind_retry(db,await db.get(Case,job.case_id),attempt)
        elif json.loads(previous.input_manifest_json).get("workspace"):
            from ..models import User
            from .attempt_workspace import seal
            await seal(db, await db.get(Case, job.case_id), attempt,
                await db.get(User, submission.created_by), None, previous=previous,
                generated_inputs=prepared.generated_inputs if prepared else None,resume=checkpoint,
                resume_view=prepared.source_view if prepared else None)
        if handler is not None and prepared is None:
            from .artifact_publication import reserve
            original=json.loads(previous.input_manifest_json)
            publication=json.loads(previous.publication_json)
            await reserve(db,await db.get(Case,job.case_id),attempt,kind=original['handler']['publication_kind'],
                descriptor=publication['descriptor'])
            document=json.loads(attempt.input_manifest_json);document['handler']=original['handler']
            attempt.input_manifest_json=json_text(document)
            validate_binding(retry,attempt)
        db.add(JobSubmissionRequest(case_id=job.case_id, created_by=user_id or submission.created_by,
            idempotency_key=key,request_hash=digest or submission.request_hash,job_id=retry.id,execution_spec_json=json_text(spec)))
        await db.flush()
        if prepared is not None:
            from .step_handlers import bind
            await bind(db,await db.get(Case,job.case_id),retry,attempt,prepared,deliver_intent=False)
    return retry, attempt


async def claim(db, job_id, spec):
    job = await db.get(Job, job_id)
    if job is None:
        raise LookupError(f"Job {job_id} does not exist")
    await ensure_parent_transaction(db)
    async with db.begin_nested():
        await lock_case_execution(db, job.case_id)
        await db.refresh(job)
        if job.status != "queued":
            raise JobAlreadyClaimed(job_id)
        if job.command != spec["command"]:
            raise ExecutionConflict("invalid_request", "Delivery command differs from the durable Job")
        case = await db.get(Case, job.case_id, populate_existing=True)
        assert_case_available(case)
        from .case_validation import assert_writable_locked
        from .case_write import CaseWriteConflict
        try:
            await assert_writable_locked(db, case.id)
        except CaseWriteConflict as error:
            raise ExecutionConflict(error.code, str(error)) from error
        if case.s3_prefix != spec["case_prefix"]:
            raise ExecutionConflict("invalid_request", "Execution directory belongs to another case")
        submission = await db.scalar(select(JobSubmissionRequest).where(JobSubmissionRequest.job_id == job_id))
        if submission is not None and json.loads(submission.execution_spec_json) != spec:
            raise ExecutionConflict("invalid_request", "Delivery differs from the durable execution specification")
        attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id)
            .order_by(JobAttempt.number.desc()).limit(1).execution_options(populate_existing=True))
        if attempt is None:
            attempt = await _new_attempt(db, job, spec)
        from .step_handlers import require_binding
        await require_binding(db, job, attempt)
        head = await db.get(JobExecutionHead, attempt.root_job_id, populate_existing=True)
        if head is None or head.active_attempt_id != attempt.id or attempt.phase != "queued":
            raise JobAlreadyClaimed(job_id)
        if attempt.step_id:
            step = await db.get(WorkflowStep, attempt.step_id, populate_existing=True)
            run = await db.get(WorkflowRun, step.run_id, populate_existing=True) if step else None
            if (run is None or run.case_id != job.case_id or run.status != "running" or
                    run.cancel_requested_at is not None or step.status != "running" or
                    step.active_attempt_id != attempt.id):
                raise JobAlreadyClaimed(job_id)
        from .job_delivery import require_capacity
        await require_capacity(db,case,job,attempt,spec)
        token, at, version = uuid.uuid4().hex, now(), attempt.version+1
        from ..config import get_settings
        from .job_worker_identity import capture
        worker_identity = await asyncio.to_thread(capture, job, attempt, WORKER_ID, token)
        result = await db.execute(update(JobAttempt).where(JobAttempt.id == attempt.id,
            JobAttempt.phase == "queued", JobAttempt.version == attempt.version,
            JobAttempt.claim_token.is_(None), JobAttempt.cancel_requested_at.is_(None))
            .values(phase="claimed", version=version, claim_token=token,
                worker_id=WORKER_ID, worker_identity_json=json_text(worker_identity),
                heartbeat_at=at, lease_expires_at=at+timedelta(seconds=get_settings().job_lease_seconds)))
        if result.rowcount != 1:
            raise JobAlreadyClaimed(job_id)
        await attempt_event(db, job, attempt, "attempt.state_changed", {"previous":"queued",
            "status":"claimed", "job_id":job.id, "number":attempt.number,
            "version":version})
        return Claim(job.id, attempt.id, attempt.root_job_id, attempt.number, token)


async def claim_job(job_id, spec):
    from ..db import SessionLocal
    async with SessionLocal() as db:
        owner = await claim(db, job_id, spec)
        await db.commit()  # Must precede staging, registry mutation and native spawn.
        return owner


async def _owned(db, owner):
    job = await db.get(Job, owner.job_id)
    if job is None:
        raise ExecutionConflict("state_conflict", "Owned job was removed")
    await lock_case_execution(db, job.case_id)
    await db.refresh(job)
    attempt = await db.get(JobAttempt, owner.attempt_id, populate_existing=True)
    head = await db.get(JobExecutionHead, owner.root_job_id, populate_existing=True)
    if (attempt is None or head is None or head.active_attempt_id != owner.attempt_id or
            attempt.job_id != job.id or attempt.root_job_id != owner.root_job_id or
            attempt.claim_token != owner.token):
        raise ExecutionConflict("state_conflict", "Worker ownership is stale")
    if attempt.step_id:
        step = await db.get(WorkflowStep, attempt.step_id, populate_existing=True)
        if step is None or step.active_attempt_id != attempt.id:
            raise ExecutionConflict("state_conflict", "Workflow step ownership is stale")
    return job, attempt


async def _move(db, attempt, target, owner, *, reason="", code=None):
    if target == "cancelled" and attempt.phase not in {"queued", "claimed", "cancelling", "cancelled"}:
        await transition(db, "attempt", attempt.id, "cancelling", expected_version=attempt.version,
            claim_token=owner.token)
    return await transition(db, "attempt", attempt.id, target, expected_version=attempt.version,
        claim_token=owner.token, reason_code=reason, exit_code=code)


async def _job_status(db, job, status, code, attempt):
    if job.status == status:
        return
    allowed = {"queued"} if status == "running" else {"queued", "running"}
    result = await db.execute(update(Job).where(Job.id == job.id, Job.status.in_(allowed))
        .values(status=status, exit_code=code, **({"ended_at":now()} if status in TERMINAL else {})))
    if result.rowcount != 1:
        raise ExecutionConflict("state_conflict", "Job completion lost ownership")
    await attempt_event(db, job, attempt, "jobs.updated", {"job_id":job.id, "case_id":job.case_id,
        "status":status, "exit_code":code})


async def reconcile_workflow(db, attempt):
    if attempt.step_id:
        from .workflow_scheduler import reconcile
        step = await db.get(WorkflowStep, attempt.step_id)
        await reconcile(db, step.run_id)


async def phase(owner, target, *, identity=None, compute_usage=None):
    from ..db import SessionLocal, engine
    if target == 'finalizing' and engine.dialect.name == 'sqlite':
        from .storage_admission import sqlite_settlement_session
        transaction = sqlite_settlement_session()
    else:
        transaction = SessionLocal()
    async with transaction as db:
        job, attempt = await _owned(db, owner)
        if compute_usage is not None:
            if target != 'finalizing':
                raise ValueError('Compute usage requires the finalizing boundary')
            from .execution_budget import WINDOWS_COMPUTE_USAGE
            from .linux_gate_compute import CONTROL as LINUX_COMPUTE_USAGE, SCHEMA as LINUX_COMPUTE_SCHEMA, validate_report
            event_type = WINDOWS_COMPUTE_USAGE
            if compute_usage.get('schema') == LINUX_COMPUTE_SCHEMA:
                process = json.loads(attempt.process_identity_json)
                policy = json.loads(attempt.input_manifest_json)['linux_compute_policy']
                if process['host']['platform'] != 'linux':
                    raise ValueError('Linux compute usage requires the original Linux process')
                validate_report(compute_usage,process['launch_nonce'],policy)
                event_type = LINUX_COMPUTE_USAGE
            previous = await db.scalar(select(DurableEvent.payload_json).where(DurableEvent.attempt_id == attempt.id,
                DurableEvent.type == event_type))
            if previous is None:
                await attempt_event(db, job, attempt, event_type, compute_usage)
            elif json.loads(previous) != compute_usage:
                raise ValueError('Compute usage cannot replace an earlier observation')
        if target in {'starting', 'running'}:
            from .job_write_checkpoint import assert_native_open_locked
            await assert_native_open_locked(db, job, attempt)
        if job.status in TERMINAL:
            # Called after native exit: no success hook may run after durable cancellation.
            if target == "finalizing":
                if job.status == 'cancelled' and attempt.phase == 'cancelling':
                    # The cancellation request is durable, but output capture,
                    # log upload and the write-drain checkpoint still belong to
                    # this worker. Its final commit ends the attempt and run.
                    if compute_usage is not None:
                        await db.commit()
                    return job.status
                await _move(db, attempt, job.status, owner,
                    reason="process_exit_failed" if job.status == "failed" else "", code=job.exit_code)
                await reconcile_workflow(db, attempt)
                await db.commit()
                return job.status
            raise JobAlreadyClaimed(job.id)
        if target=='running':
            from . import process_record,owned_process
            if attempt.worker_id!=WORKER_ID:
                raise ExecutionConflict('state_conflict','Process registration belongs to another worker')
            try:
                bound=process_record.bind(identity,job,attempt,WORKER_ID)
                from .job_launch_registration import require_prepared
                await require_prepared(db,job,attempt,bound)
                spec=json.loads(attempt.input_manifest_json)['execution_spec']
                if (bound['runtime']!=spec['runtime'] or
                        bound['gate_sha256']!=hashlib.sha256(owned_process.GATE.read_bytes()).hexdigest() or
                        bound['identity_probe_sha256']!=hashlib.sha256(owned_process.PROBE.read_bytes()).hexdigest()):
                    raise ValueError('Process launch code or runtime changed before registration')
                argv=spec.get('argv')
                if argv:
                    _,distribution,nonroot=owned_process.native_payload(argv,bound['launch_nonce'])
                    if distribution is not None and (bound['native'] is None or bound['native']['distribution']!=distribution):
                        raise ValueError('The requested native distribution was not registered')
                    if nonroot and bound['native']['process']['uid']==0:
                        raise ValueError('The requested unprivileged native identity was not registered')
            except (ValueError,KeyError,TypeError,AttributeError) as error:
                raise ExecutionConflict('evidence_invalid','Process identity is invalid for this attempt') from error
            previous=json.loads(attempt.process_identity_json)
            if previous:
                if previous!=bound or attempt.phase!='running':
                    raise ExecutionConflict('state_conflict','Attempt already has a different process registration')
                return 'running'
            if attempt.phase!='starting':
                raise ExecutionConflict('state_conflict','Process registration requires the starting phase')
            from . import process_identity_native
            if (await asyncio.to_thread(process_identity_native.probe,bound['host']))['state']!='match':
                raise ExecutionConflict('evidence_invalid','Host process birth identity no longer matches')
            if bound['native'] is not None:
                native=bound['native']
                if (await owned_process.probe_native(native['distribution'],native['process']))['state']!='match':
                    raise ExecutionConflict('evidence_invalid','Native process birth identity no longer matches')
            identity=bound
        elif identity is not None:
            raise ExecutionConflict('invalid_request','Process identity is registered only at the running boundary')
        await _move(db, attempt, target, owner)
        if identity is not None:
            attempt.process_identity_json = json_text(identity)
        if target == "running":
            await _job_status(db, job, "running", None, attempt)
        if target == "finalizing":
            await reconcile_workflow(db, attempt)
        await db.commit()
        return target


async def release_process(owner, process):
    """Serialize the final release with durable cancellation on the same case.

    Birth verification may involve native reads, so do it before taking the
    write lock. Only the bounded control-pipe release occurs under that lock.
    A failed commit after release must stop the owned scope; it cannot justify
    another launch. JobProcess.start owns that exceptional cleanup.
    """
    from ..db import SessionLocal
    from .process_record import bind
    await process.verify_registration()
    async with SessionLocal() as db:
        job, attempt = await _owned(db, owner)
        if job.status == 'cancelled' or attempt.cancel_requested_at is not None:
            raise JobLaunchCancelled(job.id)
        from .job_write_checkpoint import assert_native_open_locked
        await assert_native_open_locked(db, job, attempt)
        if (job.status != 'running' or attempt.phase != 'running'
                or attempt.worker_id != WORKER_ID):
            raise JobAlreadyClaimed(job.id)
        expires = attempt.lease_expires_at
        if expires is None or (expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)) <= now():
            raise ExecutionConflict('state_conflict', 'Launch ownership expired before process release')
        if json.loads(attempt.process_identity_json) != bind(process.record, job, attempt, WORKER_ID):
            raise ExecutionConflict('evidence_invalid', 'Registered process changed before release')
        await asyncio.wait_for(process.release(), timeout=5)
        await db.commit()


async def finish(owner, status, code=None, *, reason="",process_stop=None,return_result=False,check_success=None,
                 content_drained=False, not_started=False):
    from . import execution_budget
    budget=execution_budget.current(owner)
    if budget is not None and budget.storage_watch is not None:await budget.storage_watch.sample()
    status,code,reason=execution_budget.failure(owner,status,code,reason)
    from .artifact_publication import prepare_for_finish, prepare_bundle_for_finish
    prepared = await prepare_for_finish(owner, status, process_stop)
    from .native_execution import drain_finalizers
    await drain_finalizers()  # A broken local socket cannot outlive the parent terminal commit.
    status,code,reason=execution_budget.failure(owner,status,code,reason)
    prepared = await prepare_bundle_for_finish(owner, prepared,
        (status,code,process_stop) if content_drained else None)
    if content_drained:
        from .job_write_checkpoint import record_drained
        drained = await record_drained(owner, process_stop, not_started=not_started)
        if drained and status in {'cancelled', 'failed', 'interrupted'} and not not_started:
            from .latex_workspace_cleanup import retire
            await retire(owner.job_id, owner=owner)
        from .publication_finalizing import record as record_finalization
        await record_finalization(owner,prepared,status,code,process_stop)
    from .publication_source_validation import JobHistoryRecoveryRequired,for_finish
    try:
        async with for_finish(owner,status,prepared) as (validation,prepared):
            return await _finish_owned(owner,status,code,reason=reason,process_stop=process_stop,
                return_result=return_result,check_success=check_success,prepared=prepared,validation=validation)
    except JobHistoryRecoveryRequired as error:
        # Source validation has returned permanently. Commit the stopped Job
        # without publishing and retain the historical writer's recovery gate.
        return await _finish_owned(owner,'failed',-1,reason=error.code,process_stop=process_stop,
            return_result=return_result,check_success=None,prepared=prepared,validation=None)
    except execution_budget.BudgetExceeded:
        # The success transaction rolled back, and source-validation exclusion
        # was released only after its readers drained. Commit the resource failure.
        async with for_finish(owner,'failed',prepared) as (validation,prepared):
            return await _finish_owned(owner,'failed',-1,reason='resource_limit',process_stop=process_stop,
                return_result=return_result,check_success=None,prepared=prepared,validation=validation)


async def _finish_owned(owner,status,code,*,reason,process_stop,return_result,check_success,prepared,validation,recovery=None):
    from ..db import SessionLocal
    from . import execution_budget
    budget = execution_budget.current(owner)
    # The last disk scan can block in the filesystem. Keep source validation
    # alive while it runs, and drain lease renewals before taking the write lock.
    if budget is not None and budget.storage_watch is not None:
        await budget.storage_watch.sample()
    if validation is not None:
        await validation.stop_renewal()
    async with SessionLocal() as db:
        job, attempt = await _owned(db, owner)
        if recovery is not None:
            from .publication_finalizing import require_recovery
            await require_recovery(db,job,attempt,recovery,status,reason)
        status = job.status if job.status in TERMINAL else status
        code = database_exit_code(job.exit_code if job.status in TERMINAL else code)
        if job.status not in TERMINAL:status,code,reason=execution_budget.failure(owner,status,code,reason)
        if process_stop is not None:
            from .process_record import for_attempt
            from .process_stop import validate as validate_stop
            identity=for_attempt(json.loads(attempt.process_identity_json),job,attempt)
            validate_stop(process_stop,identity)
            if status=='succeeded' and not process_stop['verified']:
                status,reason='failed','process_cleanup_failed'
        if status == 'succeeded' and attempt.phase not in TERMINAL:
            from .step_handlers import require_binding
            try:
                await require_binding(db, job, attempt)
            except ExecutionConflict as error:
                status, reason = 'failed', error.code
        if status == 'succeeded':
            from .native_child_records import require_stopped
            try:
                require_stopped(job,attempt)
            except (ValueError,TypeError,KeyError,AttributeError):
                status,reason='failed','process_cleanup_failed'
        from .artifact_publication import finalize
        if status == 'succeeded' and prepared is not None and prepared.error is not None:
            # Persist the original publication failure before dependency checks;
            # failed output requires no authority to read/publish live sources.
            status, reason = await finalize(db, job, attempt, status, reason, prepared, validation)
        if status == 'succeeded' and attempt.phase not in TERMINAL:
            if validation is not None:
                case=await db.get(Case,job.case_id,populate_existing=True)
                await validation.require_current(db,job,attempt,case,prepared)
            from .publication_inputs import PublicationInputStale
            manifest = json.loads(attempt.input_manifest_json).get('workspace')
            if manifest:
                case = await db.get(Case, job.case_id, populate_existing=True)
                try:
                    if validation is None:
                        raise PublicationInputStale('发布依赖没有完成来源校验')
                    await validation.require_dependencies(db)
                except PublicationInputStale:
                    status, reason = 'failed', 'input_stale'
                    output = json.loads(attempt.output_manifest_json)
                    if output.get('publications'):
                        output['dependency_error'] = 'input_stale'
                        attempt.output_manifest_json = json_text(output)
                    await attempt_event(db, job, attempt, 'attempt.dependency_stale' if manifest.get('publications') else 'attempt.input_stale',
                        {'reason':'input_stale'})
        status, reason = await finalize(db, job, attempt, status, reason, prepared, validation)
        if process_stop is not None:
            output=json.loads(attempt.output_manifest_json)
            previous=output.get('process_stop')
            if previous is not None and previous!=process_stop:
                raise ExecutionConflict('state_conflict','Process cleanup was already recorded differently')
            if previous is None:
                output['process_stop']=process_stop
                attempt.output_manifest_json=json_text(output)
                await attempt_event(db,job,attempt,'attempt.process_stopped',process_stop)
        if status == 'failed' and code == 0:
            code = -1
        visibility=completion_visibility(attempt)
        if status=='succeeded' and attempt.publication_json!='{}' and attempt.phase not in TERMINAL and visibility is None:
            output=json.loads(attempt.output_manifest_json)
            output['completion']={'schema':'vibeflow.job-completion.v1','state':'pending','alias_error':None,'settled_at':None}
            attempt.output_manifest_json=json_text(output)
            visibility='pending'
        await _move(db, attempt, status, owner,
            reason=(reason or "process_exit_failed") if status == "failed" else "", code=code)
        await _job_status(db, job, status, code, attempt)
        if status == 'failed':
            from .workflow_fallback import record_required
            await record_required(db, job, attempt)
        await reconcile_workflow(db, attempt)
        if status=='succeeded':
            if budget is not None:budget.check()
            if check_success is not None:
                check_success()  # A local ownership failure during preparation must roll back success.
        if validation is not None:
            await validation.release_locked(db,status)
        if status=='failed' and reason=='process_cleanup_failed' and attempt.write_checkpoint_json=='{}':
            from . import job_recovery_notice as notices
            await db.flush()
            await notices.record(db,await notices.rows(db,job.id),'process_cleanup_unverified')
        await execution_budget.record_usage(db,job,attempt,owner)
        if status=='succeeded' and budget is not None:budget.check()
        await db.commit()
        if budget is not None:budget.committed=True
        if validation is not None:
            validation.released=True
        # Capture before optional alias/fallback work can roll back and expire
        # ORM state. This is the exact terminal transaction, without a reread.
        completed=completion_snapshot(job,attempt) if return_result else None
        if status == 'succeeded' and attempt.publication_json != '{}':
            from .artifact_publication import repair_aliases
            alias_error=None
            try:
                await repair_aliases(owner)
            except Exception as error:
                alias_error=type(error).__name__
                # The committed immutable bundle remains authoritative. Never
                # turn a successful committed publication into a false rollback.
                detail=getattr(error,'detail',None)
                error_code=getattr(error,'code',None) or (detail.get('code') if isinstance(detail,dict) else None)
                if not isinstance(error_code,str) or not re.fullmatch('[a-z][a-z0-9_]{0,79}',error_code):error_code=None
                http_status=getattr(error,'status_code',None)
                if type(http_status) is not int or not 400<=http_status<=599:http_status=None
                print(f'[WARN] Job #{job.id} alias repair: {type(error).__name__} code={error_code} status={http_status}')
                try:
                    await attempt_event(db, job, attempt, 'artifact.alias_repair_required',
                        {'reason':type(error).__name__,'code':error_code,'http_status':http_status})
                    await db.commit()
                except Exception as notice_error:
                    await db.rollback()
                    print(f'[WARN] Job #{owner.job_id} alias repair notice: {type(notice_error).__name__}')
            if visibility=='pending':await settle_visibility(owner,alias_error)
        if status == "failed":
            from .workflow_fallback import settle_failure_fallback
            try:
                case = await db.get(Case, job.case_id)
                await settle_failure_fallback(db, case, job)
            except Exception as error:
                await db.rollback()
                print(f"[WARN] Job #{owner.job_id} fallback: {type(error).__name__}")
        return completed if return_result else status


async def committed_result(owner):
    """Resolve an exceptional late callback against its still-bound owner."""
    from ..db import SessionLocal
    async with SessionLocal() as db:
        job,attempt=await _owned(db,owner)
        if completion_visibility(attempt) in {'pending','invalid'}:
            raise ExecutionConflict('recovery_required','Job compatibility completion is not settled')
        return completion_snapshot(job,attempt)


async def settle_visibility(owner,alias_error):
    """Persist that compatibility repair was attempted, retaining the real result."""
    from ..db import SessionLocal
    async with SessionLocal() as db:
        job,attempt=await _owned(db,owner)
        state=completion_visibility(attempt)
        if state=='settled':return
        if job.status!='succeeded' or state!='pending':
            raise ExecutionConflict('state_conflict','Job compatibility completion no longer matches')
        output=json.loads(attempt.output_manifest_json)
        output['completion'].update(state='settled',alias_error=alias_error,settled_at=now().isoformat())
        attempt.output_manifest_json=json_text(output)
        await attempt_event(db,job,attempt,'attempt.completion_visible',{'alias_error':alias_error})
        await db.commit()


async def heartbeat(owner):
    from ..db import SessionLocal
    from ..config import get_settings
    async with SessionLocal() as db:
        job, attempt = await _owned(db, owner)
        if attempt.phase not in TERMINAL or completion_visibility(attempt)=='pending':
            attempt.heartbeat_at = now()
            attempt.lease_expires_at = now() + timedelta(seconds=get_settings().job_lease_seconds)
        await db.commit()
        return job.status


async def record_external_terminal(db, job, status):
    """Cancellation/recovery command; running process completion remains owner-only."""
    attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id)
        .order_by(JobAttempt.number.desc()).limit(1).execution_options(populate_existing=True))
    if attempt is None or attempt.phase in TERMINAL:
        return
    if status not in {"cancelled", "interrupted"}:
        raise ExecutionConflict("state_conflict", "Claimed execution must complete through its owner")
    target = status
    if status == "cancelled" and attempt.phase not in {"queued", "claimed"}:
        target = "cancelling"
    await transition(db, "attempt", attempt.id, target, expected_version=attempt.version,
        claim_token=attempt.claim_token)


async def request_cancel(db, job_id):
    """Durable cancellation in the caller's transaction; wakeup is optional."""
    job = await db.get(Job, job_id)
    if job is None:
        raise ExecutionConflict("invalid_request", "Job does not exist")
    await lock_case_execution(db, job.case_id)
    await db.refresh(job)
    if job.status in TERMINAL:
        return job.status
    await record_external_terminal(db, job, "cancelled")
    attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id)
        .order_by(JobAttempt.number.desc()).limit(1))
    if attempt is None:
        raise ExecutionConflict("invalid_request", "Workflow Job has no attempt")
    await _job_status(db, job, "cancelled", None, attempt)
    return "cancelled"
