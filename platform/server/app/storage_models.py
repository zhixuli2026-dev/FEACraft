"""Durable case storage reservations, independent of long network requests."""
from datetime import datetime,timezone
from sqlalchemy import BigInteger,CheckConstraint,DateTime,ForeignKey,Index,String,Text,text
from sqlalchemy.orm import Mapped,mapped_column
from .db import Base
from .reservation_receipts import ReservationReceipt


class StorageQuotaEpoch(Base):
    """Version of the owner's cooperating storage admission transactions."""
    __tablename__='storage_quota_epochs'
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id',ondelete='CASCADE'),primary_key=True,autoincrement=False)
    revision: Mapped[int]=mapped_column(BigInteger,nullable=False)


class CaseStorageOperation(Base):
    __tablename__='case_storage_operations'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    case_id: Mapped[int]=mapped_column(ForeignKey('cases.id',ondelete='CASCADE'),nullable=False,index=True)
    source_prefix: Mapped[str]=mapped_column(Text,nullable=False)
    purpose: Mapped[str]=mapped_column(String(20),nullable=False)
    state: Mapped[str]=mapped_column(String(16),nullable=False)
    owner_json: Mapped[str]=mapped_column(Text,nullable=False)
    journal_id: Mapped[str|None]=mapped_column(String(36),nullable=True,index=True)
    plan_sha256: Mapped[str|None]=mapped_column(String(64),nullable=True)
    event_id: Mapped[str|None]=mapped_column(String(36),nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    ended_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    __table_args__=(
        CheckConstraint("purpose IN ('staging','recovery')",name='ck_storage_operation_purpose'),
        CheckConstraint("state IN ('active','drained','exited','settled')",name='ck_storage_operation_state'),
        CheckConstraint("(state = 'active' AND ended_at IS NULL) OR (state <> 'active' AND ended_at IS NOT NULL)",name='ck_storage_operation_end'),
        Index('uq_storage_operation_active_case','case_id',unique=True,
              sqlite_where=text("state = 'active'"),postgresql_where=text("state = 'active'")),
    )


class UploadStorageReservation(Base):
    """Unwritten upload payload, persisted independently of request lifetimes."""
    __tablename__='upload_storage_reservations'
    id: Mapped[str]=mapped_column(String(32),primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),nullable=False,index=True)
    case_id: Mapped[int]=mapped_column(ForeignKey('cases.id'),nullable=False,index=True)
    binding_json: Mapped[str]=mapped_column(Text,nullable=False)
    path: Mapped[str]=mapped_column(Text,nullable=False)
    size: Mapped[int]=mapped_column(BigInteger,nullable=False)
    remaining_bytes: Mapped[int]=mapped_column(BigInteger,nullable=False)
    state: Mapped[str]=mapped_column(String(16),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    __table_args__=(
        CheckConstraint('size > 0 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_upload_storage_bytes'),
        CheckConstraint("state IN ('active','completed','cancelled')",name='ck_upload_storage_state'),
        CheckConstraint("state = 'active' OR remaining_bytes = 0",name='ck_upload_storage_release'),
    )


class PublicationStorageReservation(Base):
    """Full copy allowance and process identity survive publisher termination."""
    __tablename__='publication_storage_reservations'
    publication_id: Mapped[str]=mapped_column(ForeignKey('artifact_publications.id'),primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),nullable=False,index=True)
    case_id: Mapped[int]=mapped_column(ForeignKey('cases.id'),nullable=False,index=True)
    binding_json: Mapped[str]=mapped_column(Text,nullable=False)
    manifest_sha256: Mapped[str]=mapped_column(String(64),nullable=False)
    size: Mapped[int]=mapped_column(BigInteger,nullable=False)
    remaining_bytes: Mapped[int]=mapped_column(BigInteger,nullable=False)
    state: Mapped[str]=mapped_column(String(16),nullable=False)
    writer_json: Mapped[str]=mapped_column(Text,nullable=False)
    token: Mapped[str]=mapped_column(String(36),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    __table_args__=(
        CheckConstraint('size > 0 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_publication_storage_bytes'),
        CheckConstraint("state IN ('active','materialized','released')",name='ck_publication_storage_state'),
        CheckConstraint("(state = 'active' AND remaining_bytes = size) OR (state <> 'active' AND remaining_bytes = 0)",name='ck_publication_storage_release'),
    )


class OutputArchiveReservation(Base):
    """Future S3 output bytes retained across API/worker failure."""
    __tablename__='output_archive_reservations'
    attempt_id: Mapped[str]=mapped_column(ForeignKey('job_attempts.id'),primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),nullable=False,index=True)
    case_id: Mapped[int]=mapped_column(ForeignKey('cases.id'),nullable=False,index=True)
    binding_json: Mapped[str]=mapped_column(Text,nullable=False)
    input_sha256: Mapped[str]=mapped_column(String(64),nullable=False)
    manifest_sha256: Mapped[str]=mapped_column(String(64),nullable=False)
    size: Mapped[int]=mapped_column(BigInteger,nullable=False)
    remaining_bytes: Mapped[int]=mapped_column(BigInteger,nullable=False)
    state: Mapped[str]=mapped_column(String(16),nullable=False)
    writer_json: Mapped[str]=mapped_column(Text,nullable=False)
    token: Mapped[str]=mapped_column(String(36),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    __table_args__=(
        CheckConstraint('size > 0 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_output_archive_bytes'),
        CheckConstraint("state IN ('active','materialized','released')",name='ck_output_archive_state'),
        CheckConstraint("(state = 'active' AND remaining_bytes = size) OR (state <> 'active' AND remaining_bytes = 0)",name='ck_output_archive_release'),
    )


class JobLogReservation(Base):
    """Immutable remote log allowance and content receipt."""
    __tablename__='job_log_reservations'
    job_id: Mapped[int]=mapped_column(ForeignKey('jobs.id'),primary_key=True)
    attempt_id: Mapped[str]=mapped_column(ForeignKey('job_attempts.id'),nullable=False)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),nullable=False,index=True)
    case_id: Mapped[int]=mapped_column(ForeignKey('cases.id'),nullable=False,index=True)
    binding_json: Mapped[str]=mapped_column(Text,nullable=False)
    sha256: Mapped[str]=mapped_column(String(64),nullable=False)
    size: Mapped[int]=mapped_column(BigInteger,nullable=False)
    remaining_bytes: Mapped[int]=mapped_column(BigInteger,nullable=False)
    state: Mapped[str]=mapped_column(String(16),nullable=False)
    writer_json: Mapped[str]=mapped_column(Text,nullable=False)
    token: Mapped[str]=mapped_column(String(36),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    __table_args__=(
        CheckConstraint('size >= 0 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_job_log_bytes'),
        CheckConstraint("state IN ('active','materialized','released')",name='ck_job_log_state'),
        CheckConstraint("(state = 'active' AND remaining_bytes = size) OR (state <> 'active' AND remaining_bytes = 0)",name='ck_job_log_release'),
    )


class CliInitializationReservation(Base):
    """One native initializer's peak, committed before its bridge is released."""
    __tablename__='cli_initialization_reservations'
    session_id: Mapped[str]=mapped_column(ForeignKey('native_cli_sessions.id'),primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),nullable=False,index=True)
    binding_json: Mapped[str]=mapped_column(Text,nullable=False)
    size: Mapped[int]=mapped_column(BigInteger,nullable=False)
    remaining_bytes: Mapped[int]=mapped_column(BigInteger,nullable=False)
    state: Mapped[str]=mapped_column(String(16),nullable=False)
    receipt_json: Mapped[str]=mapped_column(Text,nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    __table_args__=(
        CheckConstraint('size >= 0 AND size <= 32768 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_cli_init_bytes'),
        CheckConstraint("state IN ('active','settled')",name='ck_cli_init_state'),
        CheckConstraint("(state = 'active' AND remaining_bytes = size AND receipt_json = '{}') OR (state = 'settled' AND remaining_bytes = 0 AND receipt_json <> '{}')",name='ck_cli_init_receipt'),
    )


class StorageWriteReservation(Base):
    """One physical remote PUT, independent of the caller's business commit."""
    __tablename__='storage_write_reservations'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    operation_id: Mapped[str]=mapped_column(ForeignKey('case_storage_operations.id'),nullable=False,index=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),nullable=False,index=True)
    case_id: Mapped[int]=mapped_column(ForeignKey('cases.id'),nullable=False,index=True)
    binding_json: Mapped[str]=mapped_column(Text,nullable=False)
    request_json: Mapped[str]=mapped_column(Text,nullable=False)
    writer_json: Mapped[str]=mapped_column(Text,nullable=False)
    size: Mapped[int]=mapped_column(BigInteger,nullable=False)
    remaining_bytes: Mapped[int]=mapped_column(BigInteger,nullable=False)
    state: Mapped[str]=mapped_column(String(16),nullable=False)
    receipt_json: Mapped[str]=mapped_column(ReservationReceipt(),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    __table_args__=(
        CheckConstraint('size >= 0 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_storage_write_bytes'),
        CheckConstraint("state IN ('active','settled')",name='ck_storage_write_state'),
        CheckConstraint("(state = 'active' AND remaining_bytes = size AND receipt_json = '{}') OR (state = 'settled' AND remaining_bytes = 0 AND receipt_json <> '{}')",name='ck_storage_write_receipt'),
    )


class UploadChunkCredit(Base):
    """Unwritten payload atomically transferred into one owner write pool."""
    __tablename__='upload_chunk_credits'
    id: Mapped[str]=mapped_column(ForeignKey('owner_storage_reservations.id'),primary_key=True)
    upload_id: Mapped[str]=mapped_column(ForeignKey('upload_storage_reservations.id'),nullable=False,index=True)
    binding_json: Mapped[str]=mapped_column(Text,nullable=False)
    offset: Mapped[int]=mapped_column(BigInteger,nullable=False)
    size: Mapped[int]=mapped_column(BigInteger,nullable=False)
    amount: Mapped[int]=mapped_column(BigInteger,nullable=False)
    sha256: Mapped[str]=mapped_column(String(64),nullable=False)
    state: Mapped[str]=mapped_column(String(16),nullable=False)
    receipt_json: Mapped[str]=mapped_column(Text,nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    __table_args__=(
        CheckConstraint('"offset" >= 0 AND size > 0 AND amount >= 0 AND amount <= size',name='ck_upload_chunk_bytes'),
        CheckConstraint("state IN ('active','settled')",name='ck_upload_chunk_state'),
        CheckConstraint("state = 'active' OR receipt_json <> '{}'",name='ck_upload_chunk_receipt'),
        Index('uq_upload_chunk_active','upload_id',unique=True,
            sqlite_where=text("state = 'active'"),postgresql_where=text("state = 'active'")),
    )


class OwnerStorageReservation(Base):
    """Remote capacity established before an atomic business transaction starts."""
    __tablename__='owner_storage_reservations'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),nullable=False,index=True)
    binding_json: Mapped[str]=mapped_column(Text,nullable=False)
    writer_json: Mapped[str]=mapped_column(Text,nullable=False)
    size: Mapped[int]=mapped_column(BigInteger,nullable=False)
    remaining_bytes: Mapped[int]=mapped_column(BigInteger,nullable=False)
    state: Mapped[str]=mapped_column(String(16),nullable=False)
    receipt_json: Mapped[str]=mapped_column(ReservationReceipt(),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    __table_args__=(
        CheckConstraint('size >= 0 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_owner_storage_bytes'),
        CheckConstraint("state IN ('active','settled')",name='ck_owner_storage_state'),
        CheckConstraint("(state = 'active') OR (remaining_bytes = 0 AND receipt_json <> '{}')",name='ck_owner_storage_receipt'),
    )
