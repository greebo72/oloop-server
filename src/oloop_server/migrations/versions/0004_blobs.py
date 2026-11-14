"""blobs

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('blobs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('uploaded_by', sa.Uuid(), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('first_referenced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('unreferenced_since', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_blobs_project_id_projects')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_blobs'))
    )
    op.create_index('ix_blobs_unreferenced_since', 'blobs', ['unreferenced_since'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_blobs_unreferenced_since', table_name='blobs')
    op.drop_table('blobs')
