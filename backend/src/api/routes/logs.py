import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import Log, User

router = APIRouter()


@router.get('/logs')
async def get_logs(
    level: str | None = None,
    action: str | None = None,
    user_id: str | None = None,
    page: int = 1,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not (
        check_permission(current_user, 'logs.read', db=db)
        or check_permission(current_user, 'read_logs', db=db)
    ):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    query = db.query(Log)

    if level:
        query = query.filter(Log.level == level)
    if action:
        query = query.filter(Log.action == action)
    if user_id:
        query = query.filter(Log.user_id == user_id)

    total = query.count()
    logs = query.offset((page - 1) * limit).limit(limit).all()

    return {
        'logs': [
            {
                'id': str(log_row.id),
                'user_id': str(log_row.user_id) if log_row.user_id else None,
                'level': log_row.level,
                'action': log_row.action,
                'resource_type': log_row.resource_type,
                'resource_id': str(log_row.resource_id) if log_row.resource_id else None,
                'details': log_row.details,
                'ip_address': log_row.ip_address,
                'timestamp': log_row.timestamp.isoformat() if log_row.timestamp else None,
            }
            for log_row in logs
        ],
        'total': total,
        'page': page,
        'limit': limit,
    }


@router.get('/logs/{log_id}')
async def get_log(
    log_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not (
        check_permission(current_user, 'logs.read', db=db)
        or check_permission(current_user, 'read_logs', db=db)
    ):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    from src.api.errors import not_found_error

    try:
        uuid.UUID(str(log_id))
    except ValueError as error:
        raise not_found_error('Log', log_id) from error

    log = db.query(Log).filter(Log.id == log_id).first()
    if not log:
        raise not_found_error('Log', log_id)

    return {
        'id': str(log.id),
        'user_id': str(log.user_id) if log.user_id else None,
        'level': log.level,
        'action': log.action,
        'resource_type': log.resource_type,
        'resource_id': str(log.resource_id) if log.resource_id else None,
        'details': log.details,
        'ip_address': log.ip_address,
        'timestamp': log.timestamp.isoformat() if log.timestamp else None,
    }
