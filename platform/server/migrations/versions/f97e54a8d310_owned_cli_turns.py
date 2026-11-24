"""Durable CLI turns, events, approvals and shared conversation execution fence."""
from alembic import op
import sqlalchemy as sa

revision = 'f97e54a8d310'
down_revision = 'e86d43f7c209'
branch_labels = None
depends_on = None


def stamps():
    return [sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
            for name in ('created_at', 'updated_at')]


def upgrade():
    op.create_table('conversation_executions',
        sa.Column('conversation_id', sa.Integer(), sa.ForeignKey('conversations.id'), primary_key=True),
        sa.Column('phase', sa.String(20), nullable=False, server_default='idle'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('backend', sa.String(20)), sa.Column('resource_id', sa.String(128)),
        sa.Column('owner_id', sa.String(128)), sa.Column('owner_token_sha256', sa.String(64)),
        sa.Column('owner_identity_json', sa.Text()), sa.Column('lease_expires_at', sa.DateTime(timezone=True)),
        *stamps(),
        sa.CheckConstraint("phase IN ('idle','active','recovery_required')", name='ck_conversation_execution_phase'),
        sa.CheckConstraint('version >= 0', name='ck_conversation_execution_version'),
        sa.CheckConstraint("backend IS NULL OR backend IN ('api','goal','approval','codex')", name='ck_conversation_execution_backend'),
        sa.CheckConstraint("(phase='idle' AND backend IS NULL AND resource_id IS NULL AND owner_id IS NULL AND owner_token_sha256 IS NULL AND owner_identity_json IS NULL AND lease_expires_at IS NULL) OR (phase!='idle' AND backend IS NOT NULL AND resource_id IS NOT NULL AND owner_id IS NOT NULL AND owner_token_sha256 IS NOT NULL AND owner_identity_json IS NOT NULL AND lease_expires_at IS NOT NULL)", name='ck_conversation_execution_owner'))
    op.create_table('native_cli_turns',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('native_cli_sessions.id'), nullable=False),
        sa.Column('case_id', sa.Integer(), sa.ForeignKey('cases.id'), nullable=False),
        sa.Column('conversation_id', sa.Integer(), sa.ForeignKey('conversations.id'), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('client_message_id', sa.String(128), nullable=False), sa.Column('request_sha256', sa.String(64), nullable=False),
        sa.Column('mode', sa.String(20), nullable=False), sa.Column('phase', sa.String(24), nullable=False, server_default='admitted'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_seq', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('event_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('thread_id', sa.String(128)), sa.Column('native_turn_id', sa.String(128)), sa.Column('native_status', sa.String(20)),
        sa.Column('attempts_json', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('error', sa.String(80)), sa.Column('ended_at', sa.DateTime(timezone=True)), *stamps(),
        sa.UniqueConstraint('conversation_id', 'created_by', 'client_message_id', name='uq_cli_turn_client_message'),
        sa.CheckConstraint("mode IN ('message','goal')", name='ck_cli_turn_mode'),
        sa.CheckConstraint("phase IN ('admitted','starting','running','awaiting_input','finalizing','completed','failed','cancelled','interrupted')", name='ck_cli_turn_phase'),
        sa.CheckConstraint('version >= 0 AND next_seq > 0 AND event_bytes >= 0', name='ck_cli_turn_counters'),
        sa.CheckConstraint("native_status IS NULL OR native_status IN ('completed','failed','interrupted')", name='ck_cli_turn_native_status'),
        sa.CheckConstraint('native_turn_id IS NULL OR thread_id IS NOT NULL', name='ck_cli_turn_thread'),
        sa.CheckConstraint("(phase IN ('completed','failed','cancelled','interrupted') AND ended_at IS NOT NULL) OR (phase NOT IN ('completed','failed','cancelled','interrupted') AND ended_at IS NULL)", name='ck_cli_turn_ended'))
    op.create_index('ix_cli_turn_conversation_created', 'native_cli_turns', ['conversation_id', 'created_at'])
    op.create_index('ix_cli_turn_session', 'native_cli_turns', ['session_id'])
    op.create_table('native_cli_events',
        sa.Column('turn_id', sa.String(36), sa.ForeignKey('native_cli_turns.id'), primary_key=True),
        sa.Column('seq', sa.Integer(), primary_key=True), sa.Column('type', sa.String(80), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('seq > 0', name='ck_cli_event_sequence'))
    op.create_table('native_cli_approvals',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('turn_id', sa.String(36), sa.ForeignKey('native_cli_turns.id'), nullable=False),
        sa.Column('request_key', sa.String(160), nullable=False), sa.Column('request_id_json', sa.Text(), nullable=False),
        sa.Column('method', sa.String(80), nullable=False), sa.Column('params_json', sa.Text(), nullable=False),
        sa.Column('request_sha256', sa.String(64), nullable=False),
        sa.Column('phase', sa.String(20), nullable=False, server_default='pending'), sa.Column('response_json', sa.Text()), *stamps(),
        sa.UniqueConstraint('turn_id', 'request_key', name='uq_cli_approval_request'),
        sa.CheckConstraint("phase IN ('pending','claimed','sent','expired')", name='ck_cli_approval_phase'),
        sa.CheckConstraint("method IN ('item/commandExecution/requestApproval','item/fileChange/requestApproval','item/permissions/requestApproval','item/tool/requestUserInput')", name='ck_cli_approval_method'))


def downgrade():
    connection = op.get_bind()
    if (connection.execute(sa.text('SELECT count(*) FROM native_cli_turns')).scalar_one()
            or connection.execute(sa.text("SELECT count(*) FROM conversation_executions WHERE phase!='idle'")).scalar_one()):
        raise RuntimeError('Export and explicitly reconcile CLI turns and active conversation owners before downgrade')
    for name in ('native_cli_approvals', 'native_cli_events', 'native_cli_turns', 'conversation_executions'):
        op.drop_table(name)
