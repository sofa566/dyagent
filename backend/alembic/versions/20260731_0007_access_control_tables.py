"""新增動態權限管理資料表

Revision ID: 20260731_0007
Revises: 20260701_0006
Create Date: 2026-07-31 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '20260731_0007'
down_revision: Union[str, None] = '20260701_0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _id_type() -> sa.types.TypeEngine:
    bind = op.get_bind()
    if str(bind.dialect.name or '').lower() == 'postgresql':
        return postgresql.UUID(as_uuid=False)
    return sa.String(length=36)


def upgrade() -> None:
    id_type = _id_type()

    if not _table_exists('access_permissions'):
        op.create_table(
            'access_permissions',
            sa.Column('id', id_type, nullable=False),
            sa.Column('key', sa.String(length=100), nullable=False),
            sa.Column('description', sa.String(length=255), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('key'),
        )

    if not _table_exists('access_roles'):
        op.create_table(
            'access_roles',
            sa.Column('id', id_type, nullable=False),
            sa.Column('code', sa.String(length=60), nullable=False),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('code'),
        )

    if not _table_exists('access_role_permissions'):
        op.create_table(
            'access_role_permissions',
            sa.Column('id', id_type, nullable=False),
            sa.Column('role_id', id_type, nullable=False),
            sa.Column('permission_id', id_type, nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['permission_id'], ['access_permissions.id']),
            sa.ForeignKeyConstraint(['role_id'], ['access_roles.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('role_id', 'permission_id', name='uq_access_role_permission'),
        )

    if not _table_exists('access_groups'):
        op.create_table(
            'access_groups',
            sa.Column('id', id_type, nullable=False),
            sa.Column('code', sa.String(length=60), nullable=False),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('code'),
        )

    if not _table_exists('user_role_bindings'):
        op.create_table(
            'user_role_bindings',
            sa.Column('id', id_type, nullable=False),
            sa.Column('user_id', id_type, nullable=False),
            sa.Column('role_id', id_type, nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['role_id'], ['access_roles.id']),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('user_id', 'role_id', name='uq_user_role_binding'),
        )

    if not _table_exists('group_role_bindings'):
        op.create_table(
            'group_role_bindings',
            sa.Column('id', id_type, nullable=False),
            sa.Column('group_id', id_type, nullable=False),
            sa.Column('role_id', id_type, nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['group_id'], ['access_groups.id']),
            sa.ForeignKeyConstraint(['role_id'], ['access_roles.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('group_id', 'role_id', name='uq_group_role_binding'),
        )

    if not _table_exists('user_group_bindings'):
        op.create_table(
            'user_group_bindings',
            sa.Column('id', id_type, nullable=False),
            sa.Column('user_id', id_type, nullable=False),
            sa.Column('group_id', id_type, nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['group_id'], ['access_groups.id']),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('user_id', 'group_id', name='uq_user_group_binding'),
        )

    if not _table_exists('access_audit_logs'):
        op.create_table(
            'access_audit_logs',
            sa.Column('id', id_type, nullable=False),
            sa.Column('actor_user_id', id_type, nullable=True),
            sa.Column('action', sa.String(length=80), nullable=False),
            sa.Column('target_type', sa.String(length=50), nullable=False),
            sa.Column('target_id', sa.String(length=64), nullable=True),
            sa.Column('details', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['actor_user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )


def downgrade() -> None:
    if _table_exists('access_audit_logs'):
        op.drop_table('access_audit_logs')
    if _table_exists('user_group_bindings'):
        op.drop_table('user_group_bindings')
    if _table_exists('group_role_bindings'):
        op.drop_table('group_role_bindings')
    if _table_exists('user_role_bindings'):
        op.drop_table('user_role_bindings')
    if _table_exists('access_groups'):
        op.drop_table('access_groups')
    if _table_exists('access_role_permissions'):
        op.drop_table('access_role_permissions')
    if _table_exists('access_roles'):
        op.drop_table('access_roles')
    if _table_exists('access_permissions'):
        op.drop_table('access_permissions')
