"""Persist publication copy capacity and its exclusive process owner.

Revision ID: 9d72f4e813ba
Revises: 8c61e3d702a9
"""
from alembic import op
import sqlalchemy as sa

revision='9d72f4e813ba'
down_revision='8c61e3d702a9'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('publication_storage_reservations',
        sa.Column('publication_id',sa.String(36),sa.ForeignKey('artifact_publications.id'),primary_key=True),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('case_id',sa.Integer(),sa.ForeignKey('cases.id'),nullable=False),
        sa.Column('binding_json',sa.Text(),nullable=False),
        sa.Column('manifest_sha256',sa.String(64),nullable=False),
        sa.Column('size',sa.BigInteger(),nullable=False),
        sa.Column('remaining_bytes',sa.BigInteger(),nullable=False),
        sa.Column('state',sa.String(16),nullable=False),
        sa.Column('writer_json',sa.Text(),nullable=False),
        sa.Column('token',sa.String(36),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('size > 0 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_publication_storage_bytes'),
        sa.CheckConstraint("state IN ('active','materialized','released')",name='ck_publication_storage_state'),
        sa.CheckConstraint("(state = 'active' AND remaining_bytes = size) OR (state <> 'active' AND remaining_bytes = 0)",name='ck_publication_storage_release'))
    op.create_index('ix_publication_storage_reservations_user_id','publication_storage_reservations',['user_id'])
    op.create_index('ix_publication_storage_reservations_case_id','publication_storage_reservations',['case_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM publication_storage_reservations')).scalar_one():
        raise RuntimeError('Downgrade would discard publication quota ownership and settlement history')
    op.drop_table('publication_storage_reservations')
