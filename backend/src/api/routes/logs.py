from fastapi import APIRouter, Depends
import uuid
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models import User, Log
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission

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
    if not check_permission(current_user, 'read_logs'):
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
                'id': str(l.id),
                'user_id': str(l.user_id) if l.user_id else None,
                'level': l.level,
                'action': l.action,
                'resource_type': l.resource_type,
                'resource_id': str(l.resource_id) if l.resource_id else None,
                'details': l.details,
                'ip_address': l.ip_address,
                'timestamp': l.timestamp.isoformat() if l.timestamp else None,
            }
            for l in logs
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
    if not check_permission(current_user, 'read_logs'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    from src.api.errors import not_found_error

    try:
        uuid.UUID(str(log_id))
    except ValueError:
        raise not_found_error('Log', log_id)

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
