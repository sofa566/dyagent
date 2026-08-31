"""移除 Agent 舊綁定欄位

Revision ID: 20260830_0018
Revises: 20260830_0017
Create Date: 2026-08-30 23:30:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260830_0018'
down_revision: Union[str, None] = '20260830_0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = inspector.get_columns(table_name)
    return any(str(column.get('name') or '') == column_name for column in columns)


def upgrade() -> None:
    with op.batch_alter_table('agents') as batch_op:
        if _column_exists('agents', 'mcp_config'):
            batch_op.drop_column('mcp_config')
        if _column_exists('agents', 'skills'):
            batch_op.drop_column('skills')
        if _column_exists('agents', 'tools'):
            batch_op.drop_column('tools')
        if _column_exists('agents', 'rag_config'):
            batch_op.drop_column('rag_config')


def downgrade() -> None:
    with op.batch_alter_table('agents') as batch_op:
        if not _column_exists('agents', 'mcp_config'):
            batch_op.add_column(sa.Column('mcp_config', sa.JSON(), nullable=True))
        if not _column_exists('agents', 'skills'):
            batch_op.add_column(sa.Column('skills', sa.JSON(), nullable=True))
        if not _column_exists('agents', 'tools'):
            batch_op.add_column(sa.Column('tools', sa.JSON(), nullable=True))
        if not _column_exists('agents', 'rag_config'):
            batch_op.add_column(sa.Column('rag_config', sa.JSON(), nullable=True))
