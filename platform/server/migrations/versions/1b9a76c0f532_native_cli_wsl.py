"""Admit owned WSL CLI sessions without changing historical Windows records."""
from alembic import op
import sqlalchemy as sa

revision = '1b9a76c0f532'
down_revision = '0a8f65b9e421'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('native_cli_sessions') as batch:
        batch.drop_constraint('ck_native_cli_runtime', type_='check')
        batch.create_check_constraint('ck_native_cli_runtime', "runtime IN ('windows','wsl')")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT count(*) FROM native_cli_sessions WHERE runtime='wsl'")).scalar_one():
        raise RuntimeError('WSL CLI history must be explicitly reconciled before downgrade')
    with op.batch_alter_table('native_cli_sessions') as batch:
        batch.drop_constraint('ck_native_cli_runtime', type_='check')
        batch.create_check_constraint('ck_native_cli_runtime', "runtime='windows'")
