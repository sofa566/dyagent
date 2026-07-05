"""Phase 5: Functions/Skills 行業標準對齊

- function_profiles: 新增 parameters, handler_type, handler_config（OpenAI Function Calling）
- skills: 新增 skill_type, prompt_template, zip_bundle, references（Claude Skills）

Revision ID: 20260328_0001
Revises: 20260327_0001
Create Date: 2026-03-28 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260328_0001"
down_revision: Union[str, None] = "20260327_0001"
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
    # ========== function_profiles: OpenAI Function Calling 格式 ==========
    if _table_exists("function_profiles"):
        # parameters: OpenAI JSON Schema 格式
        if not _column_exists("function_profiles", "parameters"):
            op.add_column(
                "function_profiles",
                sa.Column("parameters", sa.JSON(), nullable=True)
            )

        # handler_type: internal | webhook | mcp
        if not _column_exists("function_profiles", "handler_type"):
            op.add_column(
                "function_profiles",
                sa.Column("handler_type", sa.String(20), nullable=True, server_default="internal")
            )

        # handler_config: 執行配置（endpoint URL、MCP server 等）
        if not _column_exists("function_profiles", "handler_config"):
            op.add_column(
                "function_profiles",
                sa.Column("handler_config", sa.JSON(), nullable=True)
            )

    # ========== skills: Claude Skills 格式 ==========
    if _table_exists("skills"):
        # skill_type: prompt | executable | hybrid（預設 executable 以向後相容）
        if not _column_exists("skills", "skill_type"):
            op.add_column(
                "skills",
                sa.Column("skill_type", sa.String(20), nullable=True, server_default="executable")
            )

        # prompt_template: SKILL.md 內容（提示詞模板）
        if not _column_exists("skills", "prompt_template"):
            op.add_column(
                "skills",
                sa.Column("prompt_template", sa.Text(), nullable=True)
            )

        # zip_bundle: 完整 ZIP 檔案（LargeBinary）
        if not _column_exists("skills", "zip_bundle"):
            op.add_column(
                "skills",
                sa.Column("zip_bundle", sa.LargeBinary(), nullable=True)
            )

        # references: 解壓後的 references/ 內容（JSON 快取）
        if not _column_exists("skills", "references"):
            op.add_column(
                "skills",
                sa.Column("references", sa.JSON(), nullable=True)
            )


def downgrade() -> None:
    # skills 欄位移除
    if _table_exists("skills"):
        if _column_exists("skills", "references"):
            op.drop_column("skills", "references")
        if _column_exists("skills", "zip_bundle"):
            op.drop_column("skills", "zip_bundle")
        if _column_exists("skills", "prompt_template"):
            op.drop_column("skills", "prompt_template")
        if _column_exists("skills", "skill_type"):
            op.drop_column("skills", "skill_type")

    # function_profiles 欄位移除
    if _table_exists("function_profiles"):
        if _column_exists("function_profiles", "handler_config"):
            op.drop_column("function_profiles", "handler_config")
        if _column_exists("function_profiles", "handler_type"):
            op.drop_column("function_profiles", "handler_type")
        if _column_exists("function_profiles", "parameters"):
            op.drop_column("function_profiles", "parameters")
