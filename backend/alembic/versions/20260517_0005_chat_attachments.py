"""新增聊天附件 metadata 表

Revision ID: 20260517_0005
Revises: 20260408_0004
Create Date: 2026-05-17 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '20260517_0005'
down_revision: Union[str, None] = '20260408_0004'
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
    if _table_exists('chat_attachments'):
        return
    op.create_table(
        'chat_attachments',
        sa.Column('id', _id_type(), nullable=False),
        sa.Column('conversation_id', _id_type(), nullable=True),
        sa.Column('user_id', _id_type(), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('ext', sa.String(length=20), nullable=False, server_default=''),
        sa.Column('mime_type', sa.String(length=120), nullable=False, server_default='application/octet-stream'),
        sa.Column('file_path', sa.String(length=700), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='uploaded'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_chat_attachments_user_created', 'chat_attachments', ['user_id', 'created_at'])
    op.create_index('ix_chat_attachments_conversation', 'chat_attachments', ['conversation_id'])


def downgrade() -> None:
    if not _table_exists('chat_attachments'):
        return
    op.drop_index('ix_chat_attachments_conversation', table_name='chat_attachments')
    op.drop_index('ix_chat_attachments_user_created', table_name='chat_attachments')
    op.drop_table('chat_attachments')
