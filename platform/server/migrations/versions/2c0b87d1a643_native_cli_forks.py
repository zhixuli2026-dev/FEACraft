"""Persist native branch admission and uncertain creation outcomes."""
from alembic import op
import sqlalchemy as sa

revision = '2c0b87d1a643'
down_revision = '1b9a76c0f532'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('native_cli_forks',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('source_conversation_id',sa.Integer(),sa.ForeignKey('conversations.id'),nullable=False),
        sa.Column('conversation_id',sa.Integer(),sa.ForeignKey('conversations.id'),nullable=False,unique=True),
        sa.Column('source_turn_id',sa.String(36),sa.ForeignKey('native_cli_turns.id'),nullable=False),
        sa.Column('boundary_event_id',sa.BigInteger().with_variant(sa.Integer(),'sqlite'),sa.ForeignKey('agent_events.id'),nullable=False),
        sa.Column('case_id',sa.Integer(),sa.ForeignKey('cases.id'),nullable=False),
        sa.Column('created_by',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('client_request_id',sa.String(128),nullable=False),
        sa.Column('request_sha256',sa.String(64),nullable=False),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('native_cli_sessions.id')),
        sa.Column('thread_id',sa.String(128)),
        sa.Column('phase',sa.String(24),nullable=False,server_default='admitted'),
        sa.Column('version',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('error',sa.String(80)),
        sa.Column('ended_at',sa.DateTime(timezone=True)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint('source_conversation_id','created_by','client_request_id',name='uq_cli_fork_request'),
        sa.CheckConstraint('source_conversation_id != conversation_id',name='ck_cli_fork_distinct'),
        sa.CheckConstraint("phase IN ('admitted','forking','registered','completed','failed','recovery_required')",name='ck_cli_fork_phase'),
        sa.CheckConstraint('version >= 0 AND length(client_request_id)>0 AND length(request_sha256)=64',name='ck_cli_fork_values'),
        sa.CheckConstraint('thread_id IS NULL OR (session_id IS NOT NULL AND length(thread_id)>0)',name='ck_cli_fork_thread'),
        sa.CheckConstraint("phase NOT IN ('forking','registered','completed') OR session_id IS NOT NULL",name='ck_cli_fork_session'),
        sa.CheckConstraint("phase NOT IN ('registered','completed') OR thread_id IS NOT NULL",name='ck_cli_fork_registered'),
        sa.CheckConstraint("(phase IN ('completed','failed','recovery_required') AND ended_at IS NOT NULL) OR (phase IN ('admitted','forking','registered') AND ended_at IS NULL)",name='ck_cli_fork_ended'))
    op.create_index('ix_cli_fork_source_created','native_cli_forks',['source_conversation_id','created_at'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM native_cli_forks LIMIT 1')).first():
        raise RuntimeError('Refusing to discard native fork history')
    op.drop_table('native_cli_forks')
