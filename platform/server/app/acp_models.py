"""Private native thread identities and durable ACP turn admissions."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class AcpThread(Base):
    __tablename__ = 'acp_threads'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey('conversations.id', ondelete='CASCADE'))
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    harness: Mapped[str] = mapped_column(String(16))
    runtime: Mapped[str] = mapped_column(String(16))
    profile_id: Mapped[int]
    profile_sha256: Mapped[str] = mapped_column(String(64))
    host_id: Mapped[str] = mapped_column(String(128))
    contract_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    allow_drafts: Mapped[bool] = mapped_column(default=False, nullable=False)
    native_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("harness IN ('opencode','kimi')", name='ck_acp_thread_harness'),
        CheckConstraint("runtime IN ('windows','linux','wsl')", name='ck_acp_thread_runtime'),
        CheckConstraint('length(profile_sha256) = 64', name='ck_acp_thread_profile'),
        Index('ix_acp_thread_conversation', 'conversation_id', 'created_by', 'created_at'),
    )


class AcpTurn(Base):
    __tablename__ = 'acp_turns'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey('acp_threads.id', ondelete='CASCADE'))
    conversation_id: Mapped[int] = mapped_column(ForeignKey('conversations.id', ondelete='CASCADE'))
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    client_message_id: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    phase: Mapped[str] = mapped_column(String(20))
    process_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stop_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint('conversation_id', 'created_by', 'client_message_id', name='uq_acp_turn_message'),
        CheckConstraint("phase IN ('admitted','running','completed','failed','cancelled','recovery_required')",
                        name='ck_acp_turn_phase'),
        CheckConstraint('length(request_sha256) = 64', name='ck_acp_turn_request'),
        Index('ix_acp_turn_thread', 'thread_id', 'created_at'),
    )
