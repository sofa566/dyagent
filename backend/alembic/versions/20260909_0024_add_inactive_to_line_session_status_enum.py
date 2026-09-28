"""為 line_session_status_enum 新增 inactive 狀態

Revision ID: 20260909_0024
Revises: 20260907_0023
Create Date: 2026-09-09 05:40:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = '20260909_0024'
down_revision: str | None = '20260907_0023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgresql() -> bool:
    bind = op.get_bind()
    return str(getattr(bind.dialect, 'name', '') or '').lower() == 'postgresql'


def upgrade() -> None:
    if not _is_postgresql():
        return
    op.execute("ALTER TYPE line_session_status_enum ADD VALUE IF NOT EXISTS 'inactive'")


def downgrade() -> None:
    if not _is_postgresql():
        return

    # PostgreSQL 不支援直接刪除 enum value，因此重建 enum 並移除 inactive。
    op.execute("UPDATE line_channel_sessions SET status = 'archived' WHERE status = 'inactive'")
    op.execute('ALTER TYPE line_session_status_enum RENAME TO line_session_status_enum_old')
    op.execute("CREATE TYPE line_session_status_enum AS ENUM ('active', 'archived')")
    op.execute(
        "ALTER TABLE line_channel_sessions "
        "ALTER COLUMN status TYPE line_session_status_enum "
        "USING status::text::line_session_status_enum"
    )
    op.execute('DROP TYPE line_session_status_enum_old')
