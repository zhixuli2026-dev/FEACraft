"""Job submission identity and immutable retry lineage.

Revision ID: 4c02b1a7d9e0
Revises: 0fd328dae4cf
"""
from alembic import op
import sqlalchemy as sa

revision = "4c02b1a7d9e0"
down_revision = "0fd328dae4cf"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("job_attempts") as batch:
        batch.add_column(sa.Column("root_job_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("retry_of_attempt_id", sa.String(36), nullable=True))
    op.execute("UPDATE job_attempts SET root_job_id=job_id")
    with op.batch_alter_table("job_attempts") as batch:
        batch.alter_column("root_job_id", existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key("fk_attempt_root_job", "jobs", ["root_job_id"], ["id"])
        batch.create_foreign_key("fk_attempt_retry_source", "job_attempts", ["retry_of_attempt_id"], ["id"])
        batch.create_unique_constraint("uq_job_attempt_lineage_number", ["root_job_id", "number"])
    op.create_table("job_submission_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("execution_spec_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("case_id", "created_by", "idempotency_key", name="uq_job_submission_idempotency"))


def downgrade():
    op.drop_table("job_submission_requests")
    with op.batch_alter_table("job_attempts") as batch:
        batch.drop_constraint("uq_job_attempt_lineage_number", type_="unique")
        batch.drop_constraint("fk_attempt_retry_source", type_="foreignkey")
        batch.drop_constraint("fk_attempt_root_job", type_="foreignkey")
        batch.drop_column("retry_of_attempt_id")
        batch.drop_column("root_job_id")
