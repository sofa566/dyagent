from fastapi import APIRouter, Depends
import uuid
from sqlalchemy.orm import Session
from typing import Any

from src.core.database import get_db
from src.models import ChatAttachment, User, UserGroupBinding, UserRoleBinding
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import forbidden_error, not_found_error, validation_error
from src.middleware.auth import get_password_hash
from src.services.access_control_service import access_control_service

router = APIRouter()


def _role_for_response(db: Session, user: User) -> str:
    return access_control_service.resolve_primary_role_code(db, user)


@router.get('/users')
async def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_user', db=db):
        raise forbidden_error()

    users = db.query(User).all()
    return {
        'users': [
            {
                'id': str(u.id),
                'username': u.username,
                'email': u.email,
                'role': _role_for_response(db, u),
                'enabled': bool(getattr(u, 'enabled', True)),
                'created_at': u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ]
    }


@router.put('/users/{user_id}')
async def update_user_profile(
    user_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_user', db=db):
        raise forbidden_error()

    try:
        parsed_user_id = uuid.UUID(str(user_id))
    except ValueError:
        raise not_found_error('User', user_id)

    target_user = db.query(User).filter(User.id == parsed_user_id).first()
    if target_user is None:
        raise not_found_error('User', user_id)

    username = str((payload or {}).get('username') or '').strip()
    email = str((payload or {}).get('email') or '').strip()
    enabled = bool((payload or {}).get('enabled', True))

    if not username:
        raise validation_error('username 為必填')
    if not email:
        raise validation_error('email 為必填')

    existed_user = (
        db.query(User)
        .filter(
            ((User.username == username) | (User.email == email)),
            User.id != parsed_user_id,
        )
        .first()
    )
    if existed_user is not None:
        raise validation_error('Email 或 username 已存在')

    target_user.username = username
    target_user.email = email
    target_user.enabled = enabled
    db.commit()
    db.refresh(target_user)

    return {
        'id': str(target_user.id),
        'username': target_user.username,
        'email': target_user.email,
        'enabled': bool(target_user.enabled),
        'role': _role_for_response(db, target_user),
    }


@router.put('/users/{user_id}/role')
async def update_user_role(
    user_id: str,
    role: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_user', db=db):
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

    access_control_service.sync_user_system_role_binding(db, user, role)
    db.commit()
    db.refresh(user)

    return {
        'id': str(user.id),
        'username': user.username,
        'email': user.email,
        'role': _role_for_response(db, user),
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
    if not check_permission(current_user, 'update_user', db=db):
        raise forbidden_error()

    username = (payload or {}).get('username') or ''
    email = (payload or {}).get('email') or ''
    password = (payload or {}).get('password') or ''
    role = (payload or {}).get('role') or 'user'
    enabled = bool((payload or {}).get('enabled', True))

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
        enabled=enabled,
    )
    db.add(user)
    db.flush()
    access_control_service.sync_user_system_role_binding(db, user, role)
    db.commit()
    db.refresh(user)

    return {
        'id': str(user.id),
        'username': user.username,
        'email': user.email,
        'role': _role_for_response(db, user),
        'enabled': bool(user.enabled),
        'created_at': user.created_at.isoformat() if user.created_at else None,
    }


@router.delete('/users/{user_id}')
async def delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'delete_user', db=db):
        raise forbidden_error()

    try:
        parsed_user_id = uuid.UUID(str(user_id))
    except ValueError:
        raise not_found_error('User', user_id)

    if str(current_user.id) == str(parsed_user_id):
        raise validation_error('不可刪除目前登入中的使用者')

    target_user = db.query(User).filter(User.id == parsed_user_id).first()
    if target_user is None:
        raise not_found_error('User', user_id)

    db.query(UserRoleBinding).filter(UserRoleBinding.user_id == parsed_user_id).delete()
    db.query(UserGroupBinding).filter(UserGroupBinding.user_id == parsed_user_id).delete()
    db.query(ChatAttachment).filter(ChatAttachment.user_id == parsed_user_id).delete()
    db.delete(target_user)
    db.commit()

    return {'ok': True, 'user_id': str(parsed_user_id)}
