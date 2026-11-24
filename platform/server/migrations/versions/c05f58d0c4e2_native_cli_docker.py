"""Admit Docker-owned Codex sessions alongside the other native runtimes."""
from alembic import op
import sqlalchemy as sa

revision = 'c05f58d0c4e2'
down_revision = 'f8c3d9486581'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('native_cli_sessions') as batch:
        batch.drop_constraint('ck_native_cli_runtime', type_='check')
        batch.create_check_constraint('ck_native_cli_runtime', "runtime IN ('windows','wsl','docker')")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT count(*) FROM native_cli_sessions WHERE runtime='docker'")).scalar_one():
        raise RuntimeError('Docker CLI history requires its runtime constraint')
    with op.batch_alter_table('native_cli_sessions') as batch:
        batch.drop_constraint('ck_native_cli_runtime', type_='check')
        batch.create_check_constraint('ck_native_cli_runtime', "runtime IN ('windows','wsl')")
