"""Durable case storage operations outside SQL network transactions.

Revision ID: 7b50d2c6f198
Revises: 6a4fc1b5e087
"""
from alembic import op
import sqlalchemy as sa

revision='7b50d2c6f198'
down_revision='6a4fc1b5e087'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('case_storage_operations',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('case_id',sa.Integer(),sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=False),
        sa.Column('source_prefix',sa.Text(),nullable=False),
        sa.Column('purpose',sa.String(20),nullable=False),
        sa.Column('state',sa.String(16),nullable=False),
        sa.Column('owner_json',sa.Text(),nullable=False),
        sa.Column('journal_id',sa.String(36),nullable=True),
        sa.Column('plan_sha256',sa.String(64),nullable=True),
        sa.Column('event_id',sa.String(36),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('ended_at',sa.DateTime(timezone=True),nullable=True),
        sa.CheckConstraint("purpose IN ('staging','recovery')",name='ck_storage_operation_purpose'),
        sa.CheckConstraint("state IN ('active','drained','exited','settled')",name='ck_storage_operation_state'),
        sa.CheckConstraint("(state = 'active' AND ended_at IS NULL) OR (state <> 'active' AND ended_at IS NOT NULL)",name='ck_storage_operation_end'))
    op.create_index('ix_case_storage_operations_case_id','case_storage_operations',['case_id'])
    op.create_index('ix_case_storage_operations_journal_id','case_storage_operations',['journal_id'])
    op.create_index('uq_storage_operation_active_case','case_storage_operations',['case_id'],unique=True,
                    sqlite_where=sa.text("state = 'active'"),postgresql_where=sa.text("state = 'active'"))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM case_storage_operations')).scalar_one():
        raise RuntimeError('Downgrade would discard durable storage operation history')
    op.drop_table('case_storage_operations')
