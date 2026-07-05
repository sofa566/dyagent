"""add references column to function_profiles

Revision ID: 20260327_0001
Revises: 20260324_0001
Create Date: 2026-03-27 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260327_0001"
down_revision: Union[str, None] = "20260324_0001"
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
    cols = inspector.get_columns(table_name)
    return any(c.get("name") == column_name for c in cols)


def upgrade() -> None:
    # 新增 references 欄位（JSON 格式，儲存 Claude Skill 的 references/ 內容）
    if _table_exists("function_profiles") and not _column_exists("function_profiles", "references"):
        op.add_column(
            "function_profiles",
            sa.Column("references", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    if _table_exists("function_profiles") and _column_exists("function_profiles", "references"):
        op.drop_column("function_profiles", "references")
