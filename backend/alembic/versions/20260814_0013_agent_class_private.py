"""agent_class 新增 private 類型

Revision ID: 20260814_0013
Revises: 20260807_0012
Create Date: 2026-08-14 10:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260814_0013'
down_revision: Union[str, None] = '20260807_0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum_exists(enum_name: str) -> bool:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return False
    result = bind.execute(
        sa.text('SELECT 1 FROM pg_type WHERE typname = :name'),
        {'name': enum_name},
    )
    return result.fetchone() is not None


def _enum_value_exists(enum_name: str, value: str) -> bool:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return False
    result = bind.execute(
        sa.text(
            'SELECT 1 '
            'FROM pg_enum e '
            'JOIN pg_type t ON t.oid = e.enumtypid '
            'WHERE t.typname = :enum_name AND e.enumlabel = :enum_value'
        ),
        {'enum_name': enum_name, 'enum_value': value},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    if not _enum_exists('agent_class_enum'):
        return
    if _enum_value_exists('agent_class_enum', 'private'):
        return
    op.execute("ALTER TYPE agent_class_enum ADD VALUE 'private'")


def downgrade() -> None:
    # PostgreSQL 不支援直接移除 enum value，故保留 no-op。
    return
