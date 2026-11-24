"""Register sandbox recovery requests and commit their artifact receipts."""
from alembic import op
import sqlalchemy as sa

revision = 'a4e9bc5280cd'
down_revision = 'f3d8fa4179bc'
branch_labels = None
depends_on = None
KINDS = "'report.prepare','latex.prepare','synctex.query','runtime.wsl_probe','runtime.python_probe','agent.shell','terminal.export'"


def upgrade():
    with op.batch_alter_table('native_requests') as batch:
        batch.add_column(sa.Column('result_json', sa.Text(), nullable=False, server_default='{}'))
        batch.drop_constraint('ck_native_request_kind', type_='check')
        batch.create_check_constraint('ck_native_request_kind', 'kind IN (' + KINDS + ",'terminal.recover')")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM native_requests WHERE kind='terminal.recover' OR result_json!='{}'")).scalar_one():
        raise RuntimeError('Downgrade would discard recovery ownership or result evidence')
    with op.batch_alter_table('native_requests') as batch:
        batch.drop_constraint('ck_native_request_kind', type_='check')
        batch.create_check_constraint('ck_native_request_kind', 'kind IN (' + KINDS + ')')
        batch.drop_column('result_json')
