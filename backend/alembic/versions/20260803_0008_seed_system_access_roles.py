"""初始化系統保留角色與權限綁定

Revision ID: 20260803_0008
Revises: 20260731_0007
Create Date: 2026-08-03 00:00:00
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260803_0008'
down_revision: Union[str, None] = '20260731_0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LEGACY_ROLE_PERMISSIONS = {
    'admin': {
        'create_agent',
        'read_agent',
        'update_agent',
        'delete_agent',
        'create_user',
        'read_user',
        'update_user',
        'delete_user',
        'chat',
        'read_logs',
    },
    'agent_admin': {
        'read_agent',
        'update_agent',
        'chat',
    },
    'user': {
        'chat',
    },
}


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _table_exists('access_roles'):
        return
    if not _table_exists('access_permissions'):
        return
    if not _table_exists('access_role_permissions'):
        return

    bind = op.get_bind()
    now = datetime.utcnow()

    role_table = sa.table(
        'access_roles',
        sa.column('id', sa.String),
        sa.column('code', sa.String),
        sa.column('name', sa.String),
        sa.column('enabled', sa.Boolean),
        sa.column('is_system', sa.Boolean),
        sa.column('created_at', sa.DateTime),
        sa.column('updated_at', sa.DateTime),
    )
    permission_table = sa.table(
        'access_permissions',
        sa.column('id', sa.String),
        sa.column('key', sa.String),
        sa.column('description', sa.String),
        sa.column('created_at', sa.DateTime),
        sa.column('updated_at', sa.DateTime),
    )
    role_permission_table = sa.table(
        'access_role_permissions',
        sa.column('id', sa.String),
        sa.column('role_id', sa.String),
        sa.column('permission_id', sa.String),
        sa.column('created_at', sa.DateTime),
    )

    role_defaults = {
        'user': '一般使用者',
        'agent_admin': '代理者管理者',
        'admin': '系統管理者',
    }
    permission_keys = set().union(*LEGACY_ROLE_PERMISSIONS.values())

    for permission_key in sorted(permission_keys):
        existing_permission = bind.execute(
            sa.select(permission_table.c.id).where(permission_table.c.key == permission_key)
        ).first()
        if existing_permission is None:
            bind.execute(
                permission_table.insert().values(
                    id=str(uuid.uuid4()),
                    key=permission_key,
                    description=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    role_id_by_code: dict[str, str] = {}
    for role_code, role_name in role_defaults.items():
        existing_role = bind.execute(
            sa.select(role_table.c.id).where(role_table.c.code == role_code)
        ).first()
        if existing_role is None:
            role_id = str(uuid.uuid4())
            bind.execute(
                role_table.insert().values(
                    id=role_id,
                    code=role_code,
                    name=role_name,
                    enabled=True,
                    is_system=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            role_id_by_code[role_code] = role_id
        else:
            role_id_by_code[role_code] = str(existing_role.id)
            bind.execute(
                role_table.update()
                .where(role_table.c.id == existing_role.id)
                .values(is_system=True, enabled=True, updated_at=now)
            )

    permission_id_by_key = {
        str(row.key): str(row.id)
        for row in bind.execute(sa.select(permission_table.c.id, permission_table.c.key)).all()
    }

    desired_permissions_by_role = {
        'user': set(LEGACY_ROLE_PERMISSIONS['user']),
        'agent_admin': set(LEGACY_ROLE_PERMISSIONS['agent_admin']),
        'admin': set(permission_id_by_key.keys()),
    }

    for role_code, desired_permission_keys in desired_permissions_by_role.items():
        role_id = role_id_by_code.get(role_code)
        if not role_id:
            continue

        current_permission_ids = {
            str(row.permission_id)
            for row in bind.execute(
                sa.select(role_permission_table.c.permission_id).where(role_permission_table.c.role_id == role_id)
            ).all()
        }
        desired_permission_ids = {
            permission_id_by_key[key]
            for key in desired_permission_keys
            if key in permission_id_by_key
        }

        for permission_id in desired_permission_ids - current_permission_ids:
            bind.execute(
                role_permission_table.insert().values(
                    id=str(uuid.uuid4()),
                    role_id=role_id,
                    permission_id=permission_id,
                    created_at=now,
                )
            )

        removable_permission_ids = current_permission_ids - desired_permission_ids
        if removable_permission_ids:
            bind.execute(
                role_permission_table.delete().where(
                    sa.and_(
                        role_permission_table.c.role_id == role_id,
                        role_permission_table.c.permission_id.in_(list(removable_permission_ids)),
                    )
                )
            )


def downgrade() -> None:
    # 系統角色屬基線資料，降版不主動刪除以避免破壞現有授權配置。
    return
