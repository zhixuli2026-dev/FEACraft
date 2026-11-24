"""Durable native CLI initialization peak admission."""
from alembic import op
import sqlalchemy as sa
revision='d7b2ef8513fa'
down_revision='c6a1de7402ef'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('cli_initialization_reservations',
        sa.Column('session_id',sa.String(36),sa.ForeignKey('native_cli_sessions.id'),primary_key=True),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('binding_json',sa.Text(),nullable=False),
        sa.Column('size',sa.BigInteger(),nullable=False),
        sa.Column('remaining_bytes',sa.BigInteger(),nullable=False),
        sa.Column('state',sa.String(16),nullable=False),
        sa.Column('receipt_json',sa.Text(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('size >= 0 AND size <= 32768 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_cli_init_bytes'),
        sa.CheckConstraint("state IN ('active','settled')",name='ck_cli_init_state'),
        sa.CheckConstraint("(state = 'active' AND remaining_bytes = size AND receipt_json = '{}') OR (state = 'settled' AND remaining_bytes = 0 AND receipt_json <> '{}')",name='ck_cli_init_receipt'))
    op.create_index('ix_cli_initialization_reservations_user_id','cli_initialization_reservations',['user_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM cli_initialization_reservations')).scalar_one():
        raise RuntimeError('Downgrade would discard native CLI capacity evidence')
    op.drop_table('cli_initialization_reservations')
