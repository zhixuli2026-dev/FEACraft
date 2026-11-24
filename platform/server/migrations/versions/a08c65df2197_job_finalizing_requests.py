"""Admit separately journaled original-Job professional recovery validators."""
from alembic import op
import sqlalchemy as sa

revision='a08c65df2197'
down_revision='f9d4a10735bc'
branch_labels=None
depends_on=None
KINDS="'report.prepare','latex.prepare','synctex.query','runtime.wsl_probe','runtime.python_probe','agent.shell','terminal.export','terminal.recover','terminal.cleanup'"


def upgrade():
    with op.batch_alter_table('native_requests') as batch:
        batch.drop_constraint('ck_native_request_kind',type_='check')
        batch.create_check_constraint('ck_native_request_kind','kind IN ('+KINDS+",'job.finalize')")


def downgrade():
    db=op.get_bind()
    if (db.execute(sa.text("SELECT COUNT(*) FROM native_requests WHERE kind='job.finalize'")).scalar_one()
            or db.execute(sa.text("SELECT COUNT(*) FROM durable_events WHERE type='attempt.finalizing_recovery_drained'")).scalar_one()):
        raise RuntimeError('Downgrade would discard original-Job recovery validation/drain evidence')
    with op.batch_alter_table('native_requests') as batch:
        batch.drop_constraint('ck_native_request_kind',type_='check')
        batch.create_check_constraint('ck_native_request_kind','kind IN ('+KINDS+')')
