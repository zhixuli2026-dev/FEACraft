"""Store encrypted SCNET account sessions with a per-session renewal lease."""

from alembic import op
import sqlalchemy as sa

revision = 'f8c3d9486581'
down_revision = 'f71a46d08e9b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('scnet_account_sessions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('state_cipher', sa.Text(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.Integer(), nullable=False),
        sa.Column('renewal_until', sa.Integer(), nullable=False))
    op.create_index('ix_scnet_account_owner_expiry', 'scnet_account_sessions', ['user_id', 'expires_at'])


def downgrade():
    op.drop_index('ix_scnet_account_owner_expiry', table_name='scnet_account_sessions')
    op.drop_table('scnet_account_sessions')
