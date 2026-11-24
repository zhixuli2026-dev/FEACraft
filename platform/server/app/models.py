"""数据模型 — 对应方案 §数据模型 的核心表（切片版先上核心）"""
from datetime import date, datetime, timezone

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    workspaces: Mapped[list["Workspace"]] = relationship(back_populates="owner")


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    owner: Mapped[User] = relationship(back_populates="workspaces")
    cases: Mapped[list["Case"]] = relationship(back_populates="workspace")


class MaterialLibraryRevision(Base):
    __tablename__ = "material_library_revisions"
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# caseDict 状态机字母（与原版契约一致）
WORKFLOW_STATES = ("A", "B", "C", "D", "E", "V")


class Case(Base):
    """一个仿真算例。文件以 S3 为唯一事实源，本表只存元数据与 caseDict 镜像。"""
    __tablename__ = "cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    s3_prefix: Mapped[str] = mapped_column(String(500))          # workspaces/{uid}/cases/{id}/
    workflow_state: Mapped[str] = mapped_column(String(1), default="A")  # A/B/C/D/E/V
    dict_revision: Mapped[int] = mapped_column(Integer, default=0)       # caseDict 镜像版本
    case_meta: Mapped[str] = mapped_column(Text, default="{}")           # JSON 镜像（标题/学科/求解器…）
    parse_status: Mapped[str] = mapped_column(String(20), default="ok")  # ok / invalid
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)

    workspace: Mapped[Workspace] = relationship(back_populates="cases")
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_case_name"),)


class ProviderProfile(Base):
    """个人或组织持有的模型配置。凭据始终加密存储。"""
    __tablename__ = "provider_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey(
        "organizations.id", name="fk_provider_profiles_org_id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40))          # openai/deepseek/kimi-coding/custom...
    name: Mapped[str] = mapped_column(String(120))             # 显示名
    base_url: Mapped[str] = mapped_column(String(300), default="")  # 空=用注册表默认
    default_model: Mapped[str] = mapped_column(String(120), default="")
    key_ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    """Agent 会话（fork 支持通过 forked_from_id）"""
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    forked_from_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    branch_head_event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    provider_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str] = mapped_column(String(120), default="")
    permission_mode: Mapped[str] = mapped_column(String(30), default="approve_writes")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConversationModelPreference(Base):
    """Per-user choices for each conversation backend."""
    __tablename__ = "conversation_model_preferences"
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    runtime: Mapped[str] = mapped_column(String(12), primary_key=True)
    provider_profile_id: Mapped[int] = mapped_column(ForeignKey("provider_profiles.id", ondelete="CASCADE"), index=True)
    profile_fingerprint: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(120))
    options_json: Mapped[str] = mapped_column(Text)


class AgentEvent(Base):
    """append-only 事件流（JSONL schema 入 payload），fork 语义依赖此表"""
    __tablename__ = "agent_events"
    __table_args__ = (Index("ix_agent_events_history", "conversation_id", "seq", "id"),)
    # SQLite 仅 INTEGER PRIMARY KEY 自增；BigInteger 在 Postgres 生效
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(30))     # message|run|system|tool...
    payload: Mapped[str] = mapped_column(Text)        # JSON
    cwd: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolCall(Base):
    """工具调用记录（危险工具挂起待审批；approve/reject 端点驱动）"""
    __tablename__ = "tool_calls"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # 模型给的 call id
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    tool: Mapped[str] = mapped_column(String(60))
    args: Mapped[str] = mapped_column(Text, default="{}")          # JSON
    status: Mapped[str] = mapped_column(String(20), default="pending")  # queued|running|pending|executed|rejected|cancelled|interrupted
    result: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    type: Mapped[str] = mapped_column(String(40))     # solve|mesh|pvpython|geometry|convert...
    command: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|succeeded|failed|cancelled
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_s3_key: Mapped[str] = mapped_column(String(600), default="")
    parsed_summary: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index('uq_job_case_identity', 'id', 'case_id', unique=True),)


# ---------------- 订阅体系（org/seat/plan；方案 §数据模型骨架版） ----------------
class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="member")  # admin|member|viewer
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_membership"),)


class Plan(Base):
    """套餐：配额 JSON（viz_concurrent/job_concurrent/llm_msgs_per_day/storage_mb）"""
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)   # free|pro|team…
    name: Mapped[str] = mapped_column(String(120))
    quotas: Mapped[str] = mapped_column(Text, default="{}")      # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|expired
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelQuotaDay(Base):
    __tablename__ = 'model_quota_days'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    used: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint('used > 0', name='ck_model_quota_days_used'),)


# Register additive workflow tables for init_db/Alembic and retain one import surface.
from .access_grant_models import OrganizationResourceGrant  # noqa: E402,F401
from .acp_models import AcpThread, AcpTurn  # noqa: E402,F401
from .audit_models import OrganizationAuditHead, OrganizationAuditRecord  # noqa: E402,F401
from .workflow_models import (ArtifactPublication, ArtifactPublicationHead, CaseEventCounter, CaseFileObservation, CaseValidationFence,
    DurableEvent, EventOutbox, JobAttempt, JobExecutionHead, JobSubmissionRequest,
    WorkflowRun, WorkflowStep, NativeRequest, NativeSession, NativeVizSession, NativeCliSession, CliScopeAllocation,
    ConversationExecution, NativeCliTurn, NativeCliEvent, NativeCliApproval, NativeCliFork, NativeCliSessionRequest)  # noqa: E402,F401
from .auth_models import WebsiteOAuthTransaction, ExternalIdentity, WebsiteAuthSession  # noqa: E402,F401
from .viz_collaboration_models import VizPresentation, VizViewer  # noqa: E402,F401
from .workflow_batch_models import WorkflowBatch, WorkflowBatchMember  # noqa: E402,F401
from .storage_models import CaseStorageOperation, UploadStorageReservation, PublicationStorageReservation, OutputArchiveReservation, JobLogReservation, StorageWriteReservation, OwnerStorageReservation, UploadChunkCredit  # noqa: E402,F401
from .storage_models import CliInitializationReservation  # noqa: E402,F401
from .session_review_models import NativeSessionReview, NativeSessionLink  # noqa: E402,F401
from .input_recovery_models import JobInputRecovery  # noqa: E402,F401
from .remote_hpc_models import RemoteHpcRun  # noqa: E402,F401
from .scnet_models import ScnetAccountSession  # noqa: E402,F401
