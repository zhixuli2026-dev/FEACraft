"""Reserve each physical PUT before a durable storage operation sends it."""
from alembic import op
import sqlalchemy as sa

revision='c0a5c71846ed'
down_revision='bf94b60735dc'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('storage_write_reservations',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('operation_id',sa.String(36),sa.ForeignKey('case_storage_operations.id'),nullable=False),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('case_id',sa.Integer(),sa.ForeignKey('cases.id'),nullable=False),
        sa.Column('binding_json',sa.Text(),nullable=False),
        sa.Column('request_json',sa.Text(),nullable=False),
        sa.Column('writer_json',sa.Text(),nullable=False),
        sa.Column('size',sa.BigInteger(),nullable=False),
        sa.Column('remaining_bytes',sa.BigInteger(),nullable=False),
        sa.Column('state',sa.String(16),nullable=False),
        sa.Column('receipt_json',sa.Text(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('size >= 0 AND remaining_bytes >= 0 AND remaining_bytes <= size',name='ck_storage_write_bytes'),
        sa.CheckConstraint("state IN ('active','settled')",name='ck_storage_write_state'),
        sa.CheckConstraint("(state = 'active' AND remaining_bytes = size AND receipt_json = '{}') OR (state = 'settled' AND remaining_bytes = 0 AND receipt_json <> '{}')",name='ck_storage_write_receipt'))
    for column in ('operation_id','user_id','case_id'):
        op.create_index('ix_storage_write_reservations_'+column,'storage_write_reservations',[column])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM storage_write_reservations')).scalar_one():
        raise RuntimeError('Downgrade would discard remote write capacity and acknowledgement history')
    op.drop_table('storage_write_reservations')
