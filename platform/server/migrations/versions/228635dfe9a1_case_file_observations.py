"""Persist the last observed filesystem tree per case."""
from alembic import op
import sqlalchemy as sa

revision = '228635dfe9a1'
down_revision = 'e42a60d73fb1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('case_file_observations',
        sa.Column('case_id', sa.Integer(), sa.ForeignKey('cases.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('entries_json', sa.Text()),
    )


def downgrade():
    op.drop_table('case_file_observations')
