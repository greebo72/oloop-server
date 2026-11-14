"""auth: users, devices, tokens, login failures

Revision ID: 0001
Revises:
Create Date: 2026-11-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('login_failures',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('username_key', sa.String(length=100), nullable=False),
    sa.Column('address', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_login_failures'))
    )
    op.create_index('ix_login_failures_address_created_at', 'login_failures', ['address', 'created_at'], unique=False)
    op.create_index('ix_login_failures_username_key_created_at', 'login_failures', ['username_key', 'created_at'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('username', sa.String(length=100), nullable=False),
    sa.Column('username_key', sa.String(length=100), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('username_key', name=op.f('uq_users_username_key'))
    )
    op.create_table('devices',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_devices_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_devices'))
    )
    op.create_table('tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('device_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind IN ('access', 'refresh')", name=op.f('ck_tokens_kind')),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_tokens_device_id_devices')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_tokens_token_hash'))
    )


def downgrade() -> None:
    op.drop_table('tokens')
    op.drop_table('devices')
    op.drop_table('users')
    op.drop_index('ix_login_failures_username_key_created_at', table_name='login_failures')
    op.drop_index('ix_login_failures_address_created_at', table_name='login_failures')
    op.drop_table('login_failures')
