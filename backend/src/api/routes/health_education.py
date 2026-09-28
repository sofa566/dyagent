from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error
from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import User
from src.services.health_education_service import health_education_service

router = APIRouter()


def _require_health_education_permission(*, current_user: User, db: Session, permission_keys: list[str]) -> None:
    # 目的：檢查衛教提醒模組授權。
    # 為什麼：避免腎友照護舊權限橫向取得衛教提醒操作權限，需嚴格依新權限鍵檢查。
    normalized_permission_keys = [str(permission_key or '').strip() for permission_key in permission_keys if str(permission_key or '').strip()]
    can_manage = bool(any(check_permission(current_user, permission_key, db=db) for permission_key in normalized_permission_keys))
    if not can_manage:
        raise forbidden_error('無權限操作衛教內容管理')


@router.post('/health-education/contents')
async def create_health_education_content(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.content.create'])
    return health_education_service.create_content(db=db, payload=payload, current_user=current_user)


@router.get('/health-education/contents')
async def list_health_education_contents(
    status: str = '',
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.content.read'])
    return health_education_service.list_contents(db=db, status=status, page=page, page_size=page_size)


@router.post('/health-education/contents/import-candidates')
async def import_health_education_candidates(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.import.manual'])
    return health_education_service.import_candidate_contents(db=db, payload=payload, current_user=current_user)


@router.post('/health-education/contents/import-from-mcp')
async def import_health_education_candidates_from_mcp(
    payload: dict = Body(default={}),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.import.mcp'])
    return health_education_service.import_candidates_from_mcp(db=db, payload=payload, current_user=current_user)


@router.get('/health-education/source-policy')
async def get_health_education_source_policy(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.source_rule.read'])
    return health_education_service.get_source_policy(db=db)


@router.get('/health-education/source-rules')
async def list_health_education_source_rules(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.source_rule.read'])
    return health_education_service.list_source_rules(db=db, enabled_only=enabled_only)


@router.post('/health-education/source-rules')
async def create_health_education_source_rule(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.source_rule.create'])
    return health_education_service.create_source_rule(db=db, payload=payload, current_user=current_user)


@router.put('/health-education/source-rules/{rule_id}')
async def update_health_education_source_rule(
    rule_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.source_rule.update'])
    return health_education_service.update_source_rule(db=db, rule_id=rule_id, payload=payload, current_user=current_user)


@router.delete('/health-education/source-rules/{rule_id}')
async def delete_health_education_source_rule(
    rule_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.source_rule.delete'])
    return health_education_service.delete_source_rule(db=db, rule_id=rule_id)


@router.put('/health-education/contents/{content_id}/approve')
async def approve_health_education_content(
    content_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.content.approve'])
    return health_education_service.approve_content(db=db, content_id=content_id, current_user=current_user)


@router.put('/health-education/contents/{content_id}/reject')
async def reject_health_education_content(
    content_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.content.reject'])
    return health_education_service.reject_content(db=db, content_id=content_id, current_user=current_user)


@router.post('/health-education/contents/{content_id}/send-now')
async def send_health_education_content_now(
    content_id: str,
    payload: dict = Body(default={}),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.content.send'])
    audience_rule = str((payload or {}).get('audience_rule') or 'all').strip().lower() or 'all'
    return await health_education_service.send_content_now(
        db=db,
        content_id=content_id,
        audience_rule=audience_rule,
        trigger_source='manual',
    )


@router.post('/health-education/contents/{content_id}/schedule')
async def schedule_health_education_content(
    content_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.content.schedule'])
    return health_education_service.schedule_content(db=db, content_id=content_id, payload=payload, current_user=current_user)


@router.get('/health-education/contents/{content_id}/logs')
async def list_health_education_delivery_logs(
    content_id: str,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.log.read'])
    return health_education_service.list_delivery_logs(db=db, content_id=content_id, limit=limit)


@router.delete('/health-education/contents/rejected')
async def clear_rejected_health_education_contents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.content.delete'])
    return health_education_service.clear_rejected_contents(db=db)


@router.delete('/health-education/contents/{content_id}')
async def delete_health_education_content(
    content_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_health_education_permission(current_user=current_user, db=db, permission_keys=['health_education.content.delete'])
    return health_education_service.delete_content(db=db, content_id=content_id)
