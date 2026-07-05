"""為 conversation 關聯補上 ON DELETE CASCADE

Revision ID: 20260701_0006
Revises: 20260517_0005
Create Date: 2026-07-01 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260701_0006'
down_revision: Union[str, None] = '20260517_0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CONVERSATION_FOREIGN_KEYS = (
    ('messages', 'conversation_id', 'fk_messages_conversation_id'),
    ('event_parts', 'conversation_id', 'fk_event_parts_conversation_id'),
    ('llm_turns', 'conversation_id', 'fk_llm_turns_conversation_id'),
    ('multi_agent_sessions', 'conversation_id', 'fk_multi_agent_sessions_conversation_id'),
    ('skill_interactions', 'conversation_id', 'fk_skill_interactions_conversation_id'),
    ('chat_attachments', 'conversation_id', 'fk_chat_attachments_conversation_id'),
)


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
    return any(col.get('name') == column_name for col in columns)


def _find_conversation_fk(table_name: str, column_name: str) -> dict | None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return None
    for fk in inspector.get_foreign_keys(table_name):
        referred_table = str(fk.get('referred_table') or '')
        constrained_columns = list(fk.get('constrained_columns') or [])
        if referred_table == 'conversations' and constrained_columns == [column_name]:
            return fk
    return None


def _is_fk_ondelete_cascade(fk: dict | None) -> bool:
    if not isinstance(fk, dict):
        return False
    options = fk.get('options') if isinstance(fk.get('options'), dict) else {}
    ondelete = str(options.get('ondelete') or fk.get('ondelete') or '').strip().upper()
    return ondelete == 'CASCADE'


def _drop_fk_if_exists(table_name: str, fk_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return
    fk_names = {(fk.get('name') or '') for fk in inspector.get_foreign_keys(table_name)}
    if fk_name in fk_names:
        op.drop_constraint(fk_name, table_name, type_='foreignkey')


def upgrade() -> None:
    for table_name, column_name, target_fk_name in CONVERSATION_FOREIGN_KEYS:
        if not _table_exists(table_name):
            continue
        if not _column_exists(table_name, column_name):
            continue

        current_fk = _find_conversation_fk(table_name, column_name)
        if _is_fk_ondelete_cascade(current_fk):
            continue

        current_fk_name = str((current_fk or {}).get('name') or '').strip()
        if current_fk_name:
            op.drop_constraint(current_fk_name, table_name, type_='foreignkey')
        else:
            _drop_fk_if_exists(table_name, target_fk_name)

        op.create_foreign_key(
            target_fk_name,
            table_name,
            'conversations',
            [column_name],
            ['id'],
            ondelete='CASCADE',
        )


def downgrade() -> None:
    for table_name, column_name, target_fk_name in CONVERSATION_FOREIGN_KEYS:
        if not _table_exists(table_name):
            continue
        if not _column_exists(table_name, column_name):
            continue

        _drop_fk_if_exists(table_name, target_fk_name)
        current_fk = _find_conversation_fk(table_name, column_name)
        if current_fk is not None:
            continue

        op.create_foreign_key(
            target_fk_name,
            table_name,
            'conversations',
            [column_name],
            ['id'],
        )
