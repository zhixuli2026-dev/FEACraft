"""Retain model preferences independently for each Agent backend."""

from alembic import op
import sqlalchemy as sa


revision = 'd9a7c34018ef'
down_revision = 'f73518fa653d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('conversation_model_preferences',
        sa.Column('conversation_id', sa.Integer, sa.ForeignKey('conversations.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('runtime', sa.String(12), primary_key=True),
        sa.Column('provider_profile_id', sa.Integer, sa.ForeignKey('provider_profiles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('profile_fingerprint', sa.String(64), nullable=False),
        sa.Column('model', sa.String(120), nullable=False),
        sa.Column('options_json', sa.Text(), nullable=False))
    op.create_index('ix_conversation_model_preferences_provider_profile_id',
                    'conversation_model_preferences', ['provider_profile_id'])


def downgrade():
    op.drop_table('conversation_model_preferences')
