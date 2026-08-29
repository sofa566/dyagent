"""新增群組直掛權限綁定表

Revision ID: 20260807_0011
Revises: 20260803_0010
Create Date: 2026-08-07 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '20260807_0011'
down_revision: Union[str, None] = '20260803_0010'
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
    if _table_exists('group_permission_bindings'):
        return

    id_type = _id_type()
    op.create_table(
        'group_permission_bindings',
        sa.Column('id', id_type, nullable=False),
        sa.Column('group_id', id_type, nullable=False),
        sa.Column('permission_id', id_type, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['group_id'], ['access_groups.id']),
        sa.ForeignKeyConstraint(['permission_id'], ['access_permissions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'permission_id', name='uq_group_permission_binding'),
    )


def downgrade() -> None:
    if _table_exists('group_permission_bindings'):
        op.drop_table('group_permission_bindings')
