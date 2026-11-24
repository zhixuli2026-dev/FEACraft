"""Durable ACP thread bindings and idempotent turn receipts."""

from alembic import op
import sqlalchemy as sa


revision = '8da5cff5b71b'
down_revision = '56e2d59f03c1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('acp_threads',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('conversation_id', sa.Integer, sa.ForeignKey('conversations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_by', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('harness', sa.String(16), nullable=False),
        sa.Column('runtime', sa.String(16), nullable=False),
        sa.Column('profile_id', sa.Integer, nullable=False),
        sa.Column('profile_sha256', sa.String(64), nullable=False),
        sa.Column('host_id', sa.String(128), nullable=False),
        sa.Column('contract_text', sa.Text, nullable=True),
        sa.Column('allow_drafts', sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column('native_id', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("harness IN ('opencode','kimi')", name='ck_acp_thread_harness'),
        sa.CheckConstraint("runtime IN ('windows','linux','wsl')", name='ck_acp_thread_runtime'),
        sa.CheckConstraint('length(profile_sha256) = 64', name='ck_acp_thread_profile'))
    op.create_index('ix_acp_thread_conversation', 'acp_threads', ['conversation_id', 'created_by', 'created_at'])
    op.create_table('acp_turns',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('thread_id', sa.String(36), sa.ForeignKey('acp_threads.id', ondelete='CASCADE'), nullable=False),
        sa.Column('conversation_id', sa.Integer, sa.ForeignKey('conversations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_by', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('client_message_id', sa.String(128), nullable=False),
        sa.Column('request_sha256', sa.String(64), nullable=False),
        sa.Column('phase', sa.String(20), nullable=False),
        sa.Column('process_json', sa.Text, nullable=True),
        sa.Column('stop_json', sa.Text, nullable=True),
        sa.Column('error', sa.String(80), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('conversation_id', 'created_by', 'client_message_id', name='uq_acp_turn_message'),
        sa.CheckConstraint("phase IN ('admitted','running','completed','failed','cancelled','recovery_required')",
                           name='ck_acp_turn_phase'),
        sa.CheckConstraint('length(request_sha256) = 64', name='ck_acp_turn_request'))
    op.create_index('ix_acp_turn_thread', 'acp_turns', ['thread_id', 'created_at'])


def downgrade():
    op.drop_index('ix_acp_turn_thread', table_name='acp_turns')
    op.drop_table('acp_turns')
    op.drop_index('ix_acp_thread_conversation', table_name='acp_threads')
    op.drop_table('acp_threads')
