"""agents 新增 agent_class 與 enabled 欄位

Revision ID: 20260404_0003
Revises: 20260328_0002
Create Date: 2026-04-04 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260404_0003"
down_revision: Union[str, None] = "20260328_0002"
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


def _enum_exists(enum_name: str) -> bool:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    result = bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = :name"),
        {"name": enum_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    if not _table_exists("agents"):
        return

    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg and not _enum_exists("agent_class_enum"):
        op.execute("CREATE TYPE agent_class_enum AS ENUM ('master', 'public', 'tasked')")

    if not _column_exists("agents", "agent_class"):
        if is_pg:
            agent_class_type = sa.Enum(
                "master", "public", "tasked", name="agent_class_enum", create_type=False
            )
        else:
            agent_class_type = sa.String(length=20)
        op.add_column(
            "agents",
            sa.Column("agent_class", agent_class_type, nullable=False, server_default="tasked"),
        )
        if is_pg:
            op.execute(
                "UPDATE agents "
                "SET agent_class = CASE "
                "WHEN is_router = true THEN 'master'::agent_class_enum "
                "ELSE 'tasked'::agent_class_enum "
                "END"
            )
        else:
            op.execute(
                "UPDATE agents SET agent_class = CASE WHEN is_router = true THEN 'master' ELSE 'tasked' END"
            )
        op.alter_column("agents", "agent_class", server_default=None)

    if not _column_exists("agents", "enabled"):
        op.add_column(
            "agents",
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        )
        op.execute("UPDATE agents SET enabled = true WHERE enabled IS NULL")
        op.alter_column("agents", "enabled", server_default=None)


def downgrade() -> None:
    if not _table_exists("agents"):
        return

    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if _column_exists("agents", "enabled"):
        op.drop_column("agents", "enabled")

    if _column_exists("agents", "agent_class"):
        op.drop_column("agents", "agent_class")

    if is_pg and _enum_exists("agent_class_enum"):
        op.execute("DROP TYPE agent_class_enum")
