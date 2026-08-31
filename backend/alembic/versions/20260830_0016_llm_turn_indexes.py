"""新增 llm_turns 成本聚合索引

Revision ID: 20260830_0016
Revises: 20260826_0015
Create Date: 2026-08-30 21:10:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260830_0016'
down_revision: Union[str, None] = '20260826_0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    index_names = {str(row.get('name') or '') for row in inspector.get_indexes(table_name)}
    return index_name in index_names


def upgrade() -> None:
    if not _table_exists('llm_turns'):
        return

    if not _index_exists('llm_turns', 'ix_llm_turns_created_at'):
        op.create_index('ix_llm_turns_created_at', 'llm_turns', ['created_at'], unique=False)

    if not _index_exists('llm_turns', 'ix_llm_turns_agent_created_at'):
        op.create_index('ix_llm_turns_agent_created_at', 'llm_turns', ['agent_id', 'created_at'], unique=False)

    if not _index_exists('llm_turns', 'ix_llm_turns_conversation_created_at'):
        op.create_index('ix_llm_turns_conversation_created_at', 'llm_turns', ['conversation_id', 'created_at'], unique=False)


def downgrade() -> None:
    if _table_exists('llm_turns') and _index_exists('llm_turns', 'ix_llm_turns_conversation_created_at'):
        op.drop_index('ix_llm_turns_conversation_created_at', table_name='llm_turns')

    if _table_exists('llm_turns') and _index_exists('llm_turns', 'ix_llm_turns_agent_created_at'):
        op.drop_index('ix_llm_turns_agent_created_at', table_name='llm_turns')

    if _table_exists('llm_turns') and _index_exists('llm_turns', 'ix_llm_turns_created_at'):
        op.drop_index('ix_llm_turns_created_at', table_name='llm_turns')
