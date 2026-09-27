from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error, validation_error
from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import User
from src.services.monitoring_backfill_service import monitoring_backfill_service
from src.services.monitoring_reminder_service import monitoring_reminder_service

router = APIRouter()


def _require_monitoring_permission(*, current_user: User, db: Session, permission_keys: list[str]) -> None:
    # 目的：檢查腎友提醒模組授權。
    # 為什麼：避免腎友照護舊權限橫向取得腎友提醒操作權限，需嚴格依新權限鍵檢查。
    normalized_permission_keys = [str(permission_key or '').strip() for permission_key in permission_keys if str(permission_key or '').strip()]
    can_manage = bool(any(check_permission(current_user, permission_key, db=db) for permission_key in normalized_permission_keys))
    if not can_manage:
        raise forbidden_error('無權限操作腎友回報完整性與提醒')


def _parse_target_date(*, value: str) -> datetime.date:
    normalized_value = str(value or '').strip()
    if not normalized_value:
        return datetime.now().date()
    try:
        return datetime.fromisoformat(normalized_value).date()
    except Exception as error:
        raise validation_error('date 格式錯誤，需為 YYYY-MM-DD') from error


def _parse_backfill_date_range(*, payload: dict) -> tuple[datetime.date, datetime.date]:
    # 目的：解析補寫 API 的日期區間參數。
    # 為什麼：手動補帳需要可控範圍，並提供合理預設值避免誤掃過大區間。
    normalized_start_date = str((payload or {}).get('start_date') or '').strip()
    normalized_end_date = str((payload or {}).get('end_date') or '').strip()
    if not normalized_end_date:
        end_date = datetime.now().date()
    else:
        try:
            end_date = datetime.fromisoformat(normalized_end_date).date()
        except Exception as error:
            raise validation_error('end_date 格式錯誤，需為 YYYY-MM-DD') from error
    if not normalized_start_date:
        start_date = end_date - timedelta(days=1)
    else:
        try:
            start_date = datetime.fromisoformat(normalized_start_date).date()
        except Exception as error:
            raise validation_error('start_date 格式錯誤，需為 YYYY-MM-DD') from error
    return start_date, end_date


@router.get('/monitoring-compliance/daily')
async def get_daily_monitoring_compliance(
    date: str = '',
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_monitoring_permission(current_user=current_user, db=db, permission_keys=['monitoring.compliance.read'])
    target_date = _parse_target_date(value=date)
    return monitoring_reminder_service.get_daily_compliance(db=db, target_date=target_date)


@router.post('/monitoring-compliance/reminders/dispatch')
async def dispatch_monitoring_reminders(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_monitoring_permission(current_user=current_user, db=db, permission_keys=['monitoring.reminder.dispatch'])
    target_date = _parse_target_date(value=str((payload or {}).get('date') or '').strip())
    window = str((payload or {}).get('window') or '').strip().upper() or 'EVENING'
    force = bool((payload or {}).get('force'))
    return await monitoring_reminder_service.dispatch_reminders(db=db, target_date=target_date, window=window, force=force)


@router.get('/monitoring-compliance/reminders/policies')
async def list_monitoring_reminder_policies(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_monitoring_permission(current_user=current_user, db=db, permission_keys=['monitoring.reminder.policy.read'])
    return monitoring_reminder_service.list_policies(db=db)


@router.put('/monitoring-compliance/reminders/policies/{policy_id}')
async def update_monitoring_reminder_policy(
    policy_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_monitoring_permission(current_user=current_user, db=db, permission_keys=['monitoring.reminder.policy.update'])
    return monitoring_reminder_service.update_policy(db=db, policy_id=policy_id, payload=payload)


@router.get('/monitoring-compliance/reminders/logs')
async def get_monitoring_reminder_logs(
    date: str = '',
    window: str = '',
    status: str = '',
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_monitoring_permission(current_user=current_user, db=db, permission_keys=['monitoring.reminder.log.read'])
    target_date = _parse_target_date(value=date)
    return monitoring_reminder_service.get_dispatch_report(
        db=db,
        target_date=target_date,
        window=window,
        status=status,
        limit=limit,
    )


@router.post('/monitoring-compliance/backfill')
async def run_monitoring_backfill(
    payload: dict = Body(default={}),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：手動觸發 LINE 歷史監測資料補寫。
    # 為什麼：營運需要可控補帳入口，在資料缺漏時能立即回補而不等排程。
    _require_monitoring_permission(current_user=current_user, db=db, permission_keys=['monitoring.backfill.run'])
    start_date, end_date = _parse_backfill_date_range(payload=payload)
    max_scan_rows = int((payload or {}).get('max_scan_rows') or 5000)
    return await monitoring_backfill_service.run_backfill(
        db=db,
        start_date=start_date,
        end_date=end_date,
        triggered_by='manual',
        max_scan_rows=max_scan_rows,
    )


@router.get('/monitoring-compliance/backfill/runs')
async def list_monitoring_backfill_runs(
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_monitoring_permission(current_user=current_user, db=db, permission_keys=['monitoring.backfill.read'])
    return await monitoring_backfill_service.list_recent_runs(limit=limit)
