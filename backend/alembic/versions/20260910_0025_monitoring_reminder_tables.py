"""新增腎友監測提醒排程資料表

Revision ID: 20260910_0025
Revises: 20260909_0024
Create Date: 2026-09-10 09:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '20260910_0025'
down_revision: str | None = '20260909_0024'
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

    if not _table_exists('monitoring_reminder_policies'):
        op.create_table(
            'monitoring_reminder_policies',
            sa.Column('id', id_type, nullable=False),
            sa.Column('patient_id', id_type, nullable=True),
            sa.Column('channel', sa.Enum('line', name='monitoring_reminder_channel_enum'), nullable=False, server_default=sa.text("'line'")),
            sa.Column('window', sa.Enum('MORNING', 'EVENING', name='monitoring_reminder_window_enum'), nullable=False),
            sa.Column('requires_glucose_for_diabetic', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('message_template', sa.Text(), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['patient_id'], ['renal_patients.id']),
            sa.PrimaryKeyConstraint('id'),
        )

    if not _table_exists('monitoring_reminder_jobs'):
        op.create_table(
            'monitoring_reminder_jobs',
            sa.Column('id', id_type, nullable=False),
            sa.Column('policy_id', id_type, nullable=True),
            sa.Column('patient_id', id_type, nullable=False),
            sa.Column('target_date', sa.Date(), nullable=False),
            sa.Column('window', sa.Enum('MORNING', 'EVENING', name='monitoring_reminder_window_enum'), nullable=False),
            sa.Column('status', sa.Enum('pending', 'sent', 'failed', 'skipped', name='monitoring_reminder_job_status_enum'), nullable=False, server_default=sa.text("'pending'")),
            sa.Column('scheduled_at', sa.DateTime(), nullable=False),
            sa.Column('processed_at', sa.DateTime(), nullable=True),
            sa.Column('payload', sa.JSON(), nullable=True),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['patient_id'], ['renal_patients.id']),
            sa.ForeignKeyConstraint(['policy_id'], ['monitoring_reminder_policies.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_monitoring_reminder_jobs_target_window_status', 'monitoring_reminder_jobs', ['target_date', 'window', 'status'], unique=False)

    if not _table_exists('monitoring_reminder_delivery_logs'):
        op.create_table(
            'monitoring_reminder_delivery_logs',
            sa.Column('id', id_type, nullable=False),
            sa.Column('job_id', id_type, nullable=False),
            sa.Column('patient_id', id_type, nullable=False),
            sa.Column('line_user_id', sa.String(length=128), nullable=True),
            sa.Column('delivery_status', sa.Enum('sent', 'failed', 'skipped', name='monitoring_reminder_delivery_status_enum'), nullable=False),
            sa.Column('detail', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['job_id'], ['monitoring_reminder_jobs.id']),
            sa.ForeignKeyConstraint(['patient_id'], ['renal_patients.id']),
            sa.PrimaryKeyConstraint('id'),
        )


def downgrade() -> None:
    if _table_exists('monitoring_reminder_delivery_logs'):
        op.drop_table('monitoring_reminder_delivery_logs')

    if _table_exists('monitoring_reminder_jobs'):
        op.drop_index('ix_monitoring_reminder_jobs_target_window_status', table_name='monitoring_reminder_jobs')
        op.drop_table('monitoring_reminder_jobs')

    if _table_exists('monitoring_reminder_policies'):
        op.drop_table('monitoring_reminder_policies')

    bind = op.get_bind()
    if str(bind.dialect.name or '').lower() == 'postgresql':
        op.execute('DROP TYPE IF EXISTS monitoring_reminder_delivery_status_enum')
        op.execute('DROP TYPE IF EXISTS monitoring_reminder_job_status_enum')
        op.execute('DROP TYPE IF EXISTS monitoring_reminder_window_enum')
        op.execute('DROP TYPE IF EXISTS monitoring_reminder_channel_enum')
