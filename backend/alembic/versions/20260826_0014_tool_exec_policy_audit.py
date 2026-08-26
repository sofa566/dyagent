"""新增工具策略欄位與工具執行稽核表

Revision ID: 20260826_0014
Revises: 20260814_0013
Create Date: 2026-08-26 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '20260826_0014'
down_revision: Union[str, None] = '20260814_0013'
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
    column_names = {str(col.get('name') or '') for col in inspector.get_columns(table_name)}
    return column_name in column_names


def _id_type() -> sa.types.TypeEngine:
    bind = op.get_bind()
    if str(bind.dialect.name or '').lower() == 'postgresql':
        return postgresql.UUID(as_uuid=False)
    return sa.String(length=36)


def upgrade() -> None:
    for table_name in ('mcp', 'skills', 'function_profiles'):
        if _column_exists(table_name, 'execution_policy'):
            continue
        op.add_column(table_name, sa.Column('execution_policy', sa.JSON(), nullable=True))

    if not _table_exists('tool_execution_audits'):
        id_type = _id_type()
        op.create_table(
            'tool_execution_audits',
            sa.Column('id', id_type, nullable=False),
            sa.Column('user_id', id_type, nullable=True),
            sa.Column('agent_id', id_type, nullable=True),
            sa.Column('conversation_id', id_type, nullable=True),
            sa.Column('tool_name', sa.String(length=160), nullable=False),
            sa.Column('tool_type', sa.String(length=20), nullable=True),
            sa.Column('risk_level', sa.String(length=20), nullable=False),
            sa.Column('cost_class', sa.String(length=20), nullable=False),
            sa.Column('allowlist_passed', sa.Boolean(), nullable=False),
            sa.Column('confirmation_required', sa.Boolean(), nullable=False),
            sa.Column('confirmation_passed', sa.Boolean(), nullable=False),
            sa.Column('quota_passed', sa.Boolean(), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('deny_reason', sa.String(length=80), nullable=True),
            sa.Column('payload_keys', sa.JSON(), nullable=True),
            sa.Column('cost_estimate', sa.Numeric(12, 6), nullable=True),
            sa.Column('latency_ms', sa.Integer(), nullable=True),
            sa.Column('details', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['agent_id'], ['agents.id']),
            sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id']),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )


def downgrade() -> None:
    if _table_exists('tool_execution_audits'):
        op.drop_table('tool_execution_audits')

    for table_name in ('function_profiles', 'skills', 'mcp'):
        if _column_exists(table_name, 'execution_policy'):
            op.drop_column(table_name, 'execution_policy')
