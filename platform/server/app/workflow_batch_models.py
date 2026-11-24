"""Durable typed workflow groups; member runs retain their registered plans."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import CheckConstraint,DateTime,ForeignKey,ForeignKeyConstraint,Index,Integer,String,Text,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from .db import Base
from .workflow_models import Timestamps


class WorkflowBatch(Timestamps,Base):
    __tablename__='workflow_batches'
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    created_by:Mapped[int]=mapped_column(ForeignKey('users.id'),nullable=False)
    owner_id:Mapped[int]=mapped_column(ForeignKey('users.id'),nullable=False)
    idempotency_key:Mapped[str]=mapped_column(String(128),nullable=False)
    request_hash:Mapped[str]=mapped_column(String(64),nullable=False)
    request_json:Mapped[str]=mapped_column(Text,nullable=False)
    membership_json:Mapped[str]=mapped_column(Text,nullable=False)
    membership_sha256:Mapped[str]=mapped_column(String(64),nullable=False)
    max_concurrent:Mapped[int]=mapped_column(Integer,nullable=False)
    delivery_mode:Mapped[str]=mapped_column(String(8),nullable=False)
    version:Mapped[int]=mapped_column(Integer,nullable=False,default=0,server_default='0')
    cancel_requested_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    __table_args__=(
        UniqueConstraint('created_by','idempotency_key',name='uq_workflow_batch_request'),
        CheckConstraint('max_concurrent BETWEEN 1 AND 4',name='ck_workflow_batch_capacity'),
        CheckConstraint("delivery_mode IN ('local','redis')",name='ck_workflow_batch_mode'),
        CheckConstraint('version >= 0',name='ck_workflow_batch_version'),
        Index('ix_workflow_batch_creator_created','created_by','created_at'),)


class WorkflowBatchMember(Base):
    __tablename__='workflow_batch_members'
    run_id:Mapped[str]=mapped_column(String(36),primary_key=True)
    batch_id:Mapped[str]=mapped_column(ForeignKey('workflow_batches.id'),nullable=False,index=True)
    ordinal:Mapped[int]=mapped_column(Integer,nullable=False)
    member_key:Mapped[str]=mapped_column(String(80),nullable=False)
    case_id:Mapped[int]=mapped_column(ForeignKey('cases.id'),nullable=False)
    request_hash:Mapped[str]=mapped_column(String(64),nullable=False)
    __table_args__=(
        ForeignKeyConstraint(['run_id','case_id'],['workflow_runs.id','workflow_runs.case_id'],name='fk_workflow_batch_run_case'),
        UniqueConstraint('batch_id','ordinal',name='uq_workflow_batch_ordinal'),
        UniqueConstraint('batch_id','member_key',name='uq_workflow_batch_member_key'),
        UniqueConstraint('batch_id','case_id',name='uq_workflow_batch_member_case'),
        CheckConstraint('ordinal BETWEEN 0 AND 49',name='ck_workflow_batch_ordinal'),)
