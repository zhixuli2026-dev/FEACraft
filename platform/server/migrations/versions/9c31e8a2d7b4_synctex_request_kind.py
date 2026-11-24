"""Allow fixed SyncTeX request owners; retain existing request history.

Revision ID: 9c31e8a2d7b4
Revises: 8b20d7f1c6a3
"""
from alembic import op
import sqlalchemy as sa

revision='9c31e8a2d7b4'
down_revision='8b20d7f1c6a3'
branch_labels=None
depends_on=None


def constraint(kinds):
    with op.batch_alter_table('native_requests') as batch:
        batch.drop_constraint('ck_native_request_kind',type_='check')
        batch.create_check_constraint('ck_native_request_kind',"kind IN ("+','.join("'"+kind+"'" for kind in kinds)+")")


def upgrade():
    constraint(('report.prepare','latex.prepare','synctex.query'))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT count(*) FROM native_requests WHERE kind='synctex.query'")).scalar():
        raise RuntimeError('Export and remove SyncTeX request history before downgrading its kind constraint')
    constraint(('report.prepare','latex.prepare'))
