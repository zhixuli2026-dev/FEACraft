"""Index ordered history windows without changing stored events."""
from alembic import op

revision = "e42a60d73fb1"
down_revision = "d31f98a254ca"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_agent_events_history", "agent_events", ["conversation_id", "seq", "id"])


def downgrade():
    op.drop_index("ix_agent_events_history", table_name="agent_events")
