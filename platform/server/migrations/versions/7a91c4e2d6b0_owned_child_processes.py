"""Persistent attempt child execution identities and cleanup receipts.

Revision ID: 7a91c4e2d6b0
Revises: 4c02b1a7d9e0
"""
from alembic import op
import sqlalchemy as sa

revision='7a91c4e2d6b0'
down_revision='4c02b1a7d9e0'
branch_labels=None
depends_on=None


def upgrade():
    op.add_column('job_attempts',sa.Column('child_processes_json',sa.Text(),nullable=False,server_default='{}'))


def downgrade():
    op.drop_column('job_attempts','child_processes_json')
