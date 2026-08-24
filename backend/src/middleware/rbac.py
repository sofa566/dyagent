from functools import wraps
from typing import Callable

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models import User
from src.core.logging import get_logger
from src.services.access_control_service import LEGACY_ROLE_PERMISSIONS, access_control_service

logger = get_logger(__name__)
def _resolve_legacy_role_code(user: User) -> str:
    role_code = str(getattr(user, 'role', '') or 'user').strip()
    if role_code in LEGACY_ROLE_PERMISSIONS:
        return role_code
    return 'user'


def _extract_session(*args, **kwargs) -> Session | None:
    db = kwargs.get('db')
    if isinstance(db, Session):
        return db
    for arg in args:
        if isinstance(arg, Session):
            return arg
    return None


def check_permission(user: User, permission: str, db: Session | None = None) -> bool:
    # 目的：在授權判斷時優先採用動態權限，必要時回退舊角色模型。
    # 為什麼：新舊授權模型需共存一段時間，不能中斷既有流程。
    has_permission = False
    if db is not None:
        try:
            has_permission = access_control_service.has_permission(db, user, permission)
        except Exception as error:
            logger.warning('dynamic_permission_check_failed', error=str(error), user_id=str(user.id))

    if not has_permission:
        role_code = _resolve_legacy_role_code(user)
        permissions = LEGACY_ROLE_PERMISSIONS.get(role_code, set())
        has_permission = permission in permissions

    if not has_permission:
        logger.warning(
            'permission_denied',
            user_id=str(user.id),
            role=str(getattr(user, 'role', '') or 'user'),
            permission=permission,
        )

    return has_permission


def require_permission(permission: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            user: User | None = kwargs.get('current_user')
            if user is None:
                for arg in args:
                    if isinstance(arg, User):
                        user = arg
                        break

            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail='Not authenticated',
                )

            db = _extract_session(*args, **kwargs)
            if not check_permission(user, permission, db=db):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f'Permission denied: {permission}',
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator
