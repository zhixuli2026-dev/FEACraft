"""Register native image cleanup without discarding existing ownership."""
from alembic import op
import sqlalchemy as sa

revision='c6a1de7402ef'
down_revision='b5f0cd6391de'
branch_labels=None
depends_on=None
KINDS="'report.prepare','latex.prepare','synctex.query','runtime.wsl_probe','runtime.python_probe','agent.shell','terminal.export','terminal.recover'"


def upgrade():
    with op.batch_alter_table('native_requests') as batch:
        batch.drop_constraint('ck_native_request_kind',type_='check')
        batch.create_check_constraint('ck_native_request_kind','kind IN ('+KINDS+",'terminal.cleanup')")


def downgrade():
    db=op.get_bind()
    if (db.execute(sa.text("SELECT COUNT(*) FROM native_requests WHERE kind='terminal.cleanup'")).scalar_one()
            or db.execute(sa.text("SELECT COUNT(*) FROM durable_events WHERE type IN ('terminal.image_cleanup_started','terminal.image_cleanup_completed')")).scalar_one()):
        raise RuntimeError('Downgrade would discard image cleanup evidence')
    with op.batch_alter_table('native_requests') as batch:
        batch.drop_constraint('ck_native_request_kind',type_='check')
        batch.create_check_constraint('ck_native_request_kind','kind IN ('+KINDS+')')
