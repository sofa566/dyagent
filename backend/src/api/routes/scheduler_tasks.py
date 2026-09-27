from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error
from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import User
from src.services.scheduler_task_service import scheduler_task_service

router = APIRouter()


def _require_scheduler_permission(*, current_user: User, db: Session, permission_keys: list[str]) -> None:
    # 目的：檢查排程管理模組授權。
    # 為什麼：避免腎友照護舊權限橫向取得排程管理操作權限，需嚴格依新權限鍵檢查。
    normalized_permission_keys = [str(permission_key or '').strip() for permission_key in permission_keys if str(permission_key or '').strip()]
    can_manage = bool(any(check_permission(current_user, permission_key, db=db) for permission_key in normalized_permission_keys))
    if not can_manage:
        raise forbidden_error('無權限管理排程任務')


@router.get('/scheduler/tasks')
async def list_scheduler_tasks(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_scheduler_permission(current_user=current_user, db=db, permission_keys=['scheduler.task.read'])
    return scheduler_task_service.list_tasks(db=db, only_enabled=enabled_only)


@router.post('/scheduler/tasks')
async def create_scheduler_task(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_scheduler_permission(current_user=current_user, db=db, permission_keys=['scheduler.task.create'])
    return scheduler_task_service.create_task(db=db, payload=payload, current_user=current_user)


@router.put('/scheduler/tasks/{task_id}')
async def update_scheduler_task(
    task_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_scheduler_permission(current_user=current_user, db=db, permission_keys=['scheduler.task.update'])
    return scheduler_task_service.update_task(db=db, task_id=task_id, payload=payload, current_user=current_user)


@router.delete('/scheduler/tasks/{task_id}')
async def delete_scheduler_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_scheduler_permission(current_user=current_user, db=db, permission_keys=['scheduler.task.delete'])
    return scheduler_task_service.delete_task(db=db, task_id=task_id)


@router.post('/scheduler/tasks/{task_id}/run')
async def run_scheduler_task_now(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_scheduler_permission(current_user=current_user, db=db, permission_keys=['scheduler.task.run'])
    return await scheduler_task_service.run_task_now(db=db, task_id=task_id, current_user=current_user)


@router.get('/scheduler/tasks/{task_id}/runs')
async def list_scheduler_task_runs(
    task_id: str,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_scheduler_permission(current_user=current_user, db=db, permission_keys=['scheduler.run.read'])
    return scheduler_task_service.list_runs(db=db, task_id=task_id, limit=limit)


@router.get('/scheduler/templates')
async def list_scheduler_templates(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_scheduler_permission(current_user=current_user, db=db, permission_keys=['scheduler.template.read'])
    return scheduler_task_service.list_templates(db=db, enabled_only=enabled_only)


@router.post('/scheduler/templates')
async def create_scheduler_template(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_scheduler_permission(current_user=current_user, db=db, permission_keys=['scheduler.template.create'])
    return scheduler_task_service.create_template(db=db, payload=payload)


@router.put('/scheduler/templates/{template_id}')
async def update_scheduler_template(
    template_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_scheduler_permission(current_user=current_user, db=db, permission_keys=['scheduler.template.update'])
    return scheduler_task_service.update_template(db=db, template_id=template_id, payload=payload)


@router.delete('/scheduler/templates/{template_id}')
async def delete_scheduler_template(
    template_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_scheduler_permission(current_user=current_user, db=db, permission_keys=['scheduler.template.delete'])
    return scheduler_task_service.delete_template(db=db, template_id=template_id)
