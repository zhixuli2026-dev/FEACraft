"""Reserve private S3 runtime output archives before remote writes.

Revision ID: ae83a5f924cb
Revises: 9d72f4e813ba
"""
from alembic import op
import sqlalchemy as sa

revision='ae83a5f924cb'
down_revision='9d72f4e813ba'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('output_archive_reservations',
        sa.Column('attempt_id',sa.String(36),sa.ForeignKey('job_attempts.id'),primary_key=True),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('case_id',sa.Integer(),sa.ForeignKey('cases.id'),nullable=False),
        sa.Column('binding_json',sa.Text(),nullable=False),
        sa.Column('input_sha256',sa.String(64),nullable=False),
        sa.Column('manifest_sha256',sa.String(64),nullable=False),
        sa.Column('size',sa.BigInteger(),nullable=False),
        sa.Column('remaining_bytes',sa.BigInteger(),nullable=False),
        sa.Column('state',sa.String(16),nullable=False),
        sa.Column('writer_json',sa.Text(),nullable=False),
        sa.Column('token',sa.String(36),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('size > 0 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_output_archive_bytes'),
        sa.CheckConstraint("state IN ('active','materialized','released')",name='ck_output_archive_state'),
        sa.CheckConstraint("(state = 'active' AND remaining_bytes = size) OR (state <> 'active' AND remaining_bytes = 0)",name='ck_output_archive_release'))
    op.create_index('ix_output_archive_reservations_user_id','output_archive_reservations',['user_id'])
    op.create_index('ix_output_archive_reservations_case_id','output_archive_reservations',['case_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM output_archive_reservations')).scalar_one():
        raise RuntimeError('Downgrade would discard output archive quota ownership and settlement history')
    op.drop_table('output_archive_reservations')
