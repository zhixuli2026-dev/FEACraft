"""Database-backed organization audit chain."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class OrganizationAuditHead(Base):
    __tablename__ = 'organization_audit_heads'
    org_id: Mapped[int] = mapped_column(ForeignKey('organizations.id'), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    digest: Mapped[str] = mapped_column(String(64))
    legacy_valid: Mapped[bool] = mapped_column(Boolean)
    __table_args__ = (CheckConstraint('sequence >= 0', name='ck_org_audit_head_sequence'),)


class OrganizationAuditRecord(Base):
    __tablename__ = 'organization_audit_records'
    org_id: Mapped[int] = mapped_column(ForeignKey('organization_audit_heads.org_id'), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_user_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(81))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[str] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint('sequence > 0', name='ck_org_audit_record_sequence'),
        Index('ix_org_audit_record_action', 'org_id', 'action', 'sequence'),
        Index('ix_org_audit_record_actor', 'org_id', 'actor_user_id', 'sequence'),
    )
