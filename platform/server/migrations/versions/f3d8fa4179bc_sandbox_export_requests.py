"""Register sandbox exporters independently of the terminal process scope."""
from alembic import op
import sqlalchemy as sa

revision = 'f3d8fa4179bc'
down_revision = 'e2c7e93068af'
branch_labels = None
depends_on = None
KINDS = "'report.prepare','latex.prepare','synctex.query','runtime.wsl_probe','runtime.python_probe','agent.shell'"


def change(kinds):
    with op.batch_alter_table('native_requests') as batch:
        batch.drop_constraint('ck_native_request_kind', type_='check')
        batch.create_check_constraint('ck_native_request_kind', 'kind IN (' + kinds + ')')


def upgrade():
    change(KINDS + ",'terminal.export'")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM native_requests WHERE kind='terminal.export'")).scalar_one():
        raise RuntimeError('Downgrade would discard sandbox export ownership evidence')
    change(KINDS)
