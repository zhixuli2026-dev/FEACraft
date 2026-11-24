"""Native session manual-review annotations, including unbound terminals.

Revision ID: b19d76e032a8
Revises: a08c65df2197
"""
from alembic import op
import sqlalchemy as sa

revision = 'b19d76e032a8'
down_revision = 'a08c65df2197'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('native_session_reviews',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('target_kind', sa.String(16), nullable=False),
        sa.Column('target_id', sa.String(36), nullable=False),
        sa.Column('case_id', sa.Integer(), sa.ForeignKey('cases.id'), nullable=True),
        sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('snapshot_sha256', sa.String(64), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("target_kind IN ('terminal','cli','viz')", name='ck_native_review_kind'),
        sa.CheckConstraint('length(target_id) > 0', name='ck_native_review_target'),
        sa.CheckConstraint('length(snapshot_sha256) = 64', name='ck_native_review_snapshot'))
    op.create_index('ix_native_review_target', 'native_session_reviews', ['actor_user_id', 'target_kind', 'target_id', 'created_at'])
    op.create_index('ix_native_review_case', 'native_session_reviews', ['case_id', 'created_at'])


def downgrade():
    op.drop_index('ix_native_review_case', table_name='native_session_reviews')
    op.drop_index('ix_native_review_target', table_name='native_session_reviews')
    op.drop_table('native_session_reviews')
