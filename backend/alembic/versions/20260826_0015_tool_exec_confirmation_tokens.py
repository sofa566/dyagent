"""新增工具二次確認一次性 token 資料表

Revision ID: 20260826_0015
Revises: 20260826_0014
Create Date: 2026-08-26 00:30:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '20260826_0015'
down_revision: Union[str, None] = '20260826_0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _id_type() -> sa.types.TypeEngine:
    bind = op.get_bind()
    if str(bind.dialect.name or '').lower() == 'postgresql':
        return postgresql.UUID(as_uuid=False)
    return sa.String(length=36)


def upgrade() -> None:
    if _table_exists('tool_execution_confirmations'):
        return

    id_type = _id_type()
    op.create_table(
        'tool_execution_confirmations',
        sa.Column('id', id_type, nullable=False),
        sa.Column('token_hash', sa.String(length=128), nullable=False),
        sa.Column('user_id', id_type, nullable=False),
        sa.Column('agent_id', id_type, nullable=True),
        sa.Column('conversation_id', id_type, nullable=True),
        sa.Column('tool_name', sa.String(length=160), nullable=False),
        sa.Column('payload_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id']),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash', name='uq_tool_execution_confirmation_token_hash'),
    )


def downgrade() -> None:
    if _table_exists('tool_execution_confirmations'):
        op.drop_table('tool_execution_confirmations')
