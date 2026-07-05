from fastapi import APIRouter, Depends
import uuid
from sqlalchemy.orm import Session
from typing import Any

from src.core.database import get_db
from src.models import User
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import forbidden_error, not_found_error, validation_error
from src.middleware.auth import get_password_hash
from src.middleware.rbac import Role

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


@router.post('/users')
async def create_user(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """由管理者建立使用者，允許指定角色；未指定則為 user。

    權限：需要 update_user。
    欄位：username（必填）、email（必填）、password（必填）、role（選填：user|agent_admin|admin）
    """
    if not check_permission(current_user, 'update_user'):
        raise forbidden_error()

    username = (payload or {}).get('username') or ''
    email = (payload or {}).get('email') or ''
    password = (payload or {}).get('password') or ''
    role = (payload or {}).get('role') or 'user'

    if not isinstance(username, str) or not username.strip():
        raise validation_error('username 為必填')
    if not isinstance(email, str) or not email.strip():
        raise validation_error('email 為必填')
    if not isinstance(password, str) or not password:
        raise validation_error('password 為必填')
    if role not in ('admin', 'agent_admin', 'user'):
        role = 'user'

    existing = db.query(User).filter((User.email == email) | (User.username == username)).first()
    if existing:
        raise validation_error('Email 或 username 已存在')

    user = User(
        username=username.strip(),
        email=email.strip(),
        password_hash=get_password_hash(password),
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        'id': str(user.id),
        'username': user.username,
        'email': user.email,
        'role': user.role,
        'created_at': user.created_at.isoformat() if user.created_at else None,
    }
