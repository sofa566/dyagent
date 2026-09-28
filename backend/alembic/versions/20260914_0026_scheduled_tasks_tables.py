"""新增通用排程任務資料表

Revision ID: 20260914_0026
Revises: 20260910_0025
Create Date: 2026-09-14 15:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '20260914_0026'
down_revision: str | None = '20260910_0025'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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

    if not _table_exists('scheduled_tasks'):
        op.create_table(
            'scheduled_tasks',
            sa.Column('id', id_type, nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('cron_expression', sa.String(length=120), nullable=False),
            sa.Column('timezone', sa.String(length=80), nullable=False, server_default=sa.text("'Asia/Taipei'")),
            sa.Column(
                'task_type',
                sa.Enum('http_call', 'renal_reminder_dispatch', 'monitoring_backfill', name='scheduled_task_type_enum'),
                nullable=False,
            ),
            sa.Column('payload', sa.JSON(), nullable=True),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('source', sa.String(length=30), nullable=False, server_default=sa.text("'manual'")),
            sa.Column('created_by_user_id', id_type, nullable=True),
            sa.Column('updated_by_user_id', id_type, nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id']),
            sa.ForeignKeyConstraint(['updated_by_user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_scheduled_tasks_enabled_type', 'scheduled_tasks', ['enabled', 'task_type'], unique=False)

    if not _table_exists('scheduled_task_runs'):
        op.create_table(
            'scheduled_task_runs',
            sa.Column('id', id_type, nullable=False),
            sa.Column('task_id', id_type, nullable=False),
            sa.Column('trigger_source', sa.Enum('scheduler', 'manual', 'api', name='scheduled_task_trigger_source_enum'), nullable=False, server_default=sa.text("'scheduler'")),
            sa.Column('status', sa.Enum('queued', 'running', 'success', 'failed', name='scheduled_task_run_status_enum'), nullable=False, server_default=sa.text("'queued'")),
            sa.Column('started_at', sa.DateTime(), nullable=True),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.Column('duration_ms', sa.Integer(), nullable=True),
            sa.Column('input_payload', sa.JSON(), nullable=True),
            sa.Column('output_payload', sa.JSON(), nullable=True),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('executed_by_user_id', id_type, nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['task_id'], ['scheduled_tasks.id']),
            sa.ForeignKeyConstraint(['executed_by_user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_scheduled_task_runs_task_created', 'scheduled_task_runs', ['task_id', 'created_at'], unique=False)


def downgrade() -> None:
    if _table_exists('scheduled_task_runs'):
        op.drop_index('ix_scheduled_task_runs_task_created', table_name='scheduled_task_runs')
        op.drop_table('scheduled_task_runs')
    if _table_exists('scheduled_tasks'):
        op.drop_index('ix_scheduled_tasks_enabled_type', table_name='scheduled_tasks')
        op.drop_table('scheduled_tasks')

    bind = op.get_bind()
    if str(bind.dialect.name or '').lower() == 'postgresql':
        op.execute('DROP TYPE IF EXISTS scheduled_task_run_status_enum')
        op.execute('DROP TYPE IF EXISTS scheduled_task_trigger_source_enum')
        op.execute('DROP TYPE IF EXISTS scheduled_task_type_enum')
