"""Persist owned rendering sessions without changing terminal history.

Revision ID: c64b21d5a0e7
Revises: b53a10c4f9d6
"""
from alembic import op
import sqlalchemy as sa

revision='c64b21d5a0e7'
down_revision='b53a10c4f9d6'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('native_viz_sessions',
        sa.Column('id',sa.Integer(),primary_key=True,autoincrement=True),
        sa.Column('case_id',sa.Integer(),sa.ForeignKey('cases.id'),nullable=False),
        sa.Column('created_by',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('owner_id',sa.String(128),nullable=False),
        sa.Column('owner_token_sha256',sa.String(64),nullable=False),
        sa.Column('owner_identity_json',sa.Text(),nullable=False),
        sa.Column('launch_nonce',sa.String(64),nullable=False,unique=True),
        sa.Column('profile_json',sa.Text(),nullable=False),
        sa.Column('phase',sa.String(20),nullable=False,server_default='starting'),
        sa.Column('version',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('process_json',sa.Text(),nullable=False,server_default='{}'),
        sa.Column('ready_json',sa.Text(),nullable=False,server_default='{}'),
        sa.Column('stop_json',sa.Text(),nullable=False,server_default='{}'),
        sa.Column('heartbeat_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('lease_expires_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('last_activity_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('exit_code',sa.Integer(),nullable=True),
        sa.Column('error',sa.String(80),nullable=True),
        sa.Column('ended_at',sa.DateTime(timezone=True),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("phase IN ('starting','active','closed','failed')",name='ck_native_viz_phase'),
        sa.CheckConstraint('version >= 0',name='ck_native_viz_version'),
        sa.CheckConstraint("(phase IN ('starting','active') AND ended_at IS NULL) OR (phase IN ('closed','failed') AND ended_at IS NOT NULL)",name='ck_native_viz_ended'))
    op.create_index('ix_native_viz_user_created','native_viz_sessions',['created_by','created_at'])
    op.create_index('ix_native_viz_case_created','native_viz_sessions',['case_id','created_at'])
    op.create_index('ix_native_viz_owner_lease','native_viz_sessions',['owner_id','lease_expires_at'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM native_viz_sessions')).scalar_one():
        raise RuntimeError('Export and remove visualization history before downgrading its ownership table')
    op.drop_table('native_viz_sessions')
