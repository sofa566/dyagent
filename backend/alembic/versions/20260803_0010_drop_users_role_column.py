"""回填系統角色綁定並移除 users.role

Revision ID: 20260803_0010
Revises: 20260803_0009
Create Date: 2026-08-03 00:50:00
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260803_0010'
down_revision: Union[str, None] = '20260803_0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SYSTEM_ROLE_CODES = ('admin', 'agent_admin', 'user')


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = inspector.get_columns(table_name)
    return any(str(column.get('name') or '') == column_name for column in columns)


def upgrade() -> None:
    if not _table_exists('users'):
        return

    has_role_column = _column_exists('users', 'role')
    if _table_exists('access_roles') and _table_exists('user_role_bindings'):
        bind = op.get_bind()
        now = datetime.utcnow()

        users_table = sa.table(
            'users',
            sa.column('id', sa.String),
            sa.column('role', sa.String),
        )
        access_roles_table = sa.table(
            'access_roles',
            sa.column('id', sa.String),
            sa.column('code', sa.String),
            sa.column('is_system', sa.Boolean),
            sa.column('enabled', sa.Boolean),
        )
        user_role_bindings_table = sa.table(
            'user_role_bindings',
            sa.column('id', sa.String),
            sa.column('user_id', sa.String),
            sa.column('role_id', sa.String),
            sa.column('created_at', sa.DateTime),
        )

        role_rows = bind.execute(
            sa.select(access_roles_table.c.id, access_roles_table.c.code).where(
                sa.and_(
                    access_roles_table.c.code.in_(list(SYSTEM_ROLE_CODES)),
                    access_roles_table.c.is_system == sa.true(),
                    access_roles_table.c.enabled == sa.true(),
                )
            )
        ).all()
        role_id_by_code = {str(row.code): str(row.id) for row in role_rows}
        default_role_id = role_id_by_code.get('user')

        if default_role_id:
            selected_columns = [users_table.c.id]
            if has_role_column:
                selected_columns.append(users_table.c.role)
            user_rows = bind.execute(sa.select(*selected_columns)).all()

            for user_row in user_rows:
                user_id = str(user_row.id)
                existing_binding = bind.execute(
                    sa.select(user_role_bindings_table.c.id).where(user_role_bindings_table.c.user_id == user_id).limit(1)
                ).first()
                if existing_binding is not None:
                    continue

                role_code = 'user'
                if has_role_column:
                    legacy_role = str(getattr(user_row, 'role', '') or '').strip()
                    if legacy_role in SYSTEM_ROLE_CODES:
                        role_code = legacy_role

                role_id = role_id_by_code.get(role_code) or default_role_id
                if not role_id:
                    continue

                bind.execute(
                    user_role_bindings_table.insert().values(
                        id=str(uuid.uuid4()),
                        user_id=user_id,
                        role_id=role_id,
                        created_at=now,
                    )
                )

    if has_role_column:
        with op.batch_alter_table('users') as batch_op:
            batch_op.drop_column('role')

    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute('DROP TYPE IF EXISTS user_role')


def downgrade() -> None:
    if not _table_exists('users'):
        return
    if _column_exists('users', 'role'):
        return

    user_role_enum = sa.Enum('admin', 'agent_admin', 'user', name='user_role')
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column('role', user_role_enum, nullable=False, server_default='user'))

    if not _table_exists('access_roles') or not _table_exists('user_role_bindings'):
        return

    bind = op.get_bind()
    users_table = sa.table(
        'users',
        sa.column('id', sa.String),
        sa.column('role', sa.String),
    )
    access_roles_table = sa.table(
        'access_roles',
        sa.column('id', sa.String),
        sa.column('code', sa.String),
        sa.column('is_system', sa.Boolean),
    )
    user_role_bindings_table = sa.table(
        'user_role_bindings',
        sa.column('user_id', sa.String),
        sa.column('role_id', sa.String),
    )

    role_rows = bind.execute(
        sa.select(access_roles_table.c.id, access_roles_table.c.code).where(
            sa.and_(
                access_roles_table.c.code.in_(list(SYSTEM_ROLE_CODES)),
                access_roles_table.c.is_system == sa.true(),
            )
        )
    ).all()
    role_code_by_id = {str(row.id): str(row.code) for row in role_rows}
    users = bind.execute(sa.select(users_table.c.id)).all()
    for user_row in users:
        user_id = str(user_row.id)
        binding_rows = bind.execute(
            sa.select(user_role_bindings_table.c.role_id).where(user_role_bindings_table.c.user_id == user_id)
        ).all()
        binding_role_codes = {role_code_by_id.get(str(binding_row.role_id), 'user') for binding_row in binding_rows}
        target_role = 'user'
        if 'admin' in binding_role_codes:
            target_role = 'admin'
        elif 'agent_admin' in binding_role_codes:
            target_role = 'agent_admin'

        bind.execute(
            users_table.update().where(users_table.c.id == user_id).values(role=target_role)
        )
