"""新增排程模板與擴充執行器類型

Revision ID: 20260915_0027
Revises: 20260914_0026
Create Date: 2026-09-15 14:20:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '20260915_0027'
down_revision: str | None = '20260914_0026'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return any(column.get('name') == column_name for column in inspector.get_columns(table_name))


def _id_type() -> sa.types.TypeEngine:
    bind = op.get_bind()
    if str(bind.dialect.name or '').lower() == 'postgresql':
        return postgresql.UUID(as_uuid=False)
    return sa.String(length=36)


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = str(bind.dialect.name or '').lower()
    id_type = _id_type()
    if dialect_name == 'postgresql':
        task_type_enum: sa.types.TypeEngine = postgresql.ENUM(
            'http_call',
            'bash_script',
            'nodejs_script',
            'python_script',
            'renal_reminder_dispatch',
            'monitoring_backfill',
            name='scheduled_task_type_enum',
            create_type=False,
        )
    else:
        task_type_enum = sa.Enum(
            'http_call',
            'bash_script',
            'nodejs_script',
            'python_script',
            'renal_reminder_dispatch',
            'monitoring_backfill',
            name='scheduled_task_type_enum',
        )

    if dialect_name == 'postgresql':
        for enum_value in ('bash_script', 'nodejs_script', 'python_script'):
            op.execute(f"ALTER TYPE scheduled_task_type_enum ADD VALUE IF NOT EXISTS '{enum_value}'")

    if not _table_exists('scheduled_task_templates'):
        op.create_table(
            'scheduled_task_templates',
            sa.Column('id', id_type, nullable=False),
            sa.Column('template_key', sa.String(length=80), nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column(
                'executor_type',
                task_type_enum,
                nullable=False,
            ),
            sa.Column('payload_schema', sa.JSON(), nullable=True),
            sa.Column('default_payload', sa.JSON(), nullable=True),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('template_key', name='uq_scheduled_task_templates_template_key'),
        )

    if not _column_exists('scheduled_tasks', 'template_id'):
        op.add_column('scheduled_tasks', sa.Column('template_id', id_type, nullable=True))
        op.create_foreign_key(
            'fk_scheduled_tasks_template_id',
            'scheduled_tasks',
            'scheduled_task_templates',
            ['template_id'],
            ['id'],
        )


def downgrade() -> None:
    if _column_exists('scheduled_tasks', 'template_id'):
        op.drop_constraint('fk_scheduled_tasks_template_id', 'scheduled_tasks', type_='foreignkey')
        op.drop_column('scheduled_tasks', 'template_id')

    if _table_exists('scheduled_task_templates'):
        op.drop_table('scheduled_task_templates')
