"""新增 LINE 會話自動綁定欄位

Revision ID: 20260907_0023
Revises: 20260906_0022
Create Date: 2026-09-07 10:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '20260907_0023'
down_revision: str | None = '20260906_0022'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column_names = [column.get('name') for column in inspector.get_columns(table_name)]
    return column_name in column_names


def _id_type() -> sa.types.TypeEngine:
    bind = op.get_bind()
    if str(bind.dialect.name or '').lower() == 'postgresql':
        return postgresql.UUID(as_uuid=False)
    return sa.String(length=36)


def upgrade() -> None:
    id_type = _id_type()
    if not _column_exists('line_channel_sessions', 'bound_patient_id'):
        op.add_column('line_channel_sessions', sa.Column('bound_patient_id', id_type, nullable=True))
        op.create_foreign_key(
            'fk_line_channel_sessions_bound_patient_id',
            'line_channel_sessions',
            'renal_patients',
            ['bound_patient_id'],
            ['id'],
        )
    if not _column_exists('line_channel_sessions', 'binding_status'):
        op.add_column('line_channel_sessions', sa.Column('binding_status', sa.String(length=32), nullable=False, server_default='pending_name'))
        op.alter_column('line_channel_sessions', 'binding_status', server_default=None)
    if not _column_exists('line_channel_sessions', 'binding_name'):
        op.add_column('line_channel_sessions', sa.Column('binding_name', sa.String(length=100), nullable=True))
    if not _column_exists('line_channel_sessions', 'binding_phone'):
        op.add_column('line_channel_sessions', sa.Column('binding_phone', sa.String(length=32), nullable=True))
    if not _column_exists('line_channel_sessions', 'bound_at'):
        op.add_column('line_channel_sessions', sa.Column('bound_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    if _column_exists('line_channel_sessions', 'bound_at'):
        op.drop_column('line_channel_sessions', 'bound_at')
    if _column_exists('line_channel_sessions', 'binding_phone'):
        op.drop_column('line_channel_sessions', 'binding_phone')
    if _column_exists('line_channel_sessions', 'binding_name'):
        op.drop_column('line_channel_sessions', 'binding_name')
    if _column_exists('line_channel_sessions', 'binding_status'):
        op.drop_column('line_channel_sessions', 'binding_status')
    if _column_exists('line_channel_sessions', 'bound_patient_id'):
        op.drop_constraint('fk_line_channel_sessions_bound_patient_id', 'line_channel_sessions', type_='foreignkey')
        op.drop_column('line_channel_sessions', 'bound_patient_id')
