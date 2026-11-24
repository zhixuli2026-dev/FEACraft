"""Persist native scope intent before permitting its actual allocation."""
from alembic import op
import sqlalchemy as sa

revision='e8c3f09624ab'
down_revision='d7b2ef8513fa'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('cli_scope_allocations',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('binding_json',sa.Text(),nullable=False),
        sa.Column('token_sha256',sa.String(64),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('prepared_json',sa.Text(),nullable=False,server_default='{}'),
        sa.Column('allocated_json',sa.Text(),nullable=False,server_default='{}'),
        sa.Column('retirement_json',sa.Text(),nullable=False,server_default='{}'),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint('version >= 0',name='ck_cli_scope_allocation_version'))
    op.create_index('ix_cli_scope_allocations_user_id','cli_scope_allocations',['user_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM cli_scope_allocations')).scalar_one():
        raise RuntimeError('Downgrade would discard native CLI scope evidence')
    op.drop_table('cli_scope_allocations')
