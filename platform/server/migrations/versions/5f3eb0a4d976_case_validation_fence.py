"""Durable read-validation reservation; preserve case/attempt ownership in the DB."""
from alembic import op
import sqlalchemy as sa

revision = '5f3eb0a4d976'
down_revision = '4e2da9f3c865'
branch_labels = None
depends_on = None


def upgrade():
    # Unique indexes avoid rebuilding referenced SQLite parent tables.
    op.create_index('uq_job_case_identity', 'jobs', ['id', 'case_id'], unique=True)
    op.create_index('uq_attempt_job_identity', 'job_attempts', ['id', 'job_id'], unique=True)
    op.create_table('case_validation_fences',
        sa.Column('case_id', sa.Integer(), sa.ForeignKey('cases.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('attempt_id', sa.String(36), nullable=False),
        sa.Column('generation', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('token_sha256', sa.String(64), nullable=False),
        sa.Column('claim_sha256', sa.String(64), nullable=False),
        sa.Column('acquired_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['job_id', 'case_id'], ['jobs.id', 'jobs.case_id'],
            name='fk_validation_job_case', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['attempt_id', 'job_id'], ['job_attempts.id', 'job_attempts.job_id'],
            name='fk_validation_attempt_job', ondelete='CASCADE'),
        sa.CheckConstraint('generation > 0', name='ck_validation_generation'),
        sa.CheckConstraint("status IN ('held','released','revoked')", name='ck_validation_status'),
        sa.CheckConstraint('length(token_sha256) = 64 AND length(claim_sha256) = 64', name='ck_validation_digests'),
        sa.CheckConstraint('heartbeat_at >= acquired_at AND lease_expires_at > heartbeat_at', name='ck_validation_times'),
        sa.CheckConstraint("(status = 'held' AND ended_at IS NULL) OR (status <> 'held' AND ended_at IS NOT NULL)",
            name='ck_validation_ended'))
    op.create_index('ix_validation_status_lease', 'case_validation_fences', ['status', 'lease_expires_at'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM case_validation_fences LIMIT 1')).first():
        raise RuntimeError('Refusing to discard case validation ownership history')
    op.drop_table('case_validation_fences')
    op.drop_index('uq_attempt_job_identity', table_name='job_attempts')
    op.drop_index('uq_job_case_identity', table_name='jobs')
