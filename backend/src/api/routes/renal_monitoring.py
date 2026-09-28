from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error
from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import User
from src.services.renal_monitoring_service import renal_monitoring_service

router = APIRouter()


def _require_renal_nurse_permission(*, current_user: User, db: Session) -> None:
    # 目的：限制腎友照護後台 API 僅授權人員可操作。
    # 為什麼：腎友資料屬敏感資訊，需避免一般帳號直接讀寫。
    can_access = bool(
        check_permission(current_user, 'nursing.trends', db=db)
        or check_permission(current_user, 'read_logs', db=db)
        or check_permission(current_user, 'update_agent', db=db)
    )
    if not can_access:
        raise forbidden_error('無權限操作腎友照護資料')


@router.post('/monitoring-records')
async def create_monitoring_record(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_nurse_permission(current_user=current_user, db=db)
    return renal_monitoring_service.create_monitoring_record(db=db, payload=payload)


@router.get('/monitoring-records')
async def list_monitoring_records(
    patient_id: str,
    days: int = 7,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_nurse_permission(current_user=current_user, db=db)
    return renal_monitoring_service.list_monitoring_records(db=db, patient_code=str(patient_id or '').strip(), days=days)
