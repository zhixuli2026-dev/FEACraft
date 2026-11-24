"""Retain the exact worker birth identity at the exclusive Job claim."""
from alembic import op
import sqlalchemy as sa

revision = '4e2da9f3c865'
down_revision = '3d1c98e2b754'
branch_labels = None
depends_on = None


def upgrade():
    # Existing claims remain unverified; never infer a birth marker from a PID.
    op.add_column('job_attempts', sa.Column('worker_identity_json', sa.Text(),
                                          nullable=False, server_default='{}'))


def downgrade():
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM job_attempts WHERE worker_identity_json <> '{}' LIMIT 1")).first():
        raise RuntimeError('Refusing to discard Job worker ownership history')
    op.drop_column('job_attempts', 'worker_identity_json')
