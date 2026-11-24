"""Explicit replacement requests; historical input evidence is never rewritten."""
from datetime import datetime, timezone
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class JobInputRecovery(Base):
    __tablename__='job_input_recoveries'
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid.uuid4()))
    case_id: Mapped[int]=mapped_column(Integer,nullable=False)
    source_job_id: Mapped[int]=mapped_column(Integer,nullable=False)
    source_attempt_id: Mapped[str]=mapped_column(String(36),nullable=False)
    target_job_id: Mapped[int]=mapped_column(Integer,nullable=False)
    actor_user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),nullable=False)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False,default=lambda:datetime.now(timezone.utc))
    __table_args__=(
        ForeignKeyConstraint(['source_job_id','case_id'],['jobs.id','jobs.case_id'],name='fk_input_recovery_source_case'),
        ForeignKeyConstraint(['target_job_id','case_id'],['jobs.id','jobs.case_id'],name='fk_input_recovery_target_case'),
        ForeignKeyConstraint(['source_attempt_id','source_job_id'],['job_attempts.id','job_attempts.job_id'],name='fk_input_recovery_attempt'),
        UniqueConstraint('source_job_id',name='uq_input_recovery_source'),
        UniqueConstraint('target_job_id',name='uq_input_recovery_target'),
        CheckConstraint('source_job_id != target_job_id',name='ck_input_recovery_distinct'),
        Index('ix_input_recovery_case','case_id','created_at'),
    )
