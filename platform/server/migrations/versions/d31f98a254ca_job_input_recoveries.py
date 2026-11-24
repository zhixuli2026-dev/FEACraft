"""Explicit current-input replacement requests, independent of original evidence."""
from alembic import op
import sqlalchemy as sa

revision='d31f98a254ca'
down_revision='c20e87f143b9'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('job_input_recoveries',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('case_id',sa.Integer(),nullable=False),
        sa.Column('source_job_id',sa.Integer(),nullable=False),
        sa.Column('source_attempt_id',sa.String(36),nullable=False),
        sa.Column('target_job_id',sa.Integer(),nullable=False),
        sa.Column('actor_user_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('payload_json',sa.Text(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.ForeignKeyConstraint(['source_job_id','case_id'],['jobs.id','jobs.case_id'],name='fk_input_recovery_source_case'),
        sa.ForeignKeyConstraint(['target_job_id','case_id'],['jobs.id','jobs.case_id'],name='fk_input_recovery_target_case'),
        sa.ForeignKeyConstraint(['source_attempt_id','source_job_id'],['job_attempts.id','job_attempts.job_id'],name='fk_input_recovery_attempt'),
        sa.UniqueConstraint('source_job_id',name='uq_input_recovery_source'),
        sa.UniqueConstraint('target_job_id',name='uq_input_recovery_target'),
        sa.CheckConstraint('source_job_id != target_job_id',name='ck_input_recovery_distinct'))
    op.create_index('ix_input_recovery_case','job_input_recoveries',['case_id','created_at'])


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError('Input recovery history must be checked on the target database before downgrade')
    connection = op.get_bind()
    if connection.dialect.name == 'postgresql':
        connection.execute(sa.text('LOCK TABLE job_input_recoveries IN ACCESS EXCLUSIVE MODE'))
    elif connection.dialect.name == 'sqlite':
        # Begin a write transaction before checking so a concurrent insert cannot
        # land between the history check and the table drop.
        connection.execute(sa.text('DELETE FROM job_input_recoveries WHERE 0'))
    if connection.execute(sa.text('SELECT 1 FROM job_input_recoveries LIMIT 1')).first():
        raise RuntimeError('Downgrade would discard job input recovery history; restore the pre-upgrade backup')
    op.drop_index('ix_input_recovery_case',table_name='job_input_recoveries')
    op.drop_table('job_input_recoveries')
