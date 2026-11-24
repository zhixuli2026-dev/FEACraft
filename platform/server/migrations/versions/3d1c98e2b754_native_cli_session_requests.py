"""Persist idempotent CLI startup before any process can be released."""
from alembic import op
import sqlalchemy as sa

revision = '3d1c98e2b754'
down_revision = '2c0b87d1a643'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('native_cli_session_requests',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('case_id',sa.Integer(),sa.ForeignKey('cases.id'),nullable=False),
        sa.Column('conversation_id',sa.Integer(),sa.ForeignKey('conversations.id'),nullable=False),
        sa.Column('created_by',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('client_request_id',sa.String(128),nullable=False),
        sa.Column('request_sha256',sa.String(64),nullable=False),
        sa.Column('request_json',sa.Text(),nullable=False),
        sa.Column('selection_json',sa.Text(),nullable=False),
        sa.Column('session_id',sa.String(36),sa.ForeignKey('native_cli_sessions.id'),unique=True),
        sa.Column('phase',sa.String(24),nullable=False,server_default='admitted'),
        sa.Column('version',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('not_started',sa.Boolean(),nullable=False,server_default=sa.false()),
        sa.Column('error',sa.String(80)),
        sa.Column('ended_at',sa.DateTime(timezone=True)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint('conversation_id','created_by','client_request_id',name='uq_cli_session_request'),
        sa.CheckConstraint("phase IN ('admitted','starting','completed','failed','cancelled','recovery_required')",name='ck_cli_session_request_phase'),
        sa.CheckConstraint('version >= 0 AND length(client_request_id)>0 AND length(request_sha256)=64',name='ck_cli_session_request_values'),
        sa.CheckConstraint("phase != 'completed' OR session_id IS NOT NULL",name='ck_cli_session_request_completed'),
        sa.CheckConstraint('session_id IS NULL OR session_id=id',name='ck_cli_session_request_binding'),
        sa.CheckConstraint("NOT not_started OR (session_id IS NULL AND phase IN ('failed','cancelled'))",name='ck_cli_session_request_not_started'),
        sa.CheckConstraint("(phase IN ('completed','failed','cancelled','recovery_required') AND ended_at IS NOT NULL) OR (phase IN ('admitted','starting') AND ended_at IS NULL)",name='ck_cli_session_request_ended'))
    op.create_index('ix_cli_session_request_conversation','native_cli_session_requests',['conversation_id','created_at'])

def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM native_cli_session_requests LIMIT 1')).first():
        raise RuntimeError('Refusing to discard native session request history')
    op.drop_table('native_cli_session_requests')
