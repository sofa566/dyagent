from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.errors import validation_error
from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import require_permission
from src.models import User
from src.services.cost_usage_service import cost_usage_service

router = APIRouter()


def _resolve_recent_window(*, days: int) -> tuple[datetime, datetime]:
    # 目的：將天數參數轉為查詢時間窗。
    # 為什麼：集中窗口解析邏輯，避免每個端點各自處理造成不一致。
    safe_days = max(1, min(365, int(days)))
    end_at = datetime.now()
    start_at = end_at.replace(hour=0, minute=0, second=0, microsecond=0)
    delta_days = max(0, safe_days - 1)
    if delta_days > 0:
        from datetime import timedelta

        start_at = start_at - timedelta(days=delta_days)
    return start_at, end_at


def _resolve_month_window(*, month: str | None) -> tuple[datetime, datetime]:
    # 目的：回傳指定月份查詢窗口（含當月預設）。
    # 為什麼：個人月用量是治理基礎，需保證邊界計算一致。
    if month is None or not str(month).strip():
        now = datetime.now()
        month_start = datetime(now.year, now.month, 1)
    else:
        normalized_month = str(month or '').strip()
        try:
            parsed = datetime.strptime(normalized_month, '%Y-%m')
        except Exception as error:
            raise validation_error('month 格式必須為 YYYY-MM') from error
        month_start = datetime(parsed.year, parsed.month, 1)

    if month_start.month == 12:
        month_end = datetime(month_start.year + 1, 1, 1)
    else:
        month_end = datetime(month_start.year, month_start.month + 1, 1)
    return month_start, month_end


@router.get('/admin/cost/overview')
@require_permission('dashboard.read')
async def get_admin_cost_overview(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：提供管理端公司層成本總覽。
    # 為什麼：治理決策需先看總盤，再下鑽明細。
    start_at, end_at = _resolve_recent_window(days=days)
    window = cost_usage_service.build_window(start_at=start_at, end_at=end_at)
    return cost_usage_service.summarize_overview(db=db, window=window)


@router.get('/admin/cost/breakdown')
@require_permission('dashboard.read')
async def get_admin_cost_breakdown(
    dimension: str = Query(default='agent'),
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：提供群組/個人/代理者三種維度成本明細。
    # 為什麼：四層治理需要可下鑽視角快速定位成本來源。
    normalized_dimension = str(dimension or '').strip().lower()
    if normalized_dimension not in {'group', 'user', 'agent'}:
        raise validation_error('dimension 只允許 group、user、agent')
    start_at, end_at = _resolve_recent_window(days=days)
    window = cost_usage_service.build_window(start_at=start_at, end_at=end_at)
    return cost_usage_service.list_breakdown(
        db=db,
        window=window,
        dimension=normalized_dimension,
        limit=limit,
        offset=offset,
    )


@router.get('/me/cost/usage')
async def get_my_cost_usage(
    month: str | None = Query(default=None, description='格式 YYYY-MM，不填則為當月'),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：回傳登入使用者本月 token/cost 累計。
    # 為什麼：個人可見性可降低超支風險並提升自主管理。
    month_start, month_end = _resolve_month_window(month=month)
    return cost_usage_service.get_user_monthly_usage(
        db=db,
        user_id=str(current_user.id),
        month_start=month_start,
        month_end=month_end,
    )


@router.get('/admin/cost/policies')
@require_permission('dashboard.read')
async def get_admin_cost_policies(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return {'items': cost_usage_service.list_policies(db=db)}


@router.put('/admin/cost/policies')
@require_permission('dashboard.read')
async def put_admin_cost_policies(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：批次更新四層成本政策。
    # 為什麼：治理策略通常跨多層調整，需一次提交避免中間狀態不一致。
    policies = list((payload or {}).get('policies') or [])
    if not policies:
        raise validation_error('policies 不可為空')
    try:
        saved_rows = cost_usage_service.upsert_policies(db=db, policies=policies)
    except ValueError as error:
        raise validation_error(str(error)) from error
    return {'items': saved_rows}


@router.post('/admin/cost/alerts/evaluate')
@require_permission('dashboard.read')
async def post_admin_cost_alerts_evaluate(
    month: str | None = Query(default=None, description='格式 YYYY-MM，不填則為當月'),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：手動觸發月度告警計算。
    # 為什麼：warn-only 階段先建立可觀測流程，便於逐步驗證口徑。
    month_start, month_end = _resolve_month_window(month=month)
    return cost_usage_service.evaluate_monthly_alerts(db=db, month_start=month_start, month_end=month_end)


@router.get('/admin/cost/alerts')
@require_permission('dashboard.read')
async def get_admin_cost_alerts(
    month: str | None = Query(default=None, description='格式 YYYY-MM，不填則為當月'),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    month_start, month_end = _resolve_month_window(month=month)
    return cost_usage_service.list_alert_events(
        db=db,
        month_start=month_start,
        month_end=month_end,
        limit=limit,
        offset=offset,
    )
