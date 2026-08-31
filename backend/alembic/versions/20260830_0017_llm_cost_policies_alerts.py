"""新增 LLM 成本政策與告警事件資料表

Revision ID: 20260830_0017
Revises: 20260830_0016
Create Date: 2026-08-30 22:10:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '20260830_0017'
down_revision: Union[str, None] = '20260830_0016'
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

    if not _table_exists('llm_cost_policies'):
        op.create_table(
            'llm_cost_policies',
            sa.Column('id', id_type, nullable=False),
            sa.Column('scope_type', sa.String(length=20), nullable=False),
            sa.Column('scope_id', id_type, nullable=True),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('enforcement_mode', sa.String(length=20), nullable=False, server_default=sa.text("'warn_only'")),
            sa.Column('monthly_input_tokens_limit', sa.Integer(), nullable=True),
            sa.Column('monthly_total_tokens_limit', sa.Integer(), nullable=True),
            sa.Column('monthly_cost_usd_limit', sa.Numeric(12, 6), nullable=True),
            sa.Column('warn_thresholds', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('scope_type', 'scope_id', name='uq_llm_cost_policy_scope'),
        )
        op.create_index('ix_llm_cost_policies_scope_type', 'llm_cost_policies', ['scope_type'], unique=False)

    if not _table_exists('llm_cost_alert_events'):
        op.create_table(
            'llm_cost_alert_events',
            sa.Column('id', id_type, nullable=False),
            sa.Column('policy_id', id_type, nullable=False),
            sa.Column('scope_type', sa.String(length=20), nullable=False),
            sa.Column('scope_id', id_type, nullable=True),
            sa.Column('metric_key', sa.String(length=40), nullable=False),
            sa.Column('threshold_percent', sa.Integer(), nullable=False),
            sa.Column('current_value', sa.Numeric(18, 6), nullable=False),
            sa.Column('limit_value', sa.Numeric(18, 6), nullable=False),
            sa.Column('usage_percent', sa.Numeric(8, 2), nullable=False),
            sa.Column('enforcement_mode', sa.String(length=20), nullable=False, server_default=sa.text("'warn_only'")),
            sa.Column('status', sa.String(length=20), nullable=False, server_default=sa.text("'warned'")),
            sa.Column('window_start', sa.DateTime(), nullable=False),
            sa.Column('window_end', sa.DateTime(), nullable=False),
            sa.Column('details', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['policy_id'], ['llm_cost_policies.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('policy_id', 'window_start', 'metric_key', 'threshold_percent', name='uq_llm_cost_alert_dedupe'),
        )
        op.create_index('ix_llm_cost_alert_events_scope', 'llm_cost_alert_events', ['scope_type', 'scope_id'], unique=False)
        op.create_index('ix_llm_cost_alert_events_window_start', 'llm_cost_alert_events', ['window_start'], unique=False)


def downgrade() -> None:
    if _table_exists('llm_cost_alert_events'):
        op.drop_index('ix_llm_cost_alert_events_window_start', table_name='llm_cost_alert_events')
        op.drop_index('ix_llm_cost_alert_events_scope', table_name='llm_cost_alert_events')
        op.drop_table('llm_cost_alert_events')

    if _table_exists('llm_cost_policies'):
        op.drop_index('ix_llm_cost_policies_scope_type', table_name='llm_cost_policies')
        op.drop_table('llm_cost_policies')
