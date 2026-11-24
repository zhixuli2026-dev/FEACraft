"""Add durable typed workflow groups and case-bound memberships."""
from alembic import op
import sqlalchemy as sa

revision='f9d4a10735bc'
down_revision='e8c3f09624ab'
branch_labels=None
depends_on=None


def upgrade():
    op.create_index('uq_workflow_run_case','workflow_runs',['id','case_id'],unique=True)
    op.create_table('workflow_batches',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('created_by',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('owner_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('idempotency_key',sa.String(128),nullable=False),
        sa.Column('request_hash',sa.String(64),nullable=False),
        sa.Column('request_json',sa.Text(),nullable=False),
        sa.Column('membership_json',sa.Text(),nullable=False),
        sa.Column('membership_sha256',sa.String(64),nullable=False),
        sa.Column('max_concurrent',sa.Integer(),nullable=False),
        sa.Column('delivery_mode',sa.String(8),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('cancel_requested_at',sa.DateTime(timezone=True),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint('created_by','idempotency_key',name='uq_workflow_batch_request'),
        sa.CheckConstraint('max_concurrent BETWEEN 1 AND 4',name='ck_workflow_batch_capacity'),
        sa.CheckConstraint("delivery_mode IN ('local','redis')",name='ck_workflow_batch_mode'),
        sa.CheckConstraint('version >= 0',name='ck_workflow_batch_version'))
    op.create_index('ix_workflow_batch_creator_created','workflow_batches',['created_by','created_at'])
    op.create_table('workflow_batch_members',
        sa.Column('run_id',sa.String(36),primary_key=True),
        sa.Column('batch_id',sa.String(36),sa.ForeignKey('workflow_batches.id'),nullable=False),
        sa.Column('ordinal',sa.Integer(),nullable=False),
        sa.Column('member_key',sa.String(80),nullable=False),
        sa.Column('case_id',sa.Integer(),sa.ForeignKey('cases.id'),nullable=False),
        sa.Column('request_hash',sa.String(64),nullable=False),
        sa.ForeignKeyConstraint(['run_id','case_id'],['workflow_runs.id','workflow_runs.case_id'],name='fk_workflow_batch_run_case'),
        sa.UniqueConstraint('batch_id','ordinal',name='uq_workflow_batch_ordinal'),
        sa.UniqueConstraint('batch_id','member_key',name='uq_workflow_batch_member_key'),
        sa.UniqueConstraint('batch_id','case_id',name='uq_workflow_batch_member_case'),
        sa.CheckConstraint('ordinal BETWEEN 0 AND 49',name='ck_workflow_batch_ordinal'))
    op.create_index('ix_workflow_batch_members_batch_id','workflow_batch_members',['batch_id'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM workflow_batches')).scalar_one():
        raise RuntimeError('Downgrade would discard typed workflow batch evidence')
    op.drop_table('workflow_batch_members')
    op.drop_table('workflow_batches')
    op.drop_index('uq_workflow_run_case',table_name='workflow_runs')
