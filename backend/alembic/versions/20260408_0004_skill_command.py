"""skills 新增 command 欄位

Revision ID: 20260408_0004
Revises: 20260404_0003
Create Date: 2026-04-08 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260408_0004"
down_revision: Union[str, None] = "20260404_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    columns = inspector.get_columns(table_name)
    return any(c.get("name") == column_name for c in columns)


def upgrade() -> None:
    if not _table_exists("skills"):
        return
    if not _column_exists("skills", "command"):
        op.add_column("skills", sa.Column("command", sa.Text(), nullable=True))


def downgrade() -> None:
    if not _table_exists("skills"):
        return
    if _column_exists("skills", "command"):
        op.drop_column("skills", "command")
