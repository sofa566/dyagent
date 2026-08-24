"""移除舊版 roles 資料表

Revision ID: 20260803_0009
Revises: 20260803_0008
Create Date: 2026-08-03 00:30:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260803_0009'
down_revision: Union[str, None] = '20260803_0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _table_exists('roles'):
        return
    op.drop_table('roles')


def downgrade() -> None:
    if _table_exists('roles'):
        return
    op.create_table(
        'roles',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.Enum('admin', 'agent_admin', 'user', name='role_name'), nullable=False),
        sa.Column('permissions', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
