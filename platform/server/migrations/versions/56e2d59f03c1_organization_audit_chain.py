"""Store organization audit writes in the shared application database."""

from alembic import op
import sqlalchemy as sa


revision = '56e2d59f03c1'
down_revision = '2c89ba41f70e'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('organization_audit_heads',
        sa.Column('org_id', sa.Integer, sa.ForeignKey('organizations.id'), primary_key=True),
        sa.Column('sequence', sa.BigInteger, nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('legacy_valid', sa.Boolean, nullable=False),
        sa.CheckConstraint('sequence >= 0', name='ck_org_audit_head_sequence'))
    op.create_table('organization_audit_records',
        sa.Column('org_id', sa.Integer, sa.ForeignKey('organization_audit_heads.org_id'), primary_key=True),
        sa.Column('sequence', sa.BigInteger, primary_key=True),
        sa.Column('actor_user_id', sa.BigInteger, nullable=False),
        sa.Column('action', sa.String(81), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payload', sa.Text, nullable=False),
        sa.CheckConstraint('sequence > 0', name='ck_org_audit_record_sequence'))
    op.create_index('ix_org_audit_record_action', 'organization_audit_records', ['org_id', 'action', 'sequence'])
    op.create_index('ix_org_audit_record_actor', 'organization_audit_records', ['org_id', 'actor_user_id', 'sequence'])


def downgrade():
    op.drop_index('ix_org_audit_record_actor', table_name='organization_audit_records')
    op.drop_index('ix_org_audit_record_action', table_name='organization_audit_records')
    op.drop_table('organization_audit_records')
    op.drop_table('organization_audit_heads')
