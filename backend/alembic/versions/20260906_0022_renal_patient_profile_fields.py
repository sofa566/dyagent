"""擴充腎友主檔欄位

Revision ID: 20260906_0022
Revises: 20260905_0021
Create Date: 2026-09-06 11:20:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '20260906_0022'
down_revision: str | None = '20260905_0021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column_names = [column.get('name') for column in inspector.get_columns(table_name)]
    return column_name in column_names


def upgrade() -> None:
    if not _column_exists('renal_patients', 'tel_no'):
        op.add_column('renal_patients', sa.Column('tel_no', sa.String(length=32), nullable=True))
    if not _column_exists('renal_patients', 'id_card'):
        op.add_column('renal_patients', sa.Column('id_card', sa.String(length=32), nullable=True))
    if not _column_exists('renal_patients', 'med_history'):
        op.add_column('renal_patients', sa.Column('med_history', sa.Text(), nullable=True))


def downgrade() -> None:
    if _column_exists('renal_patients', 'med_history'):
        op.drop_column('renal_patients', 'med_history')
    if _column_exists('renal_patients', 'id_card'):
        op.drop_column('renal_patients', 'id_card')
    if _column_exists('renal_patients', 'tel_no'):
        op.drop_column('renal_patients', 'tel_no')
