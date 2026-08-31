"""清理 Agent model_config 舊工具綁定鍵

Revision ID: 20260830_0019
Revises: 20260830_0018
Create Date: 2026-08-30 23:50:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260830_0019'
down_revision: Union[str, None] = '20260830_0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    agents_table = sa.table(
        'agents',
        sa.column('id'),
        sa.column('model_config', sa.JSON()),
    )
    rows = bind.execute(sa.select(agents_table.c.id, agents_table.c.model_config)).mappings().all()
    for row in rows:
        model_config = row.get('model_config')
        if not isinstance(model_config, dict):
            continue
        cleaned = dict(model_config)
        cleaned.pop('mcp_ids', None)
        cleaned.pop('skill_ids', None)
        if cleaned == model_config:
            continue
        bind.execute(
            sa.update(agents_table)
            .where(agents_table.c.id == row.get('id'))
            .values(model_config=cleaned)
        )


def downgrade() -> None:
    # 無法可靠還原已移除的歷史鍵值，保留 no-op。
    pass
