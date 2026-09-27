"""新增衛教來源規則表

Revision ID: 20260917_0029
Revises: 20260916_0028
Create Date: 2026-09-17 19:10:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '20260917_0029'
down_revision: str | None = '20260916_0028'
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
    if _table_exists('health_education_source_rules'):
        return

    op.create_table(
        'health_education_source_rules',
        sa.Column('id', id_type, nullable=False),
        sa.Column('domain', sa.String(length=255), nullable=False),
        sa.Column('policy', sa.Enum('trusted', 'blocked', name='health_education_source_policy_enum'), nullable=False, server_default='trusted'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('created_by_user_id', id_type, nullable=True),
        sa.Column('updated_by_user_id', id_type, nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['updated_by_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('domain', name='uq_health_education_source_rules_domain'),
    )
    op.create_index('ix_health_education_source_rules_policy', 'health_education_source_rules', ['policy'])
    op.create_index('ix_health_education_source_rules_enabled', 'health_education_source_rules', ['enabled'])


def downgrade() -> None:
    if not _table_exists('health_education_source_rules'):
        return
    op.drop_index('ix_health_education_source_rules_enabled', table_name='health_education_source_rules')
    op.drop_index('ix_health_education_source_rules_policy', table_name='health_education_source_rules')
    op.drop_table('health_education_source_rules')
