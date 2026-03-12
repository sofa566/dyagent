from fastapi import APIRouter, Depends
import uuid
from sqlalchemy.orm import Session
from typing import Any

from src.core.database import get_db
from src.models import User
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import forbidden_error, not_found_error, validation_error

router = APIRouter()


@router.get('/users')
async def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_user'):
        raise forbidden_error()

    users = db.query(User).all()
    return {
        'users': [
            {
                'id': str(u.id),
                'username': u.username,
                'email': u.email,
                'role': u.role,
                'created_at': u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ]
    }


@router.put('/users/{user_id}/role')
async def update_user_role(
    user_id: str,
    role: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_user'):
        raise forbidden_error()

    # Validate UUID
    try:
        uuid.UUID(str(user_id))
    except ValueError:
        raise not_found_error('User', user_id)

    # Validate role
    if role not in ('admin', 'agent_admin', 'user'):
        raise validation_error('Invalid role')

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise not_found_error('User', user_id)

    user.role = role
    db.commit()
    db.refresh(user)

    return {
        'id': str(user.id),
        'username': user.username,
        'email': user.email,
        'role': user.role,
    }
