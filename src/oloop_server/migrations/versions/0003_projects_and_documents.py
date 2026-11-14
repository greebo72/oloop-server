"""projects and documents

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-14
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('counters',
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('value', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('name', name=op.f('pk_counters'))
    )
    # The number of the changes starts at 0 (see models.Counter).
    op.execute("INSERT INTO counters (name, value) VALUES ('changes', 0)")
    op.create_table('projects',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('owner_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_projects_owner_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_projects'))
    )
    op.create_table('documents',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('rev', sa.Integer(), nullable=False),
    sa.Column('seq', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind IN ('project', 'task')", name=op.f('ck_documents_kind')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_documents_project_id_projects')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_documents')),
    sa.UniqueConstraint('seq', name=op.f('uq_documents_seq'))
    )
    op.create_index('ix_documents_project_id', 'documents', ['project_id'], unique=False)
    op.create_table('project_members',
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('principal_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('role', sa.String(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind IN ('user', 'agent')", name=op.f('ck_project_members_kind')),
    sa.CheckConstraint("role IN ('owner', 'write', 'read')", name=op.f('ck_project_members_role')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_project_members_project_id_projects')),
    sa.PrimaryKeyConstraint('project_id', 'principal_id', name=op.f('pk_project_members'))
    )
    op.create_index('ix_project_members_principal_id', 'project_members', ['principal_id'], unique=False)
    op.create_table('document_revisions',
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('rev', sa.Integer(), nullable=False),
    sa.Column('key_version', sa.Integer(), nullable=False),
    sa.Column('blob_ref', sa.Uuid(), nullable=True),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('written_by', sa.Uuid(), nullable=False),
    sa.Column('written_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_revisions_document_id_documents')),
    sa.PrimaryKeyConstraint('document_id', 'rev', name=op.f('pk_document_revisions'))
    )
    op.create_table('project_keys',
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('principal_id', sa.Uuid(), nullable=False),
    sa.Column('key_version', sa.Integer(), nullable=False),
    sa.Column('granted_by', sa.Uuid(), nullable=False),
    sa.Column('wrapped', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id', 'principal_id'], ['project_members.project_id', 'project_members.principal_id'], name=op.f('fk_project_keys_project_id_project_members')),
    sa.PrimaryKeyConstraint('project_id', 'principal_id', 'key_version', name=op.f('pk_project_keys'))
    )


def downgrade() -> None:
    op.drop_table('project_keys')
    op.drop_table('document_revisions')
    op.drop_index('ix_project_members_principal_id', table_name='project_members')
    op.drop_table('project_members')
    op.drop_index('ix_documents_project_id', table_name='documents')
    op.drop_table('documents')
    op.drop_table('projects')
    op.drop_table('counters')
