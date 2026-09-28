"""新增 users.enabled 欄位

Revision ID: 20260807_0012
Revises: 20260807_0011
Create Date: 2026-08-07 10:20:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '20260807_0012'
down_revision: str | None = '20260807_0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = inspector.get_columns(table_name)
    return any(str(column.get('name') or '') == column_name for column in columns)


def upgrade() -> None:
    if _column_exists('users', 'enabled'):
        return

    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    if not _column_exists('users', 'enabled'):
        return

    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('enabled')
