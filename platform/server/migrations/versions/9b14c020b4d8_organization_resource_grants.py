"""Database-backed workspace and case organization grants."""
from alembic import op
import sqlalchemy as sa


revision = '9b14c020b4d8'
down_revision = '8da5cff5b71b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('organization_resource_grants',
        sa.Column('scope',sa.String(12),primary_key=True),
        sa.Column('resource_id',sa.Integer,primary_key=True),
        sa.Column('org_id',sa.Integer,sa.ForeignKey('organizations.id',ondelete='CASCADE'),primary_key=True),
        sa.Column('workspace_id',sa.Integer,sa.ForeignKey('workspaces.id',ondelete='CASCADE'),nullable=False),
        sa.Column('case_id',sa.Integer,sa.ForeignKey('cases.id',ondelete='CASCADE'),nullable=True),
        sa.Column('role',sa.String(12),nullable=False),
        sa.Column('role_ceiling',sa.String(12),nullable=False),
        sa.Column('enabled',sa.Boolean,nullable=False),
        sa.Column('delegated',sa.Boolean,nullable=False),
        sa.Column('legacy_slot',sa.Boolean,nullable=False),
        sa.Column('version',sa.Integer,nullable=False),
        sa.Column('updated_by',sa.Integer,nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint("(scope='workspace' AND case_id IS NULL AND resource_id=workspace_id) OR "
                           "(scope='case' AND case_id IS NOT NULL AND resource_id=case_id)",name='ck_org_grant_scope'),
        sa.CheckConstraint("role IN ('viewer','member') AND role_ceiling IN ('viewer','member') "
                           "AND (role='viewer' OR role_ceiling='member')",name='ck_org_grant_role'),
        sa.CheckConstraint('version > 0 AND updated_by > 0',name='ck_org_grant_revision'))
    op.create_index('ix_org_grant_workspace','organization_resource_grants',['workspace_id','scope','enabled','org_id'])
    op.create_index('ix_org_grant_org','organization_resource_grants',['org_id','scope','enabled'])


def downgrade():
    op.drop_index('ix_org_grant_org',table_name='organization_resource_grants')
    op.drop_index('ix_org_grant_workspace',table_name='organization_resource_grants')
    op.drop_table('organization_resource_grants')
