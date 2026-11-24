"""Explicit native session successor lineage, independent of execution journals.

Revision ID: c20e87f143b9
Revises: b19d76e032a8
"""
from alembic import op
import sqlalchemy as sa

revision = 'c20e87f143b9'
down_revision = 'b19d76e032a8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('native_session_links',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('source_id', sa.String(36), nullable=False),
        sa.Column('successor_id', sa.String(36), nullable=False),
        sa.Column('case_id', sa.Integer(), sa.ForeignKey('cases.id'), nullable=True),
        sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('terminal','cli','viz')", name='ck_native_link_kind'),
        sa.CheckConstraint('length(source_id)>0 AND length(successor_id)>0 AND source_id!=successor_id', name='ck_native_link_targets'),
        sa.UniqueConstraint('kind', 'successor_id', name='uq_native_link_successor'))
    op.create_index('ix_native_link_source', 'native_session_links', ['actor_user_id', 'kind', 'source_id', 'created_at'])
    op.create_index('ix_native_link_case', 'native_session_links', ['case_id', 'created_at'])


def downgrade():
    op.drop_index('ix_native_link_case', table_name='native_session_links')
    op.drop_index('ix_native_link_source', table_name='native_session_links')
    op.drop_table('native_session_links')
