"""Durable native request owners, separate from admitted Jobs.

Revision ID: 8b20d7f1c6a3
Revises: 7a91c4e2d6b0
"""
from alembic import op
import sqlalchemy as sa

revision='8b20d7f1c6a3'
down_revision='7a91c4e2d6b0'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('native_requests',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('case_id',sa.Integer(),sa.ForeignKey('cases.id'),nullable=False),
        sa.Column('created_by',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('owner_id',sa.String(128),nullable=False),
        sa.Column('owner_token_sha256',sa.String(64),nullable=False),
        sa.Column('owner_identity_json',sa.Text(),nullable=False),
        sa.Column('launch_nonce',sa.String(64),nullable=False,unique=True),
        sa.Column('kind',sa.String(30),nullable=False),
        sa.Column('profile_json',sa.Text(),nullable=False),
        sa.Column('phase',sa.String(20),nullable=False,server_default='active'),
        sa.Column('version',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('children_json',sa.Text(),nullable=False,server_default='{}'),
        sa.Column('heartbeat_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('lease_expires_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('error',sa.String(80),nullable=True),
        sa.Column('ended_at',sa.DateTime(timezone=True),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('report.prepare','latex.prepare')",name='ck_native_request_kind'),
        sa.CheckConstraint("phase IN ('active','succeeded','failed','cancelled')",name='ck_native_request_phase'),
        sa.CheckConstraint('version >= 0',name='ck_native_request_version'),
        sa.CheckConstraint("(phase='active' AND ended_at IS NULL) OR (phase!='active' AND ended_at IS NOT NULL)",name='ck_native_request_ended'))
    op.create_index('ix_native_requests_case_created','native_requests',['case_id','created_at'])
    op.create_index('ix_native_requests_owner_lease','native_requests',['owner_id','lease_expires_at'])


def downgrade():
    op.drop_table('native_requests')
