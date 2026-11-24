"""Bind future upload payload to one durable owner write pool."""
from alembic import op
import sqlalchemy as sa
revision='e2c7e93068af'
down_revision='d1b6d82957fe'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('upload_chunk_credits',
        sa.Column('id',sa.String(36),sa.ForeignKey('owner_storage_reservations.id'),primary_key=True),
        sa.Column('upload_id',sa.String(32),sa.ForeignKey('upload_storage_reservations.id'),nullable=False),
        sa.Column('binding_json',sa.Text(),nullable=False),
        sa.Column('offset',sa.BigInteger(),nullable=False),
        sa.Column('size',sa.BigInteger(),nullable=False),
        sa.Column('amount',sa.BigInteger(),nullable=False),
        sa.Column('sha256',sa.String(64),nullable=False),
        sa.Column('state',sa.String(16),nullable=False),
        sa.Column('receipt_json',sa.Text(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('"offset" >= 0 AND size > 0 AND amount >= 0 AND amount <= size',name='ck_upload_chunk_bytes'),
        sa.CheckConstraint("state IN ('active','settled')",name='ck_upload_chunk_state'),
        sa.CheckConstraint("state = 'active' OR receipt_json <> '{}'",name='ck_upload_chunk_receipt'))
    op.create_index('ix_upload_chunk_credits_upload_id','upload_chunk_credits',['upload_id'])
    op.create_index('uq_upload_chunk_active','upload_chunk_credits',['upload_id'],unique=True,
        sqlite_where=sa.text("state = 'active'"),postgresql_where=sa.text("state = 'active'"))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM upload_chunk_credits')).scalar_one():
        raise RuntimeError('Downgrade would discard upload payload transfer and recovery evidence')
    op.drop_table('upload_chunk_credits')
