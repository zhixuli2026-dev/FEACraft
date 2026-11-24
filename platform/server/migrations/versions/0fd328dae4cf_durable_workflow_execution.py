"""durable_workflow_execution

Revision ID: 0fd328dae4cf
Revises: 6fb6c18fb7ee
Create Date: 2026-11-02 06:31:38.374448
"""
from alembic import op
import sqlalchemy as sa


revision = '0fd328dae4cf'
down_revision = '6fb6c18fb7ee'
branch_labels = None
depends_on = None


def upgrade():
    # Additive tables only. Legacy Job/case/user columns and data are unchanged.
    op.create_table('case_event_counters',
    sa.Column('case_id', sa.Integer(), nullable=False),
    sa.Column('next_seq', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint('next_seq > 0', name='ck_case_event_counter_positive'),
    sa.ForeignKeyConstraint(['case_id'], ['cases.id'], ),
    sa.PrimaryKeyConstraint('case_id')
    )
    op.create_table('workflow_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('case_id', sa.Integer(), nullable=False),
    sa.Column('org_id', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('target', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=30), server_default='queued', nullable=False),
    sa.Column('version', sa.Integer(), server_default='0', nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('expected_revision', sa.Integer(), nullable=False),
    sa.Column('run_dir', sa.String(length=500), nullable=False),
    sa.Column('plan_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('input_manifest_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('budget_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('cancel_requested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_code', sa.String(length=64), server_default='', nullable=False),
    sa.Column('detail', sa.Text(), server_default='', nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("status IN ('queued','running','awaiting_confirmation','cancelling','succeeded','failed','cancelled','interrupted')", name='ck_workflow_run_status'),
    sa.CheckConstraint("target IN ('geometry','mesh','solve','postprocess','report')", name='ck_workflow_run_target'),
    sa.CheckConstraint('version >= 0 AND expected_revision >= 0', name='ck_workflow_run_versions'),
    sa.ForeignKeyConstraint(['case_id'], ['cases.id'], ),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('case_id', 'created_by', 'idempotency_key', name='uq_workflow_run_idempotency')
    )
    op.create_index('ix_workflow_runs_case_created', 'workflow_runs', ['case_id', 'created_at'], unique=False)
    op.create_index('ix_workflow_runs_status', 'workflow_runs', ['status'], unique=False)
    op.create_table('workflow_steps',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('step_key', sa.String(length=80), nullable=False),
    sa.Column('handler', sa.String(length=80), nullable=False),
    sa.Column('handler_version', sa.String(length=30), server_default='1', nullable=False),
    sa.Column('status', sa.String(length=30), server_default='pending', nullable=False),
    sa.Column('version', sa.Integer(), server_default='0', nullable=False),
    sa.Column('dependencies_json', sa.Text(), server_default='[]', nullable=False),
    sa.Column('confirmation_state', sa.String(length=1), nullable=True),
    sa.Column('confirmation_revision', sa.Integer(), nullable=True),
    sa.Column('input_manifest_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('output_manifest_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('active_attempt_id', sa.String(length=36), nullable=True),
    sa.Column('error_code', sa.String(length=64), server_default='', nullable=False),
    sa.Column('detail', sa.Text(), server_default='', nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("confirmation_state IS NULL OR confirmation_state IN ('B','C','D','E')", name='ck_step_confirmation_state'),
    sa.CheckConstraint("status IN ('pending','ready','running','awaiting_confirmation','finalizing','cancelling','succeeded','failed','cancelled','interrupted','blocked')", name='ck_workflow_step_status'),
    sa.CheckConstraint('confirmation_revision IS NULL OR confirmation_revision >= 0', name='ck_step_confirmation_revision'),
    sa.CheckConstraint('version >= 0', name='ck_workflow_step_version'),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'step_key', name='uq_workflow_step_key')
    )
    op.create_index(op.f('ix_workflow_steps_run_id'), 'workflow_steps', ['run_id'], unique=False)
    op.create_table('job_attempts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('step_id', sa.String(length=36), nullable=True),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('phase', sa.String(length=30), server_default='queued', nullable=False),
    sa.Column('version', sa.Integer(), server_default='0', nullable=False),
    sa.Column('worker_id', sa.String(length=128), nullable=True),
    sa.Column('claim_token', sa.String(length=64), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('process_identity_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('cancel_requested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('reason_code', sa.String(length=64), server_default='', nullable=False),
    sa.Column('detail', sa.Text(), server_default='', nullable=False),
    sa.Column('input_manifest_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('output_manifest_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('publication_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("phase IN ('queued','claimed','starting','running','finalizing','cancelling','succeeded','failed','cancelled','interrupted')", name='ck_job_attempt_phase'),
    sa.CheckConstraint('number > 0 AND version >= 0', name='ck_job_attempt_versions'),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.ForeignKeyConstraint(['step_id'], ['workflow_steps.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id', 'number', name='uq_job_attempt_number')
    )
    op.create_index('ix_job_attempt_worker_lease', 'job_attempts', ['worker_id', 'lease_expires_at'], unique=False)
    op.create_index(op.f('ix_job_attempts_step_id'), 'job_attempts', ['step_id'], unique=False)
    op.create_table('artifact_publications',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('case_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=80), nullable=False),
    sa.Column('generation', sa.Integer(), nullable=False),
    sa.Column('attempt_id', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='prepared', nullable=False),
    sa.Column('immutable_path', sa.String(length=800), nullable=False),
    sa.Column('manifest_sha256', sa.String(length=64), nullable=False),
    sa.Column('committed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("status IN ('prepared','committed','superseded','failed')", name='ck_publication_status'),
    sa.CheckConstraint('generation > 0', name='ck_publication_generation'),
    sa.ForeignKeyConstraint(['attempt_id'], ['job_attempts.id'], ),
    sa.ForeignKeyConstraint(['case_id'], ['cases.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('case_id', 'kind', 'generation', name='uq_artifact_publication_generation')
    )
    op.create_index(op.f('ix_artifact_publications_attempt_id'), 'artifact_publications', ['attempt_id'], unique=False)
    op.create_table('durable_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('case_id', sa.Integer(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=True),
    sa.Column('step_id', sa.String(length=36), nullable=True),
    sa.Column('attempt_id', sa.String(length=36), nullable=True),
    sa.Column('type', sa.String(length=80), nullable=False),
    sa.Column('payload_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint('seq > 0', name='ck_durable_event_seq'),
    sa.ForeignKeyConstraint(['attempt_id'], ['job_attempts.id'], ),
    sa.ForeignKeyConstraint(['case_id'], ['cases.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.id'], ),
    sa.ForeignKeyConstraint(['step_id'], ['workflow_steps.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('case_id', 'seq', name='uq_durable_event_case_seq')
    )
    op.create_index('ix_durable_events_run', 'durable_events', ['run_id'], unique=False)
    op.create_table('job_execution_heads',
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('next_number', sa.Integer(), server_default='1', nullable=False),
    sa.Column('active_attempt_id', sa.String(length=36), nullable=True),
    sa.Column('version', sa.Integer(), server_default='0', nullable=False),
    sa.CheckConstraint('next_number > 0 AND version >= 0', name='ck_job_execution_head_versions'),
    sa.ForeignKeyConstraint(['active_attempt_id'], ['job_attempts.id'], ),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('job_id')
    )
    op.create_table('artifact_publication_heads',
    sa.Column('case_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=80), nullable=False),
    sa.Column('next_generation', sa.Integer(), server_default='1', nullable=False),
    sa.Column('active_publication_id', sa.String(length=36), nullable=True),
    sa.Column('version', sa.Integer(), server_default='0', nullable=False),
    sa.CheckConstraint('next_generation > 0 AND version >= 0', name='ck_publication_head_versions'),
    sa.ForeignKeyConstraint(['active_publication_id'], ['artifact_publications.id'], ),
    sa.ForeignKeyConstraint(['case_id'], ['cases.id'], ),
    sa.PrimaryKeyConstraint('case_id', 'kind')
    )
    op.create_table('event_outbox',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('event_id', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('claim_token', sa.String(length=64), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('last_error', sa.Text(), server_default='', nullable=False),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('pending','delivering','delivered')", name='ck_outbox_status'),
    sa.CheckConstraint('attempts >= 0', name='ck_outbox_attempts'),
    sa.ForeignKeyConstraint(['event_id'], ['durable_events.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('event_id')
    )
    op.create_index('ix_event_outbox_delivery', 'event_outbox', ['status', 'next_attempt_at'], unique=False)


def downgrade():
    # Temporary-database rollback only after exporting any new workflow history.
    op.drop_index('ix_event_outbox_delivery', table_name='event_outbox')
    op.drop_table('event_outbox')
    op.drop_table('artifact_publication_heads')
    op.drop_table('job_execution_heads')
    op.drop_index('ix_durable_events_run', table_name='durable_events')
    op.drop_table('durable_events')
    op.drop_index(op.f('ix_artifact_publications_attempt_id'), table_name='artifact_publications')
    op.drop_table('artifact_publications')
    op.drop_index(op.f('ix_job_attempts_step_id'), table_name='job_attempts')
    op.drop_index('ix_job_attempt_worker_lease', table_name='job_attempts')
    op.drop_table('job_attempts')
    op.drop_index(op.f('ix_workflow_steps_run_id'), table_name='workflow_steps')
    op.drop_table('workflow_steps')
    op.drop_index('ix_workflow_runs_status', table_name='workflow_runs')
    op.drop_index('ix_workflow_runs_case_created', table_name='workflow_runs')
    op.drop_table('workflow_runs')
    op.drop_table('case_event_counters')
