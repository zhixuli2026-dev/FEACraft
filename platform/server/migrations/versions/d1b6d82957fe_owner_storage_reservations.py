"""Owner capacity independent of an uncommitted newly created case."""
from alembic import op
import sqlalchemy as sa
revision='d1b6d82957fe'
down_revision='c0a5c71846ed'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('owner_storage_reservations',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('binding_json',sa.Text(),nullable=False),
        sa.Column('writer_json',sa.Text(),nullable=False),
        sa.Column('size',sa.BigInteger(),nullable=False),
        sa.Column('remaining_bytes',sa.BigInteger(),nullable=False),
        sa.Column('state',sa.String(16),nullable=False),
        sa.Column('receipt_json',sa.Text(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('size >= 0 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_owner_storage_bytes'),
        sa.CheckConstraint("state IN ('active','settled')",name='ck_owner_storage_state'),
        sa.CheckConstraint("(state = 'active') OR (remaining_bytes = 0 AND receipt_json <> '{}')",name='ck_owner_storage_receipt'))
    op.create_index('ix_owner_storage_reservations_user_id','owner_storage_reservations',['user_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM owner_storage_reservations')).scalar_one():
        raise RuntimeError('Downgrade would discard owner storage capacity and remote fencing history')
    op.drop_table('owner_storage_reservations')
