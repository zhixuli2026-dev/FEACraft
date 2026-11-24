"""Fence unlocked storage quota inventories against owner write admission."""
from alembic import op
import sqlalchemy as sa

revision = '339746ef01b2'
down_revision = '228635dfe9a1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('storage_quota_epochs',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True, autoincrement=False),
        sa.Column('revision', sa.BigInteger(), nullable=False),
    )


def downgrade():
    op.drop_table('storage_quota_epochs')
