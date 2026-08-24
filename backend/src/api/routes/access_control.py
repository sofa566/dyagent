import uuid
import re
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error, not_found_error, validation_error
from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import (
    AccessAuditLog,
    AccessGroup,
    AccessPermission,
    AccessRole,
    AccessRolePermission,
    GroupPermissionBinding,
    GroupRoleBinding,
    User,
    UserGroupBinding,
    UserRoleBinding,
)
from src.services.access_control_service import access_control_service

router = APIRouter()


LEGACY_PERMISSION_KEYS = [
    'dashboard.read',
    'logs.read',
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
    'role.create',
    'role.read',
    'role.update',
    'role.delete',
    'group.create',
    'group.read',
    'group.update',
    'group.delete',
    'skills.create',
    'skills.read',
    'skills.update',
    'skills.delete',
    'mcp.create',
    'mcp.read',
    'mcp.update',
    'mcp.delete',
    'functions.create',
    'functions.read',
    'functions.update',
    'functions.delete',
    'rag.create',
    'rag.read',
    'rag.update',
    'rag.delete',
]


def _parse_uuid_or_raise(raw_id: str, resource_name: str):
    try:
        return uuid.UUID(str(raw_id))
    except ValueError:
        raise not_found_error(resource_name, raw_id)


def _normalize_group_code(raw_code: str) -> str:
    normalized = str(raw_code or '').strip().lower()
    normalized = re.sub(r'\s+', '_', normalized)
    normalized = re.sub(r'[^a-z0-9_-]', '', normalized)
    return normalized.strip('_-')


def _build_group_code(db: Session, payload: dict[str, Any]) -> str:
    # 目的：產生群組可用且唯一的 code。
    # 為什麼：UI 可僅輸入群組名稱，系統自動生成穩定代碼，避免人工作業不一致。
    raw_code = str((payload or {}).get('code') or '').strip()
    raw_name = str((payload or {}).get('name') or '').strip()
    base_code = _normalize_group_code(raw_code or raw_name)
    if not base_code:
        base_code = 'group'

    candidate_code = base_code
    suffix_counter = 2
    while db.query(AccessGroup).filter(AccessGroup.code == candidate_code).first() is not None:
        candidate_code = f'{base_code}_{suffix_counter}'
        suffix_counter += 1
    return candidate_code


def _require_user_admin_permission(db: Session, current_user: User):
    if not check_permission(current_user, 'update_user', db=db):
        raise forbidden_error()


def _require_any_permission(db: Session, current_user: User, permission_keys: list[str]):
    normalized_permission_keys = [str(permission_key or '').strip() for permission_key in permission_keys if str(permission_key or '').strip()]
    if any(check_permission(current_user, permission_key, db=db) for permission_key in normalized_permission_keys):
        return
    raise forbidden_error()


def _require_role_read_permission(db: Session, current_user: User):
    _require_any_permission(db, current_user, ['role.read', 'update_user'])


def _require_role_create_permission(db: Session, current_user: User):
    _require_any_permission(db, current_user, ['role.create', 'update_user'])


def _require_role_update_permission(db: Session, current_user: User):
    _require_any_permission(db, current_user, ['role.update', 'update_user'])


def _require_role_delete_permission(db: Session, current_user: User):
    _require_any_permission(db, current_user, ['role.delete', 'update_user'])


def _require_group_read_permission(db: Session, current_user: User):
    _require_any_permission(db, current_user, ['group.read', 'update_user'])


def _require_group_create_permission(db: Session, current_user: User):
    _require_any_permission(db, current_user, ['group.create', 'update_user'])


def _require_group_update_permission(db: Session, current_user: User):
    _require_any_permission(db, current_user, ['group.update', 'update_user'])


def _require_group_delete_permission(db: Session, current_user: User):
    _require_any_permission(db, current_user, ['group.delete', 'update_user'])


def _write_access_audit_log(
    db: Session,
    actor_user_id: str | None,
    action: str,
    target_type: str,
    target_id: str | None,
    details: dict[str, Any],
):
    # 目的：在角色/群組/綁定異動後留下最小稽核紀錄。
    # 為什麼：後續問題追查需要知道誰在何時改了哪些權限關係。
    audit_row = AccessAuditLog(
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
    )
    db.add(audit_row)


def _replace_role_permissions(db: Session, role_row: AccessRole, permission_keys: list[str]):
    # 目的：覆寫角色權限集合，將輸入權限鍵對齊到 binding table。
    # 為什麼：角色更新需要原子替換，避免舊權限殘留造成超發。
    permission_rows = access_control_service.ensure_permission_keys(db, permission_keys)
    db.query(AccessRolePermission).filter(AccessRolePermission.role_id == role_row.id).delete()
    for permission_row in permission_rows:
        db.add(
            AccessRolePermission(
                role_id=role_row.id,
                permission_id=permission_row.id,
            )
        )


@router.get('/me/capabilities')
async def get_my_capabilities(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    capability = access_control_service.resolve_effective_capability(db, current_user)
    return {
        'user_id': str(current_user.id),
        'roles': capability.role_codes,
        'groups': capability.group_codes,
        'permissions': capability.permissions,
        'permission_sources': capability.permission_sources,
    }


@router.get('/access/users/capabilities')
async def get_user_capabilities(
    identifier: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：查詢指定使用者的最終能力與權限來源。
    # 為什麼：管理者需要在不切換帳號的情況下檢視他人授權結果。
    _require_user_admin_permission(db, current_user)
    normalized_identifier = str(identifier or '').strip()
    if not normalized_identifier:
        raise validation_error('identifier 為必填')

    target_user = None
    try:
        parsed_user_id = uuid.UUID(normalized_identifier)
        target_user = db.query(User).filter(User.id == parsed_user_id).first()
    except ValueError:
        target_user = None

    if target_user is None:
        target_user = db.query(User).filter(User.username == normalized_identifier).first()
    if target_user is None:
        target_user = db.query(User).filter(User.email == normalized_identifier).first()
    if target_user is None:
        raise not_found_error('User', normalized_identifier)

    capability = access_control_service.resolve_effective_capability(db, target_user)
    return {
        'user_id': str(target_user.id),
        'username': target_user.username,
        'email': target_user.email,
        'enabled': bool(getattr(target_user, 'enabled', True)),
        'roles': capability.role_codes,
        'groups': capability.group_codes,
        'permissions': capability.permissions,
        'permission_sources': capability.permission_sources,
    }


@router.get('/access/roles')
async def list_access_roles(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_role_read_permission(db, current_user)
    removed_permission_count = access_control_service.prune_orphan_entity_permissions(db)
    if removed_permission_count > 0:
        db.commit()

    role_rows = db.query(AccessRole).order_by(AccessRole.code.asc()).all()
    role_ids = [row.id for row in role_rows]
    permission_bind_rows = []
    if role_ids:
        permission_bind_rows = (
            db.query(AccessRolePermission.role_id, AccessPermission.key)
            .join(AccessPermission, AccessPermission.id == AccessRolePermission.permission_id)
            .filter(AccessRolePermission.role_id.in_(role_ids))
            .all()
        )
    permission_map: dict[str, list[str]] = {}
    for bind_row in permission_bind_rows:
        permission_map.setdefault(str(bind_row.role_id), []).append(str(bind_row.key))

    return {
        'roles': [
            {
                'id': str(role_row.id),
                'code': role_row.code,
                'name': role_row.name,
                'enabled': bool(role_row.enabled),
                'is_system': bool(getattr(role_row, 'is_system', False)),
                'permissions': sorted(permission_map.get(str(role_row.id), [])),
            }
            for role_row in role_rows
        ]
    }


@router.get('/access/permissions')
async def list_access_permissions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_any_permission(
        db,
        current_user,
        [
            'role.read',
            'role.create',
            'role.update',
            'role.delete',
            'group.read',
            'group.create',
            'group.update',
            'group.delete',
            'update_user',
        ],
    )
    removed_permission_count = access_control_service.prune_orphan_entity_permissions(db)
    if removed_permission_count > 0:
        db.commit()
    permission_rows = db.query(AccessPermission).order_by(AccessPermission.key.asc()).all()
    permission_keys = [str(permission_row.key) for permission_row in permission_rows]
    merged_permission_keys = sorted(set(permission_keys + LEGACY_PERMISSION_KEYS))
    return {'permissions': merged_permission_keys}


@router.post('/access/roles')
async def create_access_role(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：建立新角色與其權限集合。
    # 為什麼：支援後台動態擴充角色，不再依賴硬編碼枚舉。
    _require_role_create_permission(db, current_user)

    role_code = str((payload or {}).get('code') or '').strip()
    role_name = str((payload or {}).get('name') or '').strip()
    permission_keys = list((payload or {}).get('permissions') or [])
    enabled = bool((payload or {}).get('enabled', True))

    if not role_code:
        raise validation_error('code 為必填')
    if not role_name:
        raise validation_error('name 為必填')

    existed_role = db.query(AccessRole).filter(AccessRole.code == role_code).first()
    if existed_role is not None:
        raise validation_error('role code 已存在')

    role_row = AccessRole(code=role_code, name=role_name, enabled=enabled)
    db.add(role_row)
    db.flush()
    _replace_role_permissions(db, role_row, permission_keys)
    _write_access_audit_log(
        db=db,
        actor_user_id=str(current_user.id),
        action='role.create',
        target_type='role',
        target_id=str(role_row.id),
        details={'code': role_code, 'permissions': permission_keys},
    )
    db.commit()
    db.refresh(role_row)

    return {'id': str(role_row.id), 'code': role_row.code, 'name': role_row.name, 'enabled': role_row.enabled}


@router.put('/access/roles/{role_id}')
async def update_access_role(
    role_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：更新角色名稱、啟用狀態與權限集合。
    # 為什麼：角色變更後需要即時反映在有效權限聯集計算。
    _require_role_update_permission(db, current_user)
    parsed_role_id = _parse_uuid_or_raise(role_id, 'Role')
    role_row = db.query(AccessRole).filter(AccessRole.id == parsed_role_id).first()
    if role_row is None:
        raise not_found_error('Role', role_id)
    if bool(getattr(role_row, 'is_system', False)):
        raise validation_error('系統角色不可修改')

    role_name = str((payload or {}).get('name') or role_row.name).strip()
    role_enabled = bool((payload or {}).get('enabled', role_row.enabled))
    permission_keys = list((payload or {}).get('permissions') or [])

    if not role_name:
        raise validation_error('name 為必填')

    role_row.name = role_name
    role_row.enabled = role_enabled
    _replace_role_permissions(db, role_row, permission_keys)
    _write_access_audit_log(
        db=db,
        actor_user_id=str(current_user.id),
        action='role.update',
        target_type='role',
        target_id=str(role_row.id),
        details={'permissions': permission_keys, 'enabled': role_enabled},
    )
    db.commit()
    db.refresh(role_row)

    return {'id': str(role_row.id), 'code': role_row.code, 'name': role_row.name, 'enabled': role_row.enabled}


@router.delete('/access/roles/{role_id}')
async def delete_access_role(
    role_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：刪除非系統角色與其關聯綁定。
    # 為什麼：角色與群組同為一級管理物件，需提供完整生命週期操作。
    _require_role_delete_permission(db, current_user)
    parsed_role_id = _parse_uuid_or_raise(role_id, 'Role')
    role_row = db.query(AccessRole).filter(AccessRole.id == parsed_role_id).first()
    if role_row is None:
        raise not_found_error('Role', role_id)
    if bool(getattr(role_row, 'is_system', False)):
        raise validation_error('系統角色不可刪除')

    removed_role_code = str(role_row.code or '')
    db.query(AccessRolePermission).filter(AccessRolePermission.role_id == parsed_role_id).delete()
    db.query(UserRoleBinding).filter(UserRoleBinding.role_id == parsed_role_id).delete()
    db.query(GroupRoleBinding).filter(GroupRoleBinding.role_id == parsed_role_id).delete()
    db.delete(role_row)
    _write_access_audit_log(
        db=db,
        actor_user_id=str(current_user.id),
        action='role.delete',
        target_type='role',
        target_id=str(parsed_role_id),
        details={'code': removed_role_code},
    )
    db.commit()
    return {'ok': True, 'role_id': str(parsed_role_id)}


@router.get('/access/groups')
async def list_access_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_group_read_permission(db, current_user)
    removed_permission_count = access_control_service.prune_orphan_entity_permissions(db)
    if removed_permission_count > 0:
        db.commit()
    group_rows = db.query(AccessGroup).order_by(AccessGroup.code.asc()).all()
    group_ids = [group_row.id for group_row in group_rows]
    permission_bind_rows = []
    if group_ids:
        permission_bind_rows = (
            db.query(GroupPermissionBinding.group_id, AccessPermission.key)
            .join(AccessPermission, AccessPermission.id == GroupPermissionBinding.permission_id)
            .filter(GroupPermissionBinding.group_id.in_(group_ids))
            .all()
        )
    permission_key_map: dict[str, list[str]] = {}
    for bind_row in permission_bind_rows:
        permission_key_map.setdefault(str(bind_row.group_id), []).append(str(bind_row.key))

    return {
        'groups': [
            {
                'id': str(group_row.id),
                'code': group_row.code,
                'name': group_row.name,
                'enabled': bool(group_row.enabled),
                'permission_keys': sorted(permission_key_map.get(str(group_row.id), [])),
                'created_at': group_row.created_at.isoformat() if group_row.created_at else None,
            }
            for group_row in group_rows
        ]
    }


@router.post('/access/groups')
async def create_access_group(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_group_create_permission(db, current_user)
    group_code = _build_group_code(db, payload)
    group_name = str((payload or {}).get('name') or '').strip()
    group_enabled = bool((payload or {}).get('enabled', True))

    if not group_name:
        raise validation_error('name 為必填')

    group_row = AccessGroup(code=group_code, name=group_name, enabled=group_enabled)
    db.add(group_row)
    _write_access_audit_log(
        db=db,
        actor_user_id=str(current_user.id),
        action='group.create',
        target_type='group',
        target_id=None,
        details={'code': group_code},
    )
    db.commit()
    db.refresh(group_row)

    return {'id': str(group_row.id), 'code': group_row.code, 'name': group_row.name, 'enabled': group_row.enabled}


@router.put('/access/groups/{group_id}')
async def update_access_group(
    group_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_group_update_permission(db, current_user)
    parsed_group_id = _parse_uuid_or_raise(group_id, 'Group')
    group_row = db.query(AccessGroup).filter(AccessGroup.id == parsed_group_id).first()
    if group_row is None:
        raise not_found_error('Group', group_id)

    group_name = str((payload or {}).get('name') or group_row.name).strip()
    group_enabled = bool((payload or {}).get('enabled', group_row.enabled))
    if not group_name:
        raise validation_error('name 為必填')

    group_row.name = group_name
    group_row.enabled = group_enabled
    _write_access_audit_log(
        db=db,
        actor_user_id=str(current_user.id),
        action='group.update',
        target_type='group',
        target_id=str(group_row.id),
        details={'enabled': group_enabled, 'name': group_name},
    )
    db.commit()
    db.refresh(group_row)
    return {'id': str(group_row.id), 'code': group_row.code, 'name': group_row.name, 'enabled': bool(group_row.enabled)}


@router.delete('/access/groups/{group_id}')
async def delete_access_group(
    group_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：刪除群組與關聯綁定關係。
    # 為什麼：群組停用不足以清理過期配置時，管理者需能完整移除群組。
    _require_group_delete_permission(db, current_user)
    parsed_group_id = _parse_uuid_or_raise(group_id, 'Group')
    group_row = db.query(AccessGroup).filter(AccessGroup.id == parsed_group_id).first()
    if group_row is None:
        raise not_found_error('Group', group_id)

    db.query(UserGroupBinding).filter(UserGroupBinding.group_id == parsed_group_id).delete()
    db.query(GroupRoleBinding).filter(GroupRoleBinding.group_id == parsed_group_id).delete()
    db.query(GroupPermissionBinding).filter(GroupPermissionBinding.group_id == parsed_group_id).delete()

    removed_group_code = str(group_row.code or '')
    db.delete(group_row)
    _write_access_audit_log(
        db=db,
        actor_user_id=str(current_user.id),
        action='group.delete',
        target_type='group',
        target_id=str(parsed_group_id),
        details={'code': removed_group_code},
    )
    db.commit()
    return {'ok': True, 'group_id': str(parsed_group_id)}


@router.post('/access/users/{user_id}/roles')
async def bind_user_roles(
    user_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：重設指定使用者的直掛角色集合。
    # 為什麼：B 模式要求可直接對使用者配置多個角色，並即時生效。
    _require_user_admin_permission(db, current_user)
    parsed_user_id = _parse_uuid_or_raise(user_id, 'User')
    user_row = db.query(User).filter(User.id == parsed_user_id).first()
    if user_row is None:
        raise not_found_error('User', user_id)

    role_ids = [str(role_id) for role_id in list((payload or {}).get('role_ids') or [])]
    parsed_role_ids = []
    for role_id in role_ids:
        parsed_role_ids.append(_parse_uuid_or_raise(role_id, 'Role'))

    if parsed_role_ids:
        existed_role_count = db.query(AccessRole).filter(AccessRole.id.in_(parsed_role_ids)).count()
        if existed_role_count != len(set(parsed_role_ids)):
            raise validation_error('role_ids 包含不存在的角色')

    db.query(UserRoleBinding).filter(UserRoleBinding.user_id == parsed_user_id).delete()
    for parsed_role_id in parsed_role_ids:
        db.add(UserRoleBinding(user_id=parsed_user_id, role_id=parsed_role_id))

    _write_access_audit_log(
        db=db,
        actor_user_id=str(current_user.id),
        action='user_role.replace',
        target_type='user',
        target_id=str(parsed_user_id),
        details={'role_ids': role_ids},
    )
    db.commit()
    return {'ok': True, 'user_id': str(parsed_user_id), 'role_ids': role_ids}


@router.get('/access/users/{user_id}/bindings')
async def get_user_bindings(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_user_admin_permission(db, current_user)
    parsed_user_id = _parse_uuid_or_raise(user_id, 'User')
    user_row = db.query(User).filter(User.id == parsed_user_id).first()
    if user_row is None:
        raise not_found_error('User', user_id)

    role_bind_rows = db.query(UserRoleBinding).filter(UserRoleBinding.user_id == parsed_user_id).all()
    group_bind_rows = db.query(UserGroupBinding).filter(UserGroupBinding.user_id == parsed_user_id).all()
    return {
        'user_id': str(parsed_user_id),
        'role_ids': sorted([str(bind_row.role_id) for bind_row in role_bind_rows]),
        'group_ids': sorted([str(bind_row.group_id) for bind_row in group_bind_rows]),
    }


@router.post('/access/groups/{group_id}/roles')
async def bind_group_roles(
    group_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_group_update_permission(db, current_user)
    parsed_group_id = _parse_uuid_or_raise(group_id, 'Group')
    group_row = db.query(AccessGroup).filter(AccessGroup.id == parsed_group_id).first()
    if group_row is None:
        raise not_found_error('Group', group_id)

    role_ids = [str(role_id) for role_id in list((payload or {}).get('role_ids') or [])]
    parsed_role_ids = []
    for role_id in role_ids:
        parsed_role_ids.append(_parse_uuid_or_raise(role_id, 'Role'))

    if parsed_role_ids:
        existed_role_count = db.query(AccessRole).filter(AccessRole.id.in_(parsed_role_ids)).count()
        if existed_role_count != len(set(parsed_role_ids)):
            raise validation_error('role_ids 包含不存在的角色')

    db.query(GroupRoleBinding).filter(GroupRoleBinding.group_id == parsed_group_id).delete()
    for parsed_role_id in parsed_role_ids:
        db.add(GroupRoleBinding(group_id=parsed_group_id, role_id=parsed_role_id))

    _write_access_audit_log(
        db=db,
        actor_user_id=str(current_user.id),
        action='group_role.replace',
        target_type='group',
        target_id=str(parsed_group_id),
        details={'role_ids': role_ids},
    )
    db.commit()
    return {'ok': True, 'group_id': str(parsed_group_id), 'role_ids': role_ids}


@router.get('/access/groups/{group_id}/bindings')
async def get_group_bindings(
    group_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_group_read_permission(db, current_user)
    parsed_group_id = _parse_uuid_or_raise(group_id, 'Group')
    group_row = db.query(AccessGroup).filter(AccessGroup.id == parsed_group_id).first()
    if group_row is None:
        raise not_found_error('Group', group_id)

    role_bind_rows = db.query(GroupRoleBinding).filter(GroupRoleBinding.group_id == parsed_group_id).all()
    return {
        'group_id': str(parsed_group_id),
        'role_ids': sorted([str(bind_row.role_id) for bind_row in role_bind_rows]),
    }


@router.get('/access/groups/{group_id}/permissions')
async def get_group_permissions(
    group_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：讀取指定群組的直掛權限。
    # 為什麼：群組可直接授權時，管理端需要可回填、可檢視的權限來源。
    _require_group_read_permission(db, current_user)
    parsed_group_id = _parse_uuid_or_raise(group_id, 'Group')
    group_row = db.query(AccessGroup).filter(AccessGroup.id == parsed_group_id).first()
    if group_row is None:
        raise not_found_error('Group', group_id)

    permission_rows = (
        db.query(AccessPermission.id, AccessPermission.key)
        .join(GroupPermissionBinding, GroupPermissionBinding.permission_id == AccessPermission.id)
        .filter(GroupPermissionBinding.group_id == parsed_group_id)
        .all()
    )
    return {
        'group_id': str(parsed_group_id),
        'permission_ids': sorted([str(permission_row.id) for permission_row in permission_rows]),
        'permission_keys': sorted([str(permission_row.key) for permission_row in permission_rows]),
    }


@router.post('/access/groups/{group_id}/permissions')
async def bind_group_permissions(
    group_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：重設指定群組的直掛權限集合。
    # 為什麼：群組權限需支援單次提交即最終狀態，避免殘留過期授權。
    _require_group_update_permission(db, current_user)
    parsed_group_id = _parse_uuid_or_raise(group_id, 'Group')
    group_row = db.query(AccessGroup).filter(AccessGroup.id == parsed_group_id).first()
    if group_row is None:
        raise not_found_error('Group', group_id)

    permission_ids = [str(permission_id) for permission_id in list((payload or {}).get('permission_ids') or [])]
    permission_keys = [str(permission_key).strip() for permission_key in list((payload or {}).get('permission_keys') or [])]
    permission_keys = [permission_key for permission_key in permission_keys if permission_key]

    parsed_permission_ids = []
    for permission_id in permission_ids:
        parsed_permission_ids.append(_parse_uuid_or_raise(permission_id, 'Permission'))

    if parsed_permission_ids:
        existed_permission_count = db.query(AccessPermission).filter(AccessPermission.id.in_(parsed_permission_ids)).count()
        if existed_permission_count != len(set(parsed_permission_ids)):
            raise validation_error('permission_ids 包含不存在的權限')
    elif permission_keys:
        permission_rows = access_control_service.ensure_permission_keys(db, permission_keys)
        parsed_permission_ids = [permission_row.id for permission_row in permission_rows]
    else:
        parsed_permission_ids = []

    db.query(GroupPermissionBinding).filter(GroupPermissionBinding.group_id == parsed_group_id).delete()
    for parsed_permission_id in parsed_permission_ids:
        db.add(GroupPermissionBinding(group_id=parsed_group_id, permission_id=parsed_permission_id))

    _write_access_audit_log(
        db=db,
        actor_user_id=str(current_user.id),
        action='group_permission.replace',
        target_type='group',
        target_id=str(parsed_group_id),
        details={
            'permission_ids': [str(permission_id) for permission_id in parsed_permission_ids],
            'permission_keys': sorted(set(permission_keys)),
        },
    )
    db.commit()
    return {
        'ok': True,
        'group_id': str(parsed_group_id),
        'permission_ids': [str(permission_id) for permission_id in parsed_permission_ids],
        'permission_keys': sorted(set(permission_keys)),
    }


@router.post('/access/users/{user_id}/groups')
async def bind_user_groups(
    user_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_user_admin_permission(db, current_user)
    parsed_user_id = _parse_uuid_or_raise(user_id, 'User')
    user_row = db.query(User).filter(User.id == parsed_user_id).first()
    if user_row is None:
        raise not_found_error('User', user_id)

    group_ids = [str(group_id) for group_id in list((payload or {}).get('group_ids') or [])]
    parsed_group_ids = []
    for group_id in group_ids:
        parsed_group_ids.append(_parse_uuid_or_raise(group_id, 'Group'))

    if parsed_group_ids:
        existed_group_count = db.query(AccessGroup).filter(AccessGroup.id.in_(parsed_group_ids)).count()
        if existed_group_count != len(set(parsed_group_ids)):
            raise validation_error('group_ids 包含不存在的群組')

    db.query(UserGroupBinding).filter(UserGroupBinding.user_id == parsed_user_id).delete()
    for parsed_group_id in parsed_group_ids:
        db.add(UserGroupBinding(user_id=parsed_user_id, group_id=parsed_group_id))

    _write_access_audit_log(
        db=db,
        actor_user_id=str(current_user.id),
        action='user_group.replace',
        target_type='user',
        target_id=str(parsed_user_id),
        details={'group_ids': group_ids},
    )
    db.commit()
    return {'ok': True, 'user_id': str(parsed_user_id), 'group_ids': group_ids}
