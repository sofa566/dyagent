"""多代理協作：新增 multi_agent_sessions 與 multi_agent_tasks 表

Revision ID: 20260328_0002
Revises: 20260328_0001
Create Date: 2026-03-28 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260328_0002"
down_revision: Union[str, None] = "20260328_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _enum_exists(enum_name: str) -> bool:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return False
    result = bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = :name"),
        {"name": enum_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    # --- Enum 型別（PostgreSQL） ---
    if is_pg and not _enum_exists('mas_status'):
        op.execute(
            "CREATE TYPE mas_status AS ENUM "
            "('planning','running','synthesizing','evaluating','done','failed')"
        )
    if is_pg and not _enum_exists('mat_status'):
        op.execute(
            "CREATE TYPE mat_status AS ENUM "
            "('pending','running','done','failed','skipped')"
        )

    # 根據 dialect 決定 UUID 欄位型別（PG 用原生 UUID，其餘用 CHAR(36)）
    def _uuid_col(name: str, fk: str | None = None, **kw):
        col_type = postgresql.UUID(as_uuid=False) if is_pg else sa.CHAR(36)
        if fk:
            return sa.Column(name, col_type, sa.ForeignKey(fk), **kw)
        return sa.Column(name, col_type, **kw)

    # --- multi_agent_sessions ---
    if not _table_exists('multi_agent_sessions'):
        mas_status_col = (
            postgresql.ENUM('planning', 'running', 'synthesizing', 'evaluating', 'done', 'failed',
                            name='mas_status', create_type=False)
            if is_pg
            else sa.String(20)
        )
        op.create_table(
            'multi_agent_sessions',
            _uuid_col('id', primary_key=True),
            _uuid_col('conversation_id', fk='conversations.id', nullable=False),
            _uuid_col('router_agent_id', fk='agents.id',        nullable=False),
            sa.Column('user_message',    sa.Text(),      nullable=False),
            sa.Column('status',          mas_status_col, nullable=False, server_default='planning'),
            sa.Column('react_step',      sa.Integer(),   nullable=False, server_default='0'),
            sa.Column('max_steps',       sa.Integer(),   nullable=False, server_default='3'),
            sa.Column('plan_json',       sa.JSON(),      nullable=True),
            sa.Column('synthesis',       sa.Text(),      nullable=True),
            sa.Column('eval_ok',         sa.Boolean(),   nullable=True),
            sa.Column('created_at',      sa.DateTime(),  server_default=sa.func.now()),
            sa.Column('updated_at',      sa.DateTime(),  server_default=sa.func.now()),
        )
        op.create_index('ix_mas_conversation_id', 'multi_agent_sessions', ['conversation_id'])
        op.create_index('ix_mas_status',          'multi_agent_sessions', ['status'])

    # --- multi_agent_tasks ---
    if not _table_exists('multi_agent_tasks'):
        mat_status_col = (
            postgresql.ENUM('pending', 'running', 'done', 'failed', 'skipped',
                            name='mat_status', create_type=False)
            if is_pg
            else sa.String(20)
        )
        op.create_table(
            'multi_agent_tasks',
            _uuid_col('id', primary_key=True),
            _uuid_col('session_id', fk='multi_agent_sessions.id', nullable=False),
            sa.Column('task_index',  sa.Integer(),   nullable=False),
            _uuid_col('agent_id',   fk='agents.id', nullable=False),
            sa.Column('task_desc',   sa.Text(),      nullable=False),
            sa.Column('depends_on',  sa.JSON(),      nullable=True),
            sa.Column('status',      mat_status_col, nullable=False, server_default='pending'),
            sa.Column('result_text', sa.Text(),      nullable=True),
            sa.Column('error',       sa.Text(),      nullable=True),
            sa.Column('started_at',  sa.DateTime(),  nullable=True),
            sa.Column('finished_at', sa.DateTime(),  nullable=True),
        )
        op.create_index('ix_mat_session_id',  'multi_agent_tasks', ['session_id'])
        op.create_index('ix_mat_agent_id',    'multi_agent_tasks', ['agent_id'])
        op.create_index('ix_mat_status',      'multi_agent_tasks', ['status'])


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    if _table_exists('multi_agent_tasks'):
        op.drop_index('ix_mat_status',     table_name='multi_agent_tasks')
        op.drop_index('ix_mat_agent_id',   table_name='multi_agent_tasks')
        op.drop_index('ix_mat_session_id', table_name='multi_agent_tasks')
        op.drop_table('multi_agent_tasks')

    if _table_exists('multi_agent_sessions'):
        op.drop_index('ix_mas_status',          table_name='multi_agent_sessions')
        op.drop_index('ix_mas_conversation_id', table_name='multi_agent_sessions')
        op.drop_table('multi_agent_sessions')

    if is_pg:
        if _enum_exists('mat_status'):
            op.execute('DROP TYPE mat_status')
        if _enum_exists('mas_status'):
            op.execute('DROP TYPE mas_status')
