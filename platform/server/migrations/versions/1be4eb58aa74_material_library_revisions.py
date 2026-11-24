"""Store immutable workspace material library revisions."""

from alembic import op
import sqlalchemy as sa


revision = '1be4eb58aa74'
down_revision = '339746ef01b2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'material_library_revisions',
        sa.Column('workspace_id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('material_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('workspace_id', 'key', 'revision'),
    )


def downgrade():
    op.drop_table('material_library_revisions')
