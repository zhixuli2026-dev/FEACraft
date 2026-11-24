"""Durable remaining upload payload reservations.

Revision ID: 8c61e3d702a9
Revises: 7b50d2c6f198
"""
from alembic import op
import sqlalchemy as sa

revision='8c61e3d702a9'
down_revision='7b50d2c6f198'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('upload_storage_reservations',
        sa.Column('id',sa.String(32),primary_key=True),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('case_id',sa.Integer(),sa.ForeignKey('cases.id'),nullable=False),
        sa.Column('binding_json',sa.Text(),nullable=False),
        sa.Column('path',sa.Text(),nullable=False),
        sa.Column('size',sa.BigInteger(),nullable=False),
        sa.Column('remaining_bytes',sa.BigInteger(),nullable=False),
        sa.Column('state',sa.String(16),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('size > 0 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_upload_storage_bytes'),
        sa.CheckConstraint("state IN ('active','completed','cancelled')",name='ck_upload_storage_state'),
        sa.CheckConstraint("state = 'active' OR remaining_bytes = 0",name='ck_upload_storage_release'))
    op.create_index('ix_upload_storage_reservations_user_id','upload_storage_reservations',['user_id'])
    op.create_index('ix_upload_storage_reservations_case_id','upload_storage_reservations',['case_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM upload_storage_reservations')).scalar_one():
        raise RuntimeError('Downgrade would discard upload quota ownership and settlement history')
    op.drop_table('upload_storage_reservations')
