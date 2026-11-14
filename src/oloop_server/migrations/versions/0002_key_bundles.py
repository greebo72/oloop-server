"""key bundles

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('key_bundles',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('rev', sa.Integer(), nullable=False),
    sa.Column('salt', sa.LargeBinary(), nullable=False),
    sa.Column('m', sa.BigInteger(), nullable=False),
    sa.Column('t', sa.BigInteger(), nullable=False),
    sa.Column('public_key', sa.LargeBinary(), nullable=False),
    sa.Column('wrapped_secret_key', sa.LargeBinary(), nullable=False),
    sa.Column('recovery_wrapped_secret_key', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_key_bundles_user_id_users')),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_key_bundles'))
    )


def downgrade() -> None:
    op.drop_table('key_bundles')
