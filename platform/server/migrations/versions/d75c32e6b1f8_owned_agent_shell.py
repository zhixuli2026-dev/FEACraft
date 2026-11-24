"""Bind an owned shell request to exactly one Agent tool call.

Revision ID: d75c32e6b1f8
Revises: c64b21d5a0e7
"""
from alembic import op
import sqlalchemy as sa

revision='d75c32e6b1f8'
down_revision='c64b21d5a0e7'
branch_labels=None
depends_on=None
KINDS="'report.prepare','latex.prepare','synctex.query','runtime.wsl_probe','runtime.python_probe'"


def upgrade():
    with op.batch_alter_table('native_requests') as batch:
        batch.add_column(sa.Column('conversation_id',sa.Integer(),nullable=True))
        batch.add_column(sa.Column('tool_call_id',sa.String(40),nullable=True))
        batch.create_foreign_key('fk_native_request_conversation','conversations',['conversation_id'],['id'])
        batch.create_foreign_key('fk_native_request_tool','tool_calls',['tool_call_id'],['id'])
        batch.create_unique_constraint('uq_native_request_tool_call',['tool_call_id'])
        batch.drop_constraint('ck_native_request_kind',type_='check')
        batch.create_check_constraint('ck_native_request_kind',"kind IN ("+KINDS+",'agent.shell')")
        batch.create_check_constraint('ck_native_request_tool_scope',"(kind='agent.shell' AND conversation_id IS NOT NULL AND tool_call_id IS NOT NULL) OR (kind!='agent.shell' AND conversation_id IS NULL AND tool_call_id IS NULL)")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT count(*) FROM native_requests WHERE kind='agent.shell' OR conversation_id IS NOT NULL OR tool_call_id IS NOT NULL")).scalar_one():
        raise RuntimeError('Export and remove Agent shell history before downgrading its request binding')
    with op.batch_alter_table('native_requests') as batch:
        batch.drop_constraint('ck_native_request_tool_scope',type_='check')
        batch.drop_constraint('ck_native_request_kind',type_='check')
        batch.create_check_constraint('ck_native_request_kind',"kind IN ("+KINDS+")")
        batch.drop_constraint('uq_native_request_tool_call',type_='unique')
        batch.drop_constraint('fk_native_request_tool',type_='foreignkey')
        batch.drop_constraint('fk_native_request_conversation',type_='foreignkey')
        batch.drop_column('tool_call_id');batch.drop_column('conversation_id')
