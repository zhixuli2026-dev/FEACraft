"""Bind external scheduler work to a durable case and exact submission."""

from alembic import op
import sqlalchemy as sa

revision = 'f71a46d08e9b'
down_revision = 'e87ba491d832'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('remote_hpc_runs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('case_id', sa.Integer(), sa.ForeignKey('cases.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('profile', sa.String(64), nullable=False),
        sa.Column('profile_sha256', sa.String(64), nullable=False),
        sa.Column('request_key', sa.String(160), nullable=False),
        sa.Column('request_sha256', sa.String(64), nullable=False),
        sa.Column('request_json', sa.Text(), nullable=False),
        sa.Column('input_json', sa.Text(), nullable=False),
        sa.Column('source_run_id', sa.String(36), sa.ForeignKey('remote_hpc_runs.id'), nullable=True),
        sa.Column('scheduler_id', sa.String(128), nullable=True),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('observation_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('case_id', 'user_id', 'request_key', name='uq_remote_hpc_request'))
    op.create_index('ix_remote_hpc_case_owner', 'remote_hpc_runs', ['case_id', 'user_id', 'created_at'])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT 1 FROM remote_hpc_runs LIMIT 1')):
        raise RuntimeError('Retire external scheduler jobs before downgrading')
    op.drop_index('ix_remote_hpc_case_owner', table_name='remote_hpc_runs')
    op.drop_table('remote_hpc_runs')
