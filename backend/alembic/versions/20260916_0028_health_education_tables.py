"""新增衛教內容與投遞紀錄

Revision ID: 20260916_0028
Revises: 20260915_0027
Create Date: 2026-09-16 10:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '20260916_0028'
down_revision: str | None = '20260915_0027'
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
    bind = op.get_bind()
    dialect_name = str(bind.dialect.name or '').lower()
    id_type = _id_type()

    if dialect_name == 'postgresql':
        op.execute("ALTER TYPE scheduled_task_type_enum ADD VALUE IF NOT EXISTS 'health_education_dispatch'")

    if not _table_exists('health_education_contents'):
        op.create_table(
            'health_education_contents',
            sa.Column('id', id_type, nullable=False),
            sa.Column('title', sa.String(length=300), nullable=False),
            sa.Column('source_name', sa.String(length=120), nullable=True),
            sa.Column('source_url', sa.Text(), nullable=False),
            sa.Column('summary', sa.Text(), nullable=True),
            sa.Column('tags', sa.JSON(), nullable=True),
            sa.Column(
                'status',
                sa.Enum('draft', 'approved', 'rejected', 'sent', name='health_education_status_enum'),
                nullable=False,
                server_default='draft',
            ),
            sa.Column('created_by_user_id', id_type, nullable=True),
            sa.Column('approved_by_user_id', id_type, nullable=True),
            sa.Column('approved_at', sa.DateTime(), nullable=True),
            sa.Column('last_sent_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['approved_by_user_id'], ['users.id']),
            sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('source_url', name='uq_health_education_contents_source_url'),
        )
        op.create_index('ix_health_education_contents_status', 'health_education_contents', ['status'])
        op.create_index('ix_health_education_contents_created_at', 'health_education_contents', ['created_at'])

    if not _table_exists('health_education_delivery_logs'):
        op.create_table(
            'health_education_delivery_logs',
            sa.Column('id', id_type, nullable=False),
            sa.Column('content_id', id_type, nullable=False),
            sa.Column('patient_id', id_type, nullable=False),
            sa.Column('line_user_id', sa.String(length=128), nullable=True),
            sa.Column('audience_rule', sa.String(length=60), nullable=False, server_default='all'),
            sa.Column('trigger_source', sa.Enum('manual', 'scheduler', name='health_education_trigger_source_enum'), nullable=False),
            sa.Column('status', sa.Enum('sent', 'failed', 'skipped', name='health_education_delivery_status_enum'), nullable=False),
            sa.Column('detail', sa.Text(), nullable=True),
            sa.Column('payload', sa.JSON(), nullable=True),
            sa.Column('sent_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['content_id'], ['health_education_contents.id']),
            sa.ForeignKeyConstraint(['patient_id'], ['renal_patients.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_health_education_delivery_logs_content_id', 'health_education_delivery_logs', ['content_id'])
        op.create_index('ix_health_education_delivery_logs_status', 'health_education_delivery_logs', ['status'])
        op.create_index('ix_health_education_delivery_logs_created_at', 'health_education_delivery_logs', ['created_at'])


def downgrade() -> None:
    if _table_exists('health_education_delivery_logs'):
        op.drop_index('ix_health_education_delivery_logs_created_at', table_name='health_education_delivery_logs')
        op.drop_index('ix_health_education_delivery_logs_status', table_name='health_education_delivery_logs')
        op.drop_index('ix_health_education_delivery_logs_content_id', table_name='health_education_delivery_logs')
        op.drop_table('health_education_delivery_logs')

    if _table_exists('health_education_contents'):
        op.drop_index('ix_health_education_contents_created_at', table_name='health_education_contents')
        op.drop_index('ix_health_education_contents_status', table_name='health_education_contents')
        op.drop_table('health_education_contents')
