"""Reserve immutable remote Job logs before remote writes.

Revision ID: bf94b60735dc
Revises: ae83a5f924cb
"""
from alembic import op
import sqlalchemy as sa

revision='bf94b60735dc'
down_revision='ae83a5f924cb'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('job_log_reservations',
        sa.Column('job_id',sa.Integer(),sa.ForeignKey('jobs.id'),primary_key=True),
        sa.Column('attempt_id',sa.String(36),sa.ForeignKey('job_attempts.id'),nullable=False),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('case_id',sa.Integer(),sa.ForeignKey('cases.id'),nullable=False),
        sa.Column('binding_json',sa.Text(),nullable=False),
        sa.Column('sha256',sa.String(64),nullable=False),
        sa.Column('size',sa.BigInteger(),nullable=False),
        sa.Column('remaining_bytes',sa.BigInteger(),nullable=False),
        sa.Column('state',sa.String(16),nullable=False),
        sa.Column('writer_json',sa.Text(),nullable=False),
        sa.Column('token',sa.String(36),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('size >= 0 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_job_log_bytes'),
        sa.CheckConstraint("state IN ('active','materialized','released')",name='ck_job_log_state'),
        sa.CheckConstraint("(state = 'active' AND remaining_bytes = size) OR (state <> 'active' AND remaining_bytes = 0)",name='ck_job_log_release'))
    op.create_index('ix_job_log_reservations_user_id','job_log_reservations',['user_id'])
    op.create_index('ix_job_log_reservations_case_id','job_log_reservations',['case_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM job_log_reservations')).scalar_one():
        raise RuntimeError('Downgrade would discard remote log quota ownership and settlement history')
    op.drop_table('job_log_reservations')
