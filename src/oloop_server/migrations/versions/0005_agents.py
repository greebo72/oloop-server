"""agents

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('agents',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('owner_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('name_key', sa.String(length=100), nullable=False),
    sa.Column('public_key', sa.LargeBinary(), nullable=False),
    sa.Column('registration', sa.LargeBinary(), nullable=False),
    sa.Column('lifetime_seconds', sa.Integer(), nullable=False),
    sa.Column('renew_until', sa.DateTime(timezone=True), nullable=False),
    sa.Column('renew_period_seconds', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_agents_owner_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agents'))
    )
    op.create_index('ix_agents_owner_id', 'agents', ['owner_id'], unique=False)
    op.create_table('agent_keys',
    sa.Column('agent_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('public_key', sa.LargeBinary(), nullable=False),
    sa.Column('statement', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_agent_keys_agent_id_agents')),
    sa.PrimaryKeyConstraint('agent_id', 'number', name=op.f('pk_agent_keys'))
    )
    op.create_table('agent_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('agent_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('replaced_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name=op.f('fk_agent_tokens_agent_id_agents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_agent_tokens_token_hash'))
    )
    op.create_index('ix_agent_tokens_agent_id', 'agent_tokens', ['agent_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_agent_tokens_agent_id', table_name='agent_tokens')
    op.drop_table('agent_tokens')
    op.drop_table('agent_keys')
    op.drop_index('ix_agents_owner_id', table_name='agents')
    op.drop_table('agents')
