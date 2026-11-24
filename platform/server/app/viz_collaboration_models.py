"""Latest shared viewport and short-lived viewer presence."""

from sqlalchemy import BigInteger, Float, ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class VizPresentation(Base):
    __tablename__ = 'viz_presentations'

    case_id: Mapped[int] = mapped_column(ForeignKey('cases.id', ondelete='CASCADE'), primary_key=True)
    presenter_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    session_id: Mapped[int] = mapped_column(ForeignKey('native_viz_sessions.id', ondelete='CASCADE'), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default='0')
    frame: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)


class VizViewer(Base):
    __tablename__ = 'viz_viewers'

    case_id: Mapped[int] = mapped_column(ForeignKey('viz_presentations.case_id', ondelete='CASCADE'), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
