"""spec003 schema baseline

Revision ID: 20260324_0001
Revises:
Create Date: 2026-03-24 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "20260324_0001"
down_revision: Union[str, None] = None
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


def _fk_exists(table_name: str, fk_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    fks = inspector.get_foreign_keys(table_name)
    return any((fk.get("name") or "") == fk_name for fk in fks)


def _column_type_name(table_name: str, column_name: str) -> str:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return ""
    cols = inspector.get_columns(table_name)
    for col in cols:
        if col.get("name") == column_name:
            return str(col.get("type") or "").upper()
    return ""


def _is_uuid_column(table_name: str, column_name: str) -> bool:
    type_name = _column_type_name(table_name, column_name)
    return "UUID" in type_name


def upgrade() -> None:
    # T001: agents 新欄位
    if _table_exists("agents") and not _column_exists("agents", "system_prompt"):
        op.add_column("agents", sa.Column("system_prompt", sa.Text(), nullable=True))

    if _table_exists("agents") and not _column_exists("agents", "is_router"):
        op.add_column(
            "agents",
            sa.Column("is_router", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
        op.alter_column("agents", "is_router", server_default=None)

    if _table_exists("agents") and not _column_exists("agents", "function_profile_id"):
        op.add_column("agents", sa.Column("function_profile_id", postgresql.UUID(as_uuid=False), nullable=True))

    # T002: function_profiles
    if not _table_exists("function_profiles"):
        op.create_table(
            "function_profiles",
            sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("provider", sa.String(length=50), nullable=True),
            sa.Column("template", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", name="uq_function_profiles_name"),
        )

    # 型別相容修復：agents.function_profile_id 若為字串，轉為 uuid 以便建立 FK
    if _table_exists("agents") and _table_exists("function_profiles"):
        if (not _is_uuid_column("agents", "function_profile_id")) and _is_uuid_column("function_profiles", "id"):
            op.execute(
                """
                ALTER TABLE agents
                ALTER COLUMN function_profile_id TYPE UUID
                USING (
                    CASE
                        WHEN function_profile_id IS NULL THEN NULL
                        WHEN BTRIM(function_profile_id::text) ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                            THEN BTRIM(function_profile_id::text)::uuid
                        ELSE NULL
                    END
                )
                """
            )

    # 建立 agents -> function_profiles FK
    if _table_exists("agents") and _table_exists("function_profiles") and not _fk_exists(
        "agents", "fk_agents_function_profile_id"
    ):
        if _is_uuid_column("agents", "function_profile_id") and _is_uuid_column("function_profiles", "id"):
            op.create_foreign_key(
                "fk_agents_function_profile_id",
                "agents",
                "function_profiles",
                ["function_profile_id"],
                ["id"],
            )

    # T003: rag_datasets
    if not _table_exists("rag_datasets"):
        op.create_table(
            "rag_datasets",
            sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("scope", sa.String(length=20), nullable=False),
            sa.Column("agent_id", postgresql.UUID(as_uuid=False), nullable=True),
            sa.Column("owner_user_id", postgresql.UUID(as_uuid=False), nullable=True),
            sa.Column("sensitivity", sa.String(length=20), nullable=False, server_default=sa.text("'normal'")),
            sa.Column("vector_backend", sa.String(length=50), nullable=True),
            sa.Column("index_name", sa.String(length=120), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], name="fk_rag_datasets_agent_id"),
            sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_rag_datasets_owner_user_id"),
        )

    # T004: llm_turns
    if not _table_exists("llm_turns"):
        op.create_table(
            "llm_turns",
            sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
            sa.Column("conversation_id", postgresql.UUID(as_uuid=False), nullable=False),
            sa.Column("agent_id", postgresql.UUID(as_uuid=False), nullable=False),
            sa.Column("message_user_id", postgresql.UUID(as_uuid=False), nullable=True),
            sa.Column("message_assistant_id", postgresql.UUID(as_uuid=False), nullable=True),
            sa.Column("provider", sa.String(length=50), nullable=True),
            sa.Column("model", sa.String(length=120), nullable=True),
            sa.Column("tier", sa.String(length=20), nullable=True),
            sa.Column("system_prompt_snapshot", sa.Text(), nullable=True),
            sa.Column("context_snapshot", sa.JSON(), nullable=True),
            sa.Column("usage", sa.JSON(), nullable=True),
            sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], name="fk_llm_turns_conversation_id"),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], name="fk_llm_turns_agent_id"),
            sa.ForeignKeyConstraint(["message_user_id"], ["messages.id"], name="fk_llm_turns_message_user_id"),
            sa.ForeignKeyConstraint(["message_assistant_id"], ["messages.id"], name="fk_llm_turns_message_assistant_id"),
        )


def downgrade() -> None:
    if _table_exists("llm_turns"):
        op.drop_table("llm_turns")

    if _table_exists("rag_datasets"):
        op.drop_table("rag_datasets")

    if _table_exists("agents") and _fk_exists("agents", "fk_agents_function_profile_id"):
        op.drop_constraint("fk_agents_function_profile_id", "agents", type_="foreignkey")

    if _table_exists("function_profiles"):
        op.drop_table("function_profiles")

    if _table_exists("agents") and _column_exists("agents", "function_profile_id"):
        op.drop_column("agents", "function_profile_id")
    if _table_exists("agents") and _column_exists("agents", "is_router"):
        op.drop_column("agents", "is_router")
    if _table_exists("agents") and _column_exists("agents", "system_prompt"):
        op.drop_column("agents", "system_prompt")
