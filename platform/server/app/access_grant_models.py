"""Database-authoritative organization grants for workspaces and individual cases."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class OrganizationResourceGrant(Base):
    __tablename__ = 'organization_resource_grants'
    scope: Mapped[str] = mapped_column(String(12),primary_key=True)
    resource_id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey('organizations.id',ondelete='CASCADE'),primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey('workspaces.id',ondelete='CASCADE'))
    case_id: Mapped[int | None] = mapped_column(ForeignKey('cases.id',ondelete='CASCADE'),nullable=True)
    role: Mapped[str] = mapped_column(String(12))
    role_ceiling: Mapped[str] = mapped_column(String(12))
    enabled: Mapped[bool]
    delegated: Mapped[bool]
    legacy_slot: Mapped[bool]
    version: Mapped[int]
    updated_by: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("(scope='workspace' AND case_id IS NULL AND resource_id=workspace_id) OR "
                        "(scope='case' AND case_id IS NOT NULL AND resource_id=case_id)", name='ck_org_grant_scope'),
        CheckConstraint("role IN ('viewer','member') AND role_ceiling IN ('viewer','member') "
                        "AND (role='viewer' OR role_ceiling='member')", name='ck_org_grant_role'),
        CheckConstraint('version > 0 AND updated_by > 0', name='ck_org_grant_revision'),
        Index('ix_org_grant_workspace', 'workspace_id','scope','enabled','org_id'),
        Index('ix_org_grant_org', 'org_id','scope','enabled'),
    )
