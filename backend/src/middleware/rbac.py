from enum import Enum
from functools import wraps
from typing import Callable

from fastapi import HTTPException, status

from src.models import User
from src.core.logging import get_logger

logger = get_logger(__name__)


class Role(str, Enum):
    ADMIN = 'admin'
    AGENT_ADMIN = 'agent_admin'
    USER = 'user'


ROLE_PERMISSIONS = {
    Role.ADMIN: {
        'create_agent',
        'read_agent',
        'update_agent',
        'delete_agent',
        'create_user',
        'read_user',
        'update_user',
        'delete_user',
        'chat',
        'read_logs',
    },
    Role.AGENT_ADMIN: {
        'read_agent',
        'update_agent',
        'chat',
    },
    Role.USER: {
        'chat',
    },
}


def check_permission(user: User, permission: str) -> bool:
    role = Role(user.role)
    permissions = ROLE_PERMISSIONS.get(role, set())
    has_permission = permission in permissions

    if not has_permission:
        logger.warning(
            'permission_denied',
            user_id=str(user.id),
            role=user.role,
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

            if not check_permission(user, permission):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f'Permission denied: {permission}',
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def require_role(allowed_roles: list[Role]) -> Callable:
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

            if Role(user.role) not in allowed_roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail='Role not authorized',
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator
