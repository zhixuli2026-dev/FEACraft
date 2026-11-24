"""Bind reconciled sandbox ownership to independent durable exit observations."""
from alembic import op
import sqlalchemy as sa

revision = 'b5f0cd6391de'
down_revision = 'a4e9bc5280cd'
branch_labels = None
depends_on = None

TABLES = (
    ('native_sessions','ck_native_session_retirement',"retirement_event_id IS NULL OR (shell='wsl-sandbox' AND phase='failed')"),
    ('native_requests','ck_native_request_retirement',"retirement_event_id IS NULL OR (kind='terminal.export' AND phase IN ('failed','cancelled'))"),
)


def upgrade():
    for table, constraint, expression in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('retirement_event_id',sa.String(36),nullable=True))
            batch.create_foreign_key('fk_'+table+'_retirement_event','durable_events',['retirement_event_id'],['id'])
            batch.create_check_constraint(constraint,expression)


def downgrade():
    bind=op.get_bind()
    for table,_,_ in TABLES:
        if bind.execute(sa.text('SELECT COUNT(*) FROM '+table+' WHERE retirement_event_id IS NOT NULL')).scalar_one():
            raise RuntimeError('Downgrade would discard sandbox retirement bindings')
    if bind.execute(sa.text("SELECT COUNT(*) FROM durable_events WHERE type='terminal.sandbox_retired'")).scalar_one():
        raise RuntimeError('Downgrade would hide retained sandbox retirement evidence')
    for table,constraint,_ in reversed(TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(constraint,type_='check')
            batch.drop_constraint('fk_'+table+'_retirement_event',type_='foreignkey')
            batch.drop_column('retirement_event_id')
