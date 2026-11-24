"""Add organization-owned provider profiles with externally wrapped credentials."""
from alembic import op
import sqlalchemy as sa


revision = 'e87ba491d832'
down_revision = '9b14c020b4d8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('provider_profiles') as table:
        table.add_column(sa.Column('org_id', sa.Integer(), nullable=True))
        table.create_foreign_key('fk_provider_profiles_org_id', 'organizations',
                                 ['org_id'], ['id'], ondelete='CASCADE')
        table.create_index('ix_provider_profiles_org_id', ['org_id'])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT 1 FROM provider_profiles WHERE org_id IS NOT NULL LIMIT 1')):
        raise RuntimeError('Retire organization-owned provider profiles before downgrading')
    with op.batch_alter_table('provider_profiles') as table:
        table.drop_index('ix_provider_profiles_org_id')
        table.drop_constraint('fk_provider_profiles_org_id', type_='foreignkey')
        table.drop_column('org_id')
