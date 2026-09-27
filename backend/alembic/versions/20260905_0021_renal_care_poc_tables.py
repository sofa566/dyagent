"""新增腎友照護 POC 資料表

Revision ID: 20260905_0021
Revises: 20260902_0020
Create Date: 2026-09-05 14:10:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '20260905_0021'
down_revision: str | None = '20260902_0020'
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

    if not _table_exists('renal_patients'):
        op.create_table(
            'renal_patients',
            sa.Column('id', id_type, nullable=False),
            sa.Column('patient_code', sa.String(length=24), nullable=False),
            sa.Column('display_name', sa.String(length=100), nullable=False),
            sa.Column('age', sa.String(length=20), nullable=True),
            sa.Column('diagnosis', sa.String(length=255), nullable=True),
            sa.Column('primary_nurse_id', sa.String(length=32), nullable=True),
            sa.Column('is_diabetic', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('dry_weight_kg', sa.Numeric(8, 2), nullable=True),
            sa.Column('line_user_id', sa.String(length=128), nullable=True),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('patient_code', name='uq_renal_patients_patient_code'),
            sa.UniqueConstraint('line_user_id', name='uq_renal_patients_line_user_id'),
        )
        op.create_index('ix_renal_patients_enabled', 'renal_patients', ['enabled'], unique=False)

    if not _table_exists('monitoring_records'):
        op.create_table(
            'monitoring_records',
            sa.Column('id', id_type, nullable=False),
            sa.Column('patient_id', id_type, nullable=False),
            sa.Column('record_type', sa.Enum('MORNING', 'EVENING', 'SYMPTOM_REPORT', name='monitoring_record_type_enum'), nullable=False),
            sa.Column('recorded_at', sa.DateTime(), nullable=False),
            sa.Column('submitted_by_role', sa.Enum('PATIENT', 'FAMILY', 'CAREGIVER', 'NURSE', name='monitoring_submitted_role_enum'), nullable=False),
            sa.Column('measurements', sa.JSON(), nullable=True),
            sa.Column('symptoms', sa.JSON(), nullable=True),
            sa.Column('confirmed', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('record_status', sa.Enum('SAVED', 'REJECTED', name='monitoring_record_status_enum'), nullable=False, server_default=sa.text("'SAVED'")),
            sa.Column('comparison_result', sa.JSON(), nullable=True),
            sa.Column('matched_rules', sa.JSON(), nullable=True),
            sa.Column('follow_up_required', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('patient_message', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['patient_id'], ['renal_patients.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_monitoring_records_patient_recorded_at', 'monitoring_records', ['patient_id', 'recorded_at'], unique=False)

    if not _table_exists('follow_up_cases'):
        op.create_table(
            'follow_up_cases',
            sa.Column('id', id_type, nullable=False),
            sa.Column('patient_id', id_type, nullable=False),
            sa.Column('source_type', sa.Enum('monitoring', 'dialysis', name='follow_up_source_type_enum'), nullable=False),
            sa.Column('source_record_id', sa.String(length=64), nullable=True),
            sa.Column('severity', sa.Enum('info', 'warning', 'critical', name='follow_up_severity_enum'), nullable=False, server_default=sa.text("'warning'")),
            sa.Column('status', sa.Enum('OPEN', 'IN_PROGRESS', 'CLOSED', name='follow_up_status_enum'), nullable=False, server_default=sa.text("'OPEN'")),
            sa.Column('title', sa.String(length=255), nullable=False),
            sa.Column('details', sa.JSON(), nullable=True),
            sa.Column('assigned_nurse_id', sa.String(length=32), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.Column('closed_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['patient_id'], ['renal_patients.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_follow_up_cases_status_updated_at', 'follow_up_cases', ['status', 'updated_at'], unique=False)

    if not _table_exists('dialysis_sessions'):
        op.create_table(
            'dialysis_sessions',
            sa.Column('id', id_type, nullable=False),
            sa.Column('patient_id', id_type, nullable=False),
            sa.Column('dialysis_date', sa.Date(), nullable=False),
            sa.Column('shift', sa.Enum('MORNING', 'AFTERNOON', 'EVENING', name='dialysis_shift_enum'), nullable=False),
            sa.Column('bed_no', sa.String(length=20), nullable=False),
            sa.Column('machine_no', sa.String(length=30), nullable=False),
            sa.Column('pre_weight_kg', sa.Numeric(8, 2), nullable=True),
            sa.Column('dry_weight_kg', sa.Numeric(8, 2), nullable=True),
            sa.Column('target_uf_l', sa.Numeric(8, 2), nullable=True),
            sa.Column('post_weight_kg', sa.Numeric(8, 2), nullable=True),
            sa.Column('actual_uf_l', sa.Numeric(8, 2), nullable=True),
            sa.Column('completion_status', sa.Enum('CREATED', 'PRE_CHECK_CONFIRMED', 'IN_PROGRESS', 'COMPLETED', 'ENDED_EARLY', name='dialysis_completion_status_enum'), nullable=False, server_default=sa.text("'CREATED'")),
            sa.Column('has_tourniquet', sa.Boolean(), nullable=True),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('created_by', sa.String(length=32), nullable=True),
            sa.Column('confirmed_by', sa.String(length=32), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['patient_id'], ['renal_patients.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_dialysis_sessions_patient_date', 'dialysis_sessions', ['patient_id', 'dialysis_date'], unique=False)

    if not _table_exists('dialysis_events'):
        op.create_table(
            'dialysis_events',
            sa.Column('id', id_type, nullable=False),
            sa.Column('session_id', id_type, nullable=False),
            sa.Column('event_type', sa.String(length=60), nullable=False),
            sa.Column('event_at', sa.DateTime(), nullable=False),
            sa.Column('payload', sa.JSON(), nullable=True),
            sa.Column('handled_by', sa.String(length=32), nullable=True),
            sa.Column('action', sa.Text(), nullable=True),
            sa.Column('follow_up_required', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['session_id'], ['dialysis_sessions.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_dialysis_events_session_event_at', 'dialysis_events', ['session_id', 'event_at'], unique=False)

    if not _table_exists('patient_portal_links'):
        op.create_table(
            'patient_portal_links',
            sa.Column('id', id_type, nullable=False),
            sa.Column('patient_id', id_type, nullable=False),
            sa.Column('line_user_id', sa.String(length=128), nullable=False),
            sa.Column('token_hash', sa.String(length=128), nullable=False),
            sa.Column('reason', sa.String(length=60), nullable=True),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('used_at', sa.DateTime(), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column('created_by_user_id', id_type, nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['patient_id'], ['renal_patients.id']),
            sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('token_hash', name='uq_patient_portal_links_token_hash'),
        )
        op.create_index('ix_patient_portal_links_patient_expires_at', 'patient_portal_links', ['patient_id', 'expires_at'], unique=False)


def downgrade() -> None:
    if _table_exists('patient_portal_links'):
        op.drop_index('ix_patient_portal_links_patient_expires_at', table_name='patient_portal_links')
        op.drop_table('patient_portal_links')

    if _table_exists('dialysis_events'):
        op.drop_index('ix_dialysis_events_session_event_at', table_name='dialysis_events')
        op.drop_table('dialysis_events')

    if _table_exists('dialysis_sessions'):
        op.drop_index('ix_dialysis_sessions_patient_date', table_name='dialysis_sessions')
        op.drop_table('dialysis_sessions')

    if _table_exists('follow_up_cases'):
        op.drop_index('ix_follow_up_cases_status_updated_at', table_name='follow_up_cases')
        op.drop_table('follow_up_cases')

    if _table_exists('monitoring_records'):
        op.drop_index('ix_monitoring_records_patient_recorded_at', table_name='monitoring_records')
        op.drop_table('monitoring_records')

    if _table_exists('renal_patients'):
        op.drop_index('ix_renal_patients_enabled', table_name='renal_patients')
        op.drop_table('renal_patients')

    bind = op.get_bind()
    if str(bind.dialect.name or '').lower() == 'postgresql':
        op.execute('DROP TYPE IF EXISTS dialysis_completion_status_enum')
        op.execute('DROP TYPE IF EXISTS dialysis_shift_enum')
        op.execute('DROP TYPE IF EXISTS follow_up_status_enum')
        op.execute('DROP TYPE IF EXISTS follow_up_severity_enum')
        op.execute('DROP TYPE IF EXISTS follow_up_source_type_enum')
        op.execute('DROP TYPE IF EXISTS monitoring_record_status_enum')
        op.execute('DROP TYPE IF EXISTS monitoring_submitted_role_enum')
        op.execute('DROP TYPE IF EXISTS monitoring_record_type_enum')
