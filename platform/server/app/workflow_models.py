"""Durable execution control records; case files remain engineering source of truth.

New records are additive to Job. Pointer ownership and cross-entity case checks
are enforced by the execution service; active_attempt_id intentionally has no
circular FK back from a step to its attempts.
"""
from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, false, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


def new_id():
    return str(uuid.uuid4())


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
        server_default=func.now(), onupdate=utcnow)


class WorkflowRun(Timestamps, Base):
    __tablename__ = "workflow_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), nullable=False)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    target: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="queued", server_default="queued")
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    request_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expected_revision: Mapped[int] = mapped_column(Integer)
    run_dir: Mapped[str] = mapped_column(String(500))
    plan_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    input_manifest_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    budget_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), default="", server_default="")
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("case_id", "created_by", "idempotency_key", name="uq_workflow_run_idempotency"),
        CheckConstraint("version >= 0 AND expected_revision >= 0", name="ck_workflow_run_versions"),
        CheckConstraint("target IN ('geometry','mesh','solve','postprocess','report')", name="ck_workflow_run_target"),
        CheckConstraint("status IN ('queued','running','awaiting_confirmation','cancelling','succeeded','failed','cancelled','interrupted')",
                        name="ck_workflow_run_status"),
        Index("ix_workflow_runs_case_created", "case_id", "created_at"),
        Index("ix_workflow_runs_status", "status"),
        Index('uq_workflow_run_case', 'id', 'case_id', unique=True),
    )


class WorkflowStep(Timestamps, Base):
    __tablename__ = "workflow_steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False, index=True)
    step_key: Mapped[str] = mapped_column(String(80))
    handler: Mapped[str] = mapped_column(String(80))
    handler_version: Mapped[str] = mapped_column(String(30), default="1", server_default="1")
    status: Mapped[str] = mapped_column(String(30), default="pending", server_default="pending")
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    dependencies_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    confirmation_state: Mapped[str | None] = mapped_column(String(1), nullable=True)
    confirmation_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_manifest_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    output_manifest_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    active_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), default="", server_default="")
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("run_id", "step_key", name="uq_workflow_step_key"),
        CheckConstraint("version >= 0", name="ck_workflow_step_version"),
        CheckConstraint("confirmation_revision IS NULL OR confirmation_revision >= 0", name="ck_step_confirmation_revision"),
        CheckConstraint("confirmation_state IS NULL OR confirmation_state IN ('B','C','D','E')", name="ck_step_confirmation_state"),
        CheckConstraint("status IN ('pending','ready','running','awaiting_confirmation','finalizing','cancelling','succeeded','failed','cancelled','interrupted','blocked')",
                        name="ck_workflow_step_status"),
    )


class JobAttempt(Timestamps, Base):
    __tablename__ = "job_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    root_job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False,
        default=lambda context: context.get_current_parameters()["job_id"])
    retry_of_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("job_attempts.id"), nullable=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_steps.id"), nullable=True, index=True)
    number: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(30), default="queued", server_default="queued")
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    worker_identity_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    process_identity_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    child_processes_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    write_checkpoint_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason_code: Mapped[str] = mapped_column(String(64), default="", server_default="")
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")
    input_manifest_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    output_manifest_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    publication_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("job_id", "number", name="uq_job_attempt_number"),
        UniqueConstraint("root_job_id", "number", name="uq_job_attempt_lineage_number"),
        Index('uq_attempt_job_identity', 'id', 'job_id', unique=True),
        CheckConstraint("number > 0 AND version >= 0", name="ck_job_attempt_versions"),
        CheckConstraint("phase IN ('queued','claimed','starting','running','finalizing','cancelling','succeeded','failed','cancelled','interrupted')",
                        name="ck_job_attempt_phase"),
        Index("ix_job_attempt_worker_lease", "worker_id", "lease_expires_at"),
    )


class CaseValidationFence(Base):
    """One revocable read-validation reservation per case, not a native-process lease."""
    __tablename__ = 'case_validation_fences'
    case_id: Mapped[int] = mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer)
    attempt_id: Mapped[str] = mapped_column(String(36))
    generation: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    token_sha256: Mapped[str] = mapped_column(String(64))
    claim_sha256: Mapped[str] = mapped_column(String(64))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(['job_id', 'case_id'], ['jobs.id', 'jobs.case_id'],
            name='fk_validation_job_case', ondelete='CASCADE'),
        ForeignKeyConstraint(['attempt_id', 'job_id'], ['job_attempts.id', 'job_attempts.job_id'],
            name='fk_validation_attempt_job', ondelete='CASCADE'),
        CheckConstraint('generation > 0', name='ck_validation_generation'),
        CheckConstraint("status IN ('held','released','revoked')", name='ck_validation_status'),
        CheckConstraint('length(token_sha256) = 64 AND length(claim_sha256) = 64', name='ck_validation_digests'),
        CheckConstraint('heartbeat_at >= acquired_at AND lease_expires_at > heartbeat_at', name='ck_validation_times'),
        CheckConstraint("(status = 'held' AND ended_at IS NULL) OR (status <> 'held' AND ended_at IS NOT NULL)",
            name='ck_validation_ended'),
        Index('ix_validation_status_lease', 'status', 'lease_expires_at'),
    )


class JobExecutionHead(Base):
    """Root Job's attempt family; each retry owns a new immutable legacy Job row."""
    __tablename__ = "job_execution_heads"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    next_number: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    active_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("job_attempts.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    __table_args__ = (CheckConstraint("next_number > 0 AND version >= 0", name="ck_job_execution_head_versions"),)


class JobSubmissionRequest(Timestamps, Base):
    __tablename__ = "job_submission_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, unique=True)
    execution_spec_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("case_id", "created_by", "idempotency_key", name="uq_job_submission_idempotency"),)


class NativeRequest(Timestamps, Base):
    """User-owned native requests, optionally bound to an existing case."""
    __tablename__ = 'native_requests'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[int | None] = mapped_column(ForeignKey('cases.id'), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_identity_json: Mapped[str] = mapped_column(Text, nullable=False)
    launch_nonce: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    profile_json: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey('conversations.id'),nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(ForeignKey('tool_calls.id'),nullable=True,unique=True)
    phase: Mapped[str] = mapped_column(String(20), default='active', server_default='active')
    version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    children_json: Mapped[str] = mapped_column(Text, default='{}', server_default='{}')
    result_json: Mapped[str] = mapped_column(Text, default='{}', server_default='{}')
    retirement_event_id: Mapped[str | None] = mapped_column(ForeignKey('durable_events.id', name='fk_native_requests_retirement_event'), nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("kind IN ('report.prepare','latex.prepare','synctex.query','runtime.wsl_probe','runtime.python_probe','agent.shell','terminal.export','terminal.recover','terminal.cleanup','job.finalize')", name='ck_native_request_kind'),
        CheckConstraint("(kind='agent.shell' AND conversation_id IS NOT NULL AND tool_call_id IS NOT NULL) OR (kind!='agent.shell' AND conversation_id IS NULL AND tool_call_id IS NULL)",name='ck_native_request_tool_scope'),
        CheckConstraint("(kind IN ('runtime.wsl_probe','runtime.python_probe') AND case_id IS NULL) OR "
                        "(kind NOT IN ('runtime.wsl_probe','runtime.python_probe') AND case_id IS NOT NULL)",
                        name='ck_native_request_scope'),
        CheckConstraint("phase IN ('active','succeeded','failed','cancelled')", name='ck_native_request_phase'),
        CheckConstraint('version >= 0', name='ck_native_request_version'),
        CheckConstraint("retirement_event_id IS NULL OR (kind='terminal.export' AND phase IN ('failed','cancelled'))", name='ck_native_request_retirement'),
        CheckConstraint("(phase='active' AND ended_at IS NULL) OR (phase!='active' AND ended_at IS NOT NULL)",
                        name='ck_native_request_ended'),
        Index('ix_native_requests_case_created', 'case_id', 'created_at'),
        Index('ix_native_requests_owner_lease', 'owner_id', 'lease_expires_at'),
        Index('ix_native_requests_user_created', 'created_by', 'kind', 'created_at'),
    )


class NativeSession(Timestamps, Base):
    """Interactive session ownership; never represented as a solver Job."""
    __tablename__='native_sessions'
    id: Mapped[str] = mapped_column(String(36),primary_key=True,default=new_id)
    case_id: Mapped[int | None] = mapped_column(ForeignKey('cases.id'),nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'),nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128),nullable=False)
    owner_token_sha256: Mapped[str] = mapped_column(String(64),nullable=False)
    owner_identity_json: Mapped[str] = mapped_column(Text,nullable=False)
    launch_nonce: Mapped[str] = mapped_column(String(64),nullable=False,unique=True)
    shell: Mapped[str] = mapped_column(String(20),nullable=False)
    profile_json: Mapped[str] = mapped_column(Text,nullable=False)
    phase: Mapped[str] = mapped_column(String(20),default='starting',server_default='starting')
    version: Mapped[int] = mapped_column(Integer,default=0,server_default='0')
    process_json: Mapped[str] = mapped_column(Text,default='{}',server_default='{}')
    stop_json: Mapped[str] = mapped_column(Text,default='{}',server_default='{}')
    retirement_event_id: Mapped[str | None] = mapped_column(ForeignKey('durable_events.id', name='fk_native_sessions_retirement_event'), nullable=True)
    tail_text: Mapped[str] = mapped_column(Text,default='',server_default='')
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer,nullable=True)
    error: Mapped[str | None] = mapped_column(String(80),nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),nullable=True)
    __table_args__=(
        CheckConstraint("shell IN ('cmd','wsl','wsl-sandbox')",name='ck_native_session_shell'),
        CheckConstraint("shell!='wsl-sandbox' OR case_id IS NOT NULL",name='ck_native_session_scope'),
        CheckConstraint("phase IN ('starting','active','closed','failed')",name='ck_native_session_phase'),
        CheckConstraint('version >= 0',name='ck_native_session_version'),
        CheckConstraint("retirement_event_id IS NULL OR (shell='wsl-sandbox' AND phase='failed')", name='ck_native_session_retirement'),
        CheckConstraint("(phase IN ('starting','active') AND ended_at IS NULL) OR (phase IN ('closed','failed') AND ended_at IS NOT NULL)",name='ck_native_session_ended'),
        Index('ix_native_sessions_user_created','created_by','created_at'),
        Index('ix_native_sessions_case_created','case_id','created_at'),
        Index('ix_native_sessions_owner_lease','owner_id','lease_expires_at'),
    )


class NativeVizSession(Timestamps, Base):
    """Owned rendering process, independent of engineering Jobs and PTYs."""
    __tablename__='native_viz_sessions'
    id: Mapped[int] = mapped_column(Integer,primary_key=True,autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey('cases.id'),nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'),nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128),nullable=False)
    owner_token_sha256: Mapped[str] = mapped_column(String(64),nullable=False)
    owner_identity_json: Mapped[str] = mapped_column(Text,nullable=False)
    launch_nonce: Mapped[str] = mapped_column(String(64),nullable=False,unique=True)
    profile_json: Mapped[str] = mapped_column(Text,nullable=False)
    phase: Mapped[str] = mapped_column(String(20),default='starting',server_default='starting')
    version: Mapped[int] = mapped_column(Integer,default=0,server_default='0')
    process_json: Mapped[str] = mapped_column(Text,default='{}',server_default='{}')
    ready_json: Mapped[str] = mapped_column(Text,default='{}',server_default='{}')
    stop_json: Mapped[str] = mapped_column(Text,default='{}',server_default='{}')
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer,nullable=True)
    error: Mapped[str | None] = mapped_column(String(80),nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),nullable=True)
    __table_args__=(
        CheckConstraint("phase IN ('starting','active','closed','failed')",name='ck_native_viz_phase'),
        CheckConstraint('version >= 0',name='ck_native_viz_version'),
        CheckConstraint("(phase IN ('starting','active') AND ended_at IS NULL) OR (phase IN ('closed','failed') AND ended_at IS NOT NULL)",name='ck_native_viz_ended'),
        Index('ix_native_viz_user_created','created_by','created_at'),
        Index('ix_native_viz_case_created','case_id','created_at'),
        Index('ix_native_viz_owner_lease','owner_id','lease_expires_at'),
    )


class CliScopeAllocation(Timestamps, Base):
    """Pre-session scope intent; survives a crash before a CLI owner exists."""
    __tablename__ = 'cli_scope_allocations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False, index=True)
    binding_json: Mapped[str] = mapped_column(Text, nullable=False)
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    prepared_json: Mapped[str] = mapped_column(Text, default='{}', server_default='{}')
    allocated_json: Mapped[str] = mapped_column(Text, default='{}', server_default='{}')
    retirement_json: Mapped[str] = mapped_column(Text, default='{}', server_default='{}')
    __table_args__ = (CheckConstraint('version >= 0', name='ck_cli_scope_allocation_version'),)


class NativeCliSession(Timestamps, Base):
    """CLI lifetime and private account binding, independent of Jobs and PTYs."""
    __tablename__ = 'native_cli_sessions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[int | None] = mapped_column(ForeignKey('cases.id'), nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey('conversations.id'), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_identity_json: Mapped[str] = mapped_column(Text, nullable=False)
    launch_nonce: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    runtime: Mapped[str] = mapped_column(String(20), nullable=False)
    profile_json: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(String(20), default='starting', server_default='starting')
    version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    process_json: Mapped[str] = mapped_column(Text, default='{}', server_default='{}')
    initialize_json: Mapped[str] = mapped_column(Text, default='{}', server_default='{}')
    stop_json: Mapped[str] = mapped_column(Text, default='{}', server_default='{}')
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    thread_state: Mapped[str] = mapped_column(String(20), default='none', server_default='none')
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("runtime IN ('windows','wsl','docker')", name='ck_native_cli_runtime'),
        CheckConstraint('conversation_id IS NULL OR case_id IS NOT NULL', name='ck_native_cli_scope'),
        CheckConstraint("phase IN ('starting','active','closed','failed')", name='ck_native_cli_phase'),
        CheckConstraint('version >= 0', name='ck_native_cli_version'),
        CheckConstraint("(phase IN ('starting','active') AND ended_at IS NULL) OR (phase IN ('closed','failed') AND ended_at IS NOT NULL)", name='ck_native_cli_ended'),
        CheckConstraint("(thread_state IN ('none','starting') AND thread_id IS NULL) OR (thread_state='ready' AND thread_id IS NOT NULL AND length(thread_id)>0)", name='ck_native_cli_thread'),
        Index('ix_native_cli_user_created', 'created_by', 'created_at'),
        Index('ix_native_cli_case_created', 'case_id', 'created_at'),
        Index('ix_native_cli_conversation', 'conversation_id'),
        Index('ix_native_cli_owner_lease', 'owner_id', 'lease_expires_at'),
    )


class NativeCliSessionRequest(Timestamps, Base):
    """Durable, idempotent admission before a conversation CLI session exists."""
    __tablename__ = 'native_cli_session_requests'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[int] = mapped_column(ForeignKey('cases.id'), nullable=False)
    conversation_id: Mapped[int] = mapped_column(ForeignKey('conversations.id'), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    selection_json: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str | None] = mapped_column(ForeignKey('native_cli_sessions.id'), nullable=True, unique=True)
    phase: Mapped[str] = mapped_column(String(24), default='admitted', server_default='admitted')
    version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    not_started: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint('conversation_id','created_by','client_request_id',name='uq_cli_session_request'),
        CheckConstraint("phase IN ('admitted','starting','completed','failed','cancelled','recovery_required')",name='ck_cli_session_request_phase'),
        CheckConstraint('version >= 0 AND length(client_request_id)>0 AND length(request_sha256)=64',name='ck_cli_session_request_values'),
        CheckConstraint("phase != 'completed' OR session_id IS NOT NULL",name='ck_cli_session_request_completed'),
        CheckConstraint('session_id IS NULL OR session_id=id',name='ck_cli_session_request_binding'),
        CheckConstraint("NOT not_started OR (session_id IS NULL AND phase IN ('failed','cancelled'))",name='ck_cli_session_request_not_started'),
        CheckConstraint("(phase IN ('completed','failed','cancelled','recovery_required') AND ended_at IS NOT NULL) OR (phase IN ('admitted','starting') AND ended_at IS NULL)",name='ck_cli_session_request_ended'),
        Index('ix_cli_session_request_conversation','conversation_id','created_at'),
    )


class ConversationExecution(Timestamps, Base):
    """One durable execution owner across API, Goal, approval and native CLI."""
    __tablename__ = 'conversation_executions'
    conversation_id: Mapped[int] = mapped_column(ForeignKey('conversations.id'), primary_key=True)
    phase: Mapped[str] = mapped_column(String(20), default='idle', server_default='idle')
    version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    backend: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    owner_token_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_identity_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("phase IN ('idle','active','recovery_required')", name='ck_conversation_execution_phase'),
        CheckConstraint('version >= 0', name='ck_conversation_execution_version'),
        CheckConstraint("backend IS NULL OR backend IN ('api','goal','approval','codex')", name='ck_conversation_execution_backend'),
        CheckConstraint("(phase='idle' AND backend IS NULL AND resource_id IS NULL AND owner_id IS NULL AND owner_token_sha256 IS NULL AND owner_identity_json IS NULL AND lease_expires_at IS NULL) OR (phase!='idle' AND backend IS NOT NULL AND resource_id IS NOT NULL AND owner_id IS NOT NULL AND owner_token_sha256 IS NOT NULL AND owner_identity_json IS NOT NULL AND lease_expires_at IS NOT NULL)", name='ck_conversation_execution_owner'),
    )


class NativeCliTurn(Timestamps, Base):
    __tablename__ = 'native_cli_turns'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey('native_cli_sessions.id'), nullable=False)
    case_id: Mapped[int] = mapped_column(ForeignKey('cases.id'), nullable=False)
    conversation_id: Mapped[int] = mapped_column(ForeignKey('conversations.id'), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    client_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    phase: Mapped[str] = mapped_column(String(24), default='admitted', server_default='admitted')
    version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    next_seq: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    event_bytes: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    native_turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    native_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    attempts_json: Mapped[str] = mapped_column(Text, default='[]', server_default='[]')
    error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint('conversation_id', 'created_by', 'client_message_id', name='uq_cli_turn_client_message'),
        CheckConstraint("mode IN ('message','goal')", name='ck_cli_turn_mode'),
        CheckConstraint("phase IN ('admitted','starting','running','awaiting_input','finalizing','completed','failed','cancelled','interrupted')", name='ck_cli_turn_phase'),
        CheckConstraint('version >= 0 AND next_seq > 0 AND event_bytes >= 0', name='ck_cli_turn_counters'),
        CheckConstraint("native_status IS NULL OR native_status IN ('completed','failed','interrupted')", name='ck_cli_turn_native_status'),
        CheckConstraint("native_turn_id IS NULL OR thread_id IS NOT NULL", name='ck_cli_turn_thread'),
        CheckConstraint("(phase IN ('completed','failed','cancelled','interrupted') AND ended_at IS NOT NULL) OR (phase NOT IN ('completed','failed','cancelled','interrupted') AND ended_at IS NULL)", name='ck_cli_turn_ended'),
        Index('ix_cli_turn_conversation_created', 'conversation_id', 'created_at'),
        Index('ix_cli_turn_session', 'session_id'),
    )


class NativeCliFork(Timestamps, Base):
    """One admitted native fork; an uncertain RPC is never automatically replayed."""
    __tablename__ = 'native_cli_forks'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_conversation_id: Mapped[int] = mapped_column(ForeignKey('conversations.id'), nullable=False)
    conversation_id: Mapped[int] = mapped_column(ForeignKey('conversations.id'), nullable=False, unique=True)
    source_turn_id: Mapped[str] = mapped_column(ForeignKey('native_cli_turns.id'), nullable=False)
    boundary_event_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, 'sqlite'), ForeignKey('agent_events.id'), nullable=False)
    case_id: Mapped[int] = mapped_column(ForeignKey('cases.id'), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(ForeignKey('native_cli_sessions.id'), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phase: Mapped[str] = mapped_column(String(24), default='admitted', server_default='admitted')
    version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint('source_conversation_id', 'created_by', 'client_request_id', name='uq_cli_fork_request'),
        CheckConstraint('source_conversation_id != conversation_id', name='ck_cli_fork_distinct'),
        CheckConstraint("phase IN ('admitted','forking','registered','completed','failed','recovery_required')", name='ck_cli_fork_phase'),
        CheckConstraint('version >= 0 AND length(client_request_id)>0 AND length(request_sha256)=64', name='ck_cli_fork_values'),
        CheckConstraint("thread_id IS NULL OR (session_id IS NOT NULL AND length(thread_id)>0)", name='ck_cli_fork_thread'),
        CheckConstraint("phase NOT IN ('forking','registered','completed') OR session_id IS NOT NULL", name='ck_cli_fork_session'),
        CheckConstraint("phase NOT IN ('registered','completed') OR thread_id IS NOT NULL", name='ck_cli_fork_registered'),
        CheckConstraint("(phase IN ('completed','failed','recovery_required') AND ended_at IS NOT NULL) OR (phase IN ('admitted','forking','registered') AND ended_at IS NULL)", name='ck_cli_fork_ended'),
        Index('ix_cli_fork_source_created', 'source_conversation_id', 'created_at'),
    )


class NativeCliEvent(Base):
    __tablename__ = 'native_cli_events'
    turn_id: Mapped[str] = mapped_column(ForeignKey('native_cli_turns.id'), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    __table_args__ = (CheckConstraint('seq > 0', name='ck_cli_event_sequence'),)


class NativeCliApproval(Timestamps, Base):
    __tablename__ = 'native_cli_approvals'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    turn_id: Mapped[str] = mapped_column(ForeignKey('native_cli_turns.id'), nullable=False)
    request_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_id_json: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(String(80), nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(20), default='pending', server_default='pending')
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint('turn_id', 'request_key', name='uq_cli_approval_request'),
        CheckConstraint("phase IN ('pending','claimed','sent','expired')", name='ck_cli_approval_phase'),
        CheckConstraint("method IN ('item/commandExecution/requestApproval','item/fileChange/requestApproval','item/permissions/requestApproval','item/tool/requestUserInput')", name='ck_cli_approval_method'),
    )


class ArtifactPublication(Timestamps, Base):
    __tablename__ = "artifact_publications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(80))
    generation: Mapped[int] = mapped_column(Integer)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("job_attempts.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="prepared", server_default="prepared")
    immutable_path: Mapped[str] = mapped_column(String(800))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("case_id", "kind", "generation", name="uq_artifact_publication_generation"),
        CheckConstraint("generation > 0", name="ck_publication_generation"),
        CheckConstraint("status IN ('prepared','committed','superseded','failed')", name="ck_publication_status"),
    )


class ArtifactPublicationHead(Base):
    __tablename__ = "artifact_publication_heads"
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), primary_key=True)
    next_generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    active_publication_id: Mapped[str | None] = mapped_column(ForeignKey("artifact_publications.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    __table_args__ = (CheckConstraint("next_generation > 0 AND version >= 0", name="ck_publication_head_versions"),)


class CaseEventCounter(Base):
    __tablename__ = "case_event_counters"
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), primary_key=True)
    next_seq: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    __table_args__ = (CheckConstraint("next_seq > 0", name="ck_case_event_counter_positive"),)


class CaseFileObservation(Base):
    __tablename__ = 'case_file_observations'
    case_id: Mapped[int] = mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    entries_json: Mapped[str | None] = mapped_column(Text)


class DurableEvent(Base):
    __tablename__ = "durable_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_runs.id"), nullable=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_steps.id"), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("job_attempts.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("case_id", "seq", name="uq_durable_event_case_seq"),
        CheckConstraint("seq > 0", name="ck_durable_event_seq"),
        Index("ix_durable_events_run", "run_id"),
    )


class EventOutbox(Base):
    __tablename__ = "event_outbox"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("durable_events.id"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),
        CheckConstraint("status IN ('pending','delivering','delivered')", name="ck_outbox_status"),
        Index("ix_event_outbox_delivery", "status", "next_attempt_at"),
    )
