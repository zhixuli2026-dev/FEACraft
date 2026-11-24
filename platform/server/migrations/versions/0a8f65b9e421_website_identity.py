"""Persistent website OAuth transactions, external identities and app sessions."""
from alembic import op
import sqlalchemy as sa

revision = '0a8f65b9e421'
down_revision = 'f97e54a8d310'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('website_oauth_transactions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('state_sha256', sa.String(64), nullable=False, unique=True),
        sa.Column('browser_sha256', sa.String(64), nullable=False),
        sa.Column('nonce_sha256', sa.String(64), nullable=False),
        sa.Column('verifier_ciphertext', sa.Text()),
        sa.Column('config_sha256', sa.String(64), nullable=False),
        sa.Column('phase', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('intent', sa.String(10), nullable=False),
        sa.Column('link_user_id', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('error_code', sa.String(64)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("phase IN ('pending','processing','completed','failed','cancelled','expired')", name='ck_website_oauth_phase'),
        sa.CheckConstraint("(intent='login' AND link_user_id IS NULL) OR (intent='link' AND link_user_id IS NOT NULL)", name='ck_website_oauth_intent'),
        sa.CheckConstraint("(phase IN ('pending','processing') AND verifier_ciphertext IS NOT NULL AND ended_at IS NULL) OR (phase NOT IN ('pending','processing') AND verifier_ciphertext IS NULL AND ended_at IS NOT NULL)", name='ck_website_oauth_terminal'),
        sa.CheckConstraint('expires_at > created_at', name='ck_website_oauth_expiry'))
    op.create_index('ix_website_oauth_browser', 'website_oauth_transactions', ['browser_sha256'])
    op.create_table('external_identities',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('issuer', sa.String(300), nullable=False),
        sa.Column('client_id', sa.String(200), nullable=False),
        sa.Column('subject', sa.String(255), nullable=False),
        sa.Column('display_email', sa.String(255)),
        sa.Column('email_verified', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('issuer', 'client_id', 'subject', name='uq_external_identity_subject'))
    op.create_index('ix_external_identity_user', 'external_identities', ['user_id'])
    op.create_table('website_auth_sessions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('token_sha256', sa.String(64), nullable=False, unique=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('identity_id', sa.String(36), sa.ForeignKey('external_identities.id'), nullable=False),
        sa.Column('provider_sid', sa.String(255)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint('expires_at > created_at', name='ck_website_auth_expiry'))
    op.create_index('ix_website_auth_user', 'website_auth_sessions', ['user_id'])
    op.create_index('ix_website_auth_identity', 'website_auth_sessions', ['identity_id'])


def downgrade():
    connection = op.get_bind()
    for table in ('website_auth_sessions', 'external_identities', 'website_oauth_transactions'):
        if connection.execute(sa.text('SELECT count(*) FROM ' + table)).scalar():
            raise RuntimeError('Website identity history must be explicitly reconciled before downgrade')
    op.drop_table('website_auth_sessions')
    op.drop_table('external_identities')
    op.drop_table('website_oauth_transactions')
