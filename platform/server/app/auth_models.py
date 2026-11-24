"""Durable website authorization transactions and first-party sessions."""
from datetime import datetime, timezone
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now():
    return datetime.now(timezone.utc)


class WebsiteOAuthTransaction(Base):
    __tablename__ = 'website_oauth_transactions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    state_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    browser_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verifier_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(20), default='pending', server_default='pending')
    intent: Mapped[str] = mapped_column(String(10), nullable=False)
    link_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index('ix_website_oauth_browser', 'browser_sha256'),
        CheckConstraint("phase IN ('pending','processing','completed','failed','cancelled','expired')", name='ck_website_oauth_phase'),
        CheckConstraint("(intent='login' AND link_user_id IS NULL) OR (intent='link' AND link_user_id IS NOT NULL)", name='ck_website_oauth_intent'),
        CheckConstraint("(phase IN ('pending','processing') AND verifier_ciphertext IS NOT NULL AND ended_at IS NULL) OR (phase NOT IN ('pending','processing') AND verifier_ciphertext IS NULL AND ended_at IS NOT NULL)", name='ck_website_oauth_terminal'),
        CheckConstraint('expires_at > created_at', name='ck_website_oauth_expiry'),
    )


class ExternalIdentity(Base):
    __tablename__ = 'external_identities'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    issuer: Mapped[str] = mapped_column(String(300), nullable=False)
    client_id: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    display_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_verified: Mapped[bool] = mapped_column(default=False, server_default='false')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint('issuer', 'client_id', 'subject', name='uq_external_identity_subject'),
        Index('ix_external_identity_user', 'user_id'),
    )


class WebsiteAuthSession(Base):
    __tablename__ = 'website_auth_sessions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    identity_id: Mapped[str] = mapped_column(ForeignKey('external_identities.id'), nullable=False)
    provider_sid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index('ix_website_auth_user', 'user_id'),
        Index('ix_website_auth_identity', 'identity_id'),
        CheckConstraint('expires_at > created_at', name='ck_website_auth_expiry'),
    )
