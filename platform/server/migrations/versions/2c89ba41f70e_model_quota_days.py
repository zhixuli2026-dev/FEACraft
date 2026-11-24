"""Persist daily admissions for managed model entry points."""

from alembic import op
import sqlalchemy as sa


revision = '2c89ba41f70e'
down_revision = 'd9a7c34018ef'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'model_quota_days',
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('day', sa.Date, primary_key=True),
        sa.Column('kind', sa.String(20), primary_key=True),
        sa.Column('used', sa.Integer, nullable=False),
        sa.CheckConstraint('used > 0', name='ck_model_quota_days_used'),
    )


def downgrade():
    op.drop_table('model_quota_days')
