"""Durable identities for jobs accepted by an external scheduler."""

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .workflow_models import Timestamps


class RemoteHpcRun(Timestamps, Base):
    __tablename__ = 'remote_hpc_runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey('cases.id'), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    profile: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    input_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey('remote_hpc_runs.id'))
    scheduler_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(24), nullable=False, default='staging')
    observation_json: Mapped[str] = mapped_column(Text, nullable=False, default='{}')
    __table_args__ = (
        UniqueConstraint('case_id', 'user_id', 'request_key', name='uq_remote_hpc_request'),
        Index('ix_remote_hpc_case_owner', 'case_id', 'user_id', 'created_at'),
    )
