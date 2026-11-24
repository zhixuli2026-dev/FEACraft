"""Keep Job write-drain evidence independent of artifact validation snapshots."""
from alembic import op
import sqlalchemy as sa

revision = '6a4fc1b5e087'
down_revision = '5f3eb0a4d976'
branch_labels = None
depends_on = None


def upgrade():
    # Old attempts remain unknown; terminal status is not evidence of drainage.
    op.add_column('job_attempts', sa.Column('write_checkpoint_json', sa.Text(),
                                          nullable=False, server_default='{}'))


def downgrade():
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM job_attempts WHERE write_checkpoint_json <> '{}' LIMIT 1")).first():
        raise RuntimeError('Refusing to discard Job write-drain history')
    op.drop_column('job_attempts', 'write_checkpoint_json')
