"""Persist native CLI ownership and pre-release registrations.

Revision ID: e86d43f7c209
Revises: d75c32e6b1f8
"""
from alembic import op
import sqlalchemy as sa

revision = 'e86d43f7c209'
down_revision = 'd75c32e6b1f8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('native_cli_sessions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('case_id', sa.Integer(), sa.ForeignKey('cases.id'), nullable=True),
        sa.Column('conversation_id', sa.Integer(), sa.ForeignKey('conversations.id'), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('owner_id', sa.String(128), nullable=False),
        sa.Column('owner_token_sha256', sa.String(64), nullable=False),
        sa.Column('owner_identity_json', sa.Text(), nullable=False),
        sa.Column('launch_nonce', sa.String(64), nullable=False, unique=True),
        sa.Column('runtime', sa.String(20), nullable=False),
        sa.Column('profile_json', sa.Text(), nullable=False),
        sa.Column('phase', sa.String(20), nullable=False, server_default='starting'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('process_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('initialize_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('stop_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('thread_id', sa.String(128), nullable=True),
        sa.Column('thread_state', sa.String(20), nullable=False, server_default='none'),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('exit_code', sa.Integer(), nullable=True),
        sa.Column('error', sa.String(80), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("runtime='windows'", name='ck_native_cli_runtime'),
        sa.CheckConstraint('conversation_id IS NULL OR case_id IS NOT NULL', name='ck_native_cli_scope'),
        sa.CheckConstraint("phase IN ('starting','active','closed','failed')", name='ck_native_cli_phase'),
        sa.CheckConstraint('version >= 0', name='ck_native_cli_version'),
        sa.CheckConstraint("(phase IN ('starting','active') AND ended_at IS NULL) OR (phase IN ('closed','failed') AND ended_at IS NOT NULL)", name='ck_native_cli_ended'),
        sa.CheckConstraint("(thread_state IN ('none','starting') AND thread_id IS NULL) OR (thread_state='ready' AND thread_id IS NOT NULL AND length(thread_id)>0)", name='ck_native_cli_thread'))
    for name, fields in (('user_created', ['created_by', 'created_at']), ('case_created', ['case_id', 'created_at']),
                         ('conversation', ['conversation_id']), ('owner_lease', ['owner_id', 'lease_expires_at'])):
        op.create_index('ix_native_cli_' + name, 'native_cli_sessions', fields)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM native_cli_sessions')).scalar_one():
        raise RuntimeError('Export and explicitly remove native CLI history before downgrading its ownership table')
    op.drop_table('native_cli_sessions')
