"""Store the latest frame and presence for shared case visualization."""

from alembic import op
import sqlalchemy as sa

revision = 'f73518fa653d'
down_revision = '1be4eb58aa74'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('viz_presentations',
        sa.Column('case_id', sa.Integer, sa.ForeignKey('cases.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('presenter_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('session_id', sa.Integer, sa.ForeignKey('native_viz_sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('version', sa.String(32), nullable=False),
        sa.Column('expires_at', sa.Float, nullable=False),
        sa.Column('sequence', sa.BigInteger, nullable=False, server_default='0'),
        sa.Column('frame', sa.LargeBinary, nullable=True))
    op.create_table('viz_viewers',
        sa.Column('case_id', sa.Integer, sa.ForeignKey('viz_presentations.case_id', ondelete='CASCADE'), primary_key=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('version', sa.String(32), nullable=False),
        sa.Column('expires_at', sa.Float, nullable=False))


def downgrade():
    op.drop_table('viz_viewers')
    op.drop_table('viz_presentations')
