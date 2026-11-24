"""Immutable native-session annotations; independent of possibly damaged execution journals."""
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class NativeSessionReview(Base):
    __tablename__ = 'native_session_reviews'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    case_id: Mapped[int | None] = mapped_column(ForeignKey('cases.id'), nullable=True)
    actor_user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                              default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        CheckConstraint("target_kind IN ('terminal','cli','viz')", name='ck_native_review_kind'),
        CheckConstraint('length(target_id) > 0', name='ck_native_review_target'),
        CheckConstraint('length(snapshot_sha256) = 64', name='ck_native_review_snapshot'),
        Index('ix_native_review_target', 'actor_user_id', 'target_kind', 'target_id', 'created_at'),
        Index('ix_native_review_case', 'case_id', 'created_at'),
    )


class NativeSessionLink(Base):
    """Explicit operator lineage between existing sessions, without execution inheritance."""
    __tablename__ = 'native_session_links'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    successor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    case_id: Mapped[int | None] = mapped_column(ForeignKey('cases.id'), nullable=True)
    actor_user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                              default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        CheckConstraint("kind IN ('terminal','cli','viz')", name='ck_native_link_kind'),
        CheckConstraint('length(source_id)>0 AND length(successor_id)>0 AND source_id!=successor_id', name='ck_native_link_targets'),
        UniqueConstraint('kind', 'successor_id', name='uq_native_link_successor'),
        Index('ix_native_link_source', 'actor_user_id', 'kind', 'source_id', 'created_at'),
        Index('ix_native_link_case', 'case_id', 'created_at'),
    )
