"""User-owned global runtime probes without synthetic cases.

Revision ID: ad42f9b3e8c5
Revises: 9c31e8a2d7b4
"""
from alembic import op
import sqlalchemy as sa

revision='ad42f9b3e8c5'
down_revision='9c31e8a2d7b4'
branch_labels=None
depends_on=None


def upgrade():
    with op.batch_alter_table('native_requests') as batch:
        batch.drop_constraint('ck_native_request_kind',type_='check')
        batch.alter_column('case_id',existing_type=sa.Integer(),nullable=True)
        batch.create_check_constraint('ck_native_request_kind',
            "kind IN ('report.prepare','latex.prepare','synctex.query','runtime.wsl_probe','runtime.python_probe')")
        batch.create_check_constraint('ck_native_request_scope',
            "(kind IN ('runtime.wsl_probe','runtime.python_probe') AND case_id IS NULL) OR "
            "(kind NOT IN ('runtime.wsl_probe','runtime.python_probe') AND case_id IS NOT NULL)")
    op.create_index('ix_native_requests_user_created','native_requests',['created_by','kind','created_at'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM native_requests WHERE case_id IS NULL')).scalar():
        raise RuntimeError('Export and remove global runtime request history before downgrading its ownership scope')
    op.drop_index('ix_native_requests_user_created',table_name='native_requests')
    with op.batch_alter_table('native_requests') as batch:
        batch.drop_constraint('ck_native_request_scope',type_='check')
        batch.drop_constraint('ck_native_request_kind',type_='check')
        batch.alter_column('case_id',existing_type=sa.Integer(),nullable=False)
        batch.create_check_constraint('ck_native_request_kind',"kind IN ('report.prepare','latex.prepare','synctex.query')")
