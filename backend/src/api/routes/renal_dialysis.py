from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error
from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import User
from src.services.renal_dialysis_service import renal_dialysis_service

router = APIRouter()


def _require_renal_nurse_permission(*, current_user: User, db: Session) -> None:
    can_access = bool(
        check_permission(current_user, 'nursing.dialysis', db=db)
        or check_permission(current_user, 'read_logs', db=db)
        or check_permission(current_user, 'update_agent', db=db)
    )
    if not can_access:
        raise forbidden_error('無權限操作透析療程資料')


@router.post('/dialysis-sessions')
async def create_dialysis_session(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_nurse_permission(current_user=current_user, db=db)
    return renal_dialysis_service.create_session(db=db, payload=payload)


@router.post('/dialysis-sessions/{session_id}/pre-check')
async def update_dialysis_pre_check(
    session_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_nurse_permission(current_user=current_user, db=db)
    return renal_dialysis_service.update_pre_check(db=db, session_id=str(session_id), payload=payload)


@router.post('/dialysis-events')
async def create_dialysis_event(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_nurse_permission(current_user=current_user, db=db)
    return renal_dialysis_service.create_event(db=db, payload=payload)


@router.post('/dialysis-post-check')
async def create_dialysis_post_check(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_nurse_permission(current_user=current_user, db=db)
    return renal_dialysis_service.post_check(db=db, payload=payload)
