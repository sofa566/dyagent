"""新增 LINE 通道 Session 與訊息表

Revision ID: 20260902_0020
Revises: 20260830_0019
Create Date: 2026-09-02 10:20:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '20260902_0020'
down_revision: Union[str, None] = '20260830_0019'
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
    id_type = _id_type()

    if not _table_exists('line_channel_sessions'):
        op.create_table(
            'line_channel_sessions',
            sa.Column('id', id_type, nullable=False),
            sa.Column('line_user_id', sa.String(length=128), nullable=False),
            sa.Column('conversation_id', id_type, nullable=False),
            sa.Column('assigned_agent_id', id_type, nullable=True),
            sa.Column('mode', sa.Enum('bot', 'human', name='line_session_mode_enum'), nullable=False, server_default=sa.text("'bot'")),
            sa.Column('status', sa.Enum('active', 'archived', name='line_session_status_enum'), nullable=False, server_default=sa.text("'active'")),
            sa.Column('last_inbound_at', sa.DateTime(), nullable=True),
            sa.Column('last_outbound_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id']),
            sa.ForeignKeyConstraint(['assigned_agent_id'], ['agents.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('line_user_id', name='uq_line_channel_sessions_line_user_id'),
        )
        op.create_index('ix_line_channel_sessions_updated_at', 'line_channel_sessions', ['updated_at'], unique=False)

    if not _table_exists('line_messages'):
        op.create_table(
            'line_messages',
            sa.Column('id', id_type, nullable=False),
            sa.Column('session_id', id_type, nullable=False),
            sa.Column('conversation_id', id_type, nullable=True),
            sa.Column('direction', sa.Enum('inbound', 'outbound', name='line_message_direction_enum'), nullable=False),
            sa.Column('sender_type', sa.Enum('user', 'agent', 'operator', 'system', name='line_sender_type_enum'), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('line_message_id', sa.String(length=128), nullable=True),
            sa.Column('reply_token', sa.String(length=120), nullable=True),
            sa.Column('operator_user_id', id_type, nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['session_id'], ['line_channel_sessions.id']),
            sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id']),
            sa.ForeignKeyConstraint(['operator_user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_line_messages_session_created_at', 'line_messages', ['session_id', 'created_at'], unique=False)


def downgrade() -> None:
    if _table_exists('line_messages'):
        op.drop_index('ix_line_messages_session_created_at', table_name='line_messages')
        op.drop_table('line_messages')

    if _table_exists('line_channel_sessions'):
        op.drop_index('ix_line_channel_sessions_updated_at', table_name='line_channel_sessions')
        op.drop_table('line_channel_sessions')

    bind = op.get_bind()
    if str(bind.dialect.name or '').lower() == 'postgresql':
        op.execute('DROP TYPE IF EXISTS line_sender_type_enum')
        op.execute('DROP TYPE IF EXISTS line_message_direction_enum')
        op.execute('DROP TYPE IF EXISTS line_session_status_enum')
        op.execute('DROP TYPE IF EXISTS line_session_mode_enum')
