"""SCNET account sessions shared by the application replicas."""

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class ScnetAccountSession(Base):
    __tablename__ = 'scnet_account_sessions'
    __table_args__ = (Index('ix_scnet_account_owner_expiry', 'user_id', 'expires_at'),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    state_cipher: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    renewal_until: Mapped[int] = mapped_column(Integer, nullable=False)
