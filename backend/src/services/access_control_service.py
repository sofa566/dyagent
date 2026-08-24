from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from src.core.logging import get_logger
from src.models import (
    AccessGroup,
    AccessPermission,
    AccessRole,
    AccessRolePermission,
    Agent,
    GroupPermissionBinding,
    GroupRoleBinding,
    RagDataset,
    User,
    UserGroupBinding,
    UserRoleBinding,
)


logger = get_logger(__name__)


LEGACY_ROLE_PERMISSIONS = {
    'admin': {
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
    },
    'agent_admin': {
        'read_agent',
        'update_agent',
        'chat',
    },
    'user': {
        'chat',
    },
}


SYSTEM_ROLE_DEFAULTS = {
    'user': {
        'name': '一般使用者',
        'permissions': {'chat'},
    },
    'agent_admin': {
        'name': '代理者管理者',
        'permissions': set(LEGACY_ROLE_PERMISSIONS.get('agent_admin', set())),
    },
    'admin': {
        'name': '系統管理者',
        'permissions': None,
    },
}

ROLE_PRIORITY = ('admin', 'agent_admin', 'user')


@dataclass
class EffectiveCapability:
    # 目的：封裝登入者最終可用能力，供 API 回應與授權判斷共用。
    # 為什麼：統一回傳格式可降低不同呼叫端重複組裝資料的風險。
    role_codes: list[str]
    group_codes: list[str]
    permissions: list[str]
    permission_sources: dict[str, list[str]] = field(default_factory=dict)


class AccessControlService:
    # 目的：集中處理動態權限計算與回退策略。
    # 為什麼：避免權限邏輯分散在 middleware 與 routes，提升可維護性。

    def resolve_effective_capability(self, db: Session, user: User) -> EffectiveCapability:
        # 目的：計算使用者最終角色、群組與權限聯集。
        # 為什麼：B 模式同時有直掛角色與群組繼承，需由單一流程統一求聯集。
        try:
            direct_role_rows = (
                db.query(AccessRole.code)
                .join(UserRoleBinding, UserRoleBinding.role_id == AccessRole.id)
                .filter(UserRoleBinding.user_id == user.id, AccessRole.enabled == True)  # noqa: E712
                .all()
            )
            group_rows = (
                db.query(AccessGroup.id, AccessGroup.code)
                .join(UserGroupBinding, UserGroupBinding.group_id == AccessGroup.id)
                .filter(UserGroupBinding.user_id == user.id, AccessGroup.enabled == True)  # noqa: E712
                .all()
            )
            group_ids = [row.id for row in group_rows]
            group_role_rows = []
            if group_ids:
                group_role_rows = (
                    db.query(AccessRole.code)
                    .join(GroupRoleBinding, GroupRoleBinding.role_id == AccessRole.id)
                    .filter(GroupRoleBinding.group_id.in_(group_ids), AccessRole.enabled == True)  # noqa: E712
                    .all()
                )

            dynamic_role_codes = sorted({str(row.code) for row in direct_role_rows + group_role_rows})
            dynamic_group_codes = sorted({str(row.code) for row in group_rows})
            dynamic_role_permission_sources = self._load_permission_sources_by_role_codes(db, dynamic_role_codes)
            dynamic_group_permission_sources = self._load_permission_sources_by_group_ids(db, group_ids)
            dynamic_permission_sources = self._merge_permission_sources(
                dynamic_role_permission_sources,
                dynamic_group_permission_sources,
            )
            dynamic_permission_keys = set(dynamic_permission_sources.keys())
        except (ProgrammingError, OperationalError) as error:
            logger.warning('access_control.schema_not_ready_fallback_legacy', error=str(error), user_id=str(user.id))
            return self._build_legacy_capability(user=user, group_codes=[])

        if dynamic_permission_keys:
            return EffectiveCapability(
                role_codes=dynamic_role_codes,
                group_codes=dynamic_group_codes,
                permissions=sorted(dynamic_permission_keys),
                permission_sources={
                    permission_key: sorted(source_values)
                    for permission_key, source_values in dynamic_permission_sources.items()
                },
            )

        return self._build_legacy_capability(user=user, group_codes=dynamic_group_codes)

    def ensure_system_roles(self, db: Session) -> None:
        # 目的：確保系統保留角色固定存在且不可被誤改權限。
        # 為什麼：啟動後授權模型必須有穩定基線，避免缺角色導致全站授權異常。
        try:
            existing_roles = db.query(AccessRole).filter(AccessRole.code.in_(list(SYSTEM_ROLE_DEFAULTS.keys()))).all()
        except (ProgrammingError, OperationalError):
            return

        role_by_code = {str(role_row.code): role_row for role_row in existing_roles}
        has_changes = False

        for role_code, role_config in SYSTEM_ROLE_DEFAULTS.items():
            role_row = role_by_code.get(role_code)
            if role_row is None:
                role_row = AccessRole(
                    code=role_code,
                    name=str(role_config.get('name') or role_code),
                    enabled=True,
                    is_system=True,
                )
                db.add(role_row)
                db.flush()
                has_changes = True
                role_by_code[role_code] = role_row
            else:
                if not bool(getattr(role_row, 'is_system', False)):
                    role_row.is_system = True
                    has_changes = True
                if not bool(getattr(role_row, 'enabled', True)):
                    role_row.enabled = True
                    has_changes = True

        admin_permissions = self._load_all_permission_keys_for_admin(db)
        user_permissions = set(SYSTEM_ROLE_DEFAULTS['user']['permissions'])
        agent_admin_permissions = set(SYSTEM_ROLE_DEFAULTS['agent_admin']['permissions'])

        desired_permissions_by_code = {
            'user': user_permissions,
            'agent_admin': agent_admin_permissions,
            'admin': admin_permissions,
        }

        for role_code, permission_keys in desired_permissions_by_code.items():
            role_row = role_by_code.get(role_code)
            if role_row is None:
                continue
            permission_rows = self.ensure_permission_keys(db, sorted(permission_keys))
            permission_ids = {permission_row.id for permission_row in permission_rows}
            bind_rows = db.query(AccessRolePermission).filter(AccessRolePermission.role_id == role_row.id).all()
            bind_permission_ids = {bind_row.permission_id for bind_row in bind_rows}

            missing_ids = permission_ids - bind_permission_ids
            if missing_ids:
                for permission_id in missing_ids:
                    db.add(AccessRolePermission(role_id=role_row.id, permission_id=permission_id))
                has_changes = True

            removable_rows = [bind_row for bind_row in bind_rows if bind_row.permission_id not in permission_ids]
            if removable_rows:
                for removable_row in removable_rows:
                    db.delete(removable_row)
                has_changes = True

        users_without_role_bindings = (
            db.query(User)
            .outerjoin(UserRoleBinding, UserRoleBinding.user_id == User.id)
            .filter(UserRoleBinding.id == None)  # noqa: E711
            .all()
        )
        for user_row in users_without_role_bindings:
            target_role_code = str(getattr(user_row, 'role', '') or 'user')
            target_role_row = role_by_code.get(target_role_code) or role_by_code.get('user')
            if target_role_row is None:
                continue
            db.add(UserRoleBinding(user_id=user_row.id, role_id=target_role_row.id))
            has_changes = True

        if has_changes:
            db.commit()

    def sync_user_system_role_binding(self, db: Session, user: User, role_code: str) -> None:
        # 目的：同步單一使用者的系統角色綁定至 access control 模型。
        # 為什麼：舊流程仍會寫入 users.role，需即時鏡像到 user_role_bindings 才能維持授權一致。
        self.ensure_system_roles(db)
        system_roles = db.query(AccessRole).filter(AccessRole.is_system == True).all()  # noqa: E712
        system_role_ids = {role_row.id for role_row in system_roles}
        role_row = db.query(AccessRole).filter(AccessRole.code == role_code, AccessRole.is_system == True).first()  # noqa: E712
        if role_row is None:
            role_row = db.query(AccessRole).filter(AccessRole.code == 'user', AccessRole.is_system == True).first()  # noqa: E712
        if role_row is None:
            return

        db.query(UserRoleBinding).filter(
            UserRoleBinding.user_id == user.id,
            UserRoleBinding.role_id.in_(list(system_role_ids)),
        ).delete(synchronize_session=False)
        db.flush()

        existed_target_binding = db.query(UserRoleBinding).filter(
            UserRoleBinding.user_id == user.id,
            UserRoleBinding.role_id == role_row.id,
        ).first()
        if existed_target_binding is not None:
            return
        db.add(UserRoleBinding(user_id=user.id, role_id=role_row.id))

    def has_permission(self, db: Session, user: User, permission_key: str) -> bool:
        # 目的：檢查使用者是否擁有指定權限。
        # 為什麼：提供 middleware 與 API route 共用的單一授權判斷入口。
        effective_capability = self.resolve_effective_capability(db, user)
        return str(permission_key or '') in set(effective_capability.permissions)

    def resolve_primary_role_code(self, db: Session, user: User) -> str:
        # 目的：回傳使用者對外顯示用的主要角色代碼。
        # 為什麼：在淘汰 users.role 後，API 仍需提供穩定角色欄位給前端與相容客戶端。
        effective_capability = self.resolve_effective_capability(db, user)
        role_codes = set(effective_capability.role_codes)
        for role_code in ROLE_PRIORITY:
            if role_code in role_codes:
                return role_code
        return 'user'

    def ensure_permission_keys(self, db: Session, permission_keys: list[str]) -> list[AccessPermission]:
        # 目的：確保指定權限鍵都存在於 access_permissions。
        # 為什麼：角色管理 API 允許動態權限鍵，需先 upsert 後再建立對應關係。
        normalized_keys = sorted({str(key or '').strip() for key in permission_keys if str(key or '').strip()})
        if not normalized_keys:
            return []

        existed_rows = db.query(AccessPermission).filter(AccessPermission.key.in_(normalized_keys)).all()
        existed_by_key = {row.key: row for row in existed_rows}
        created_rows: list[AccessPermission] = []
        for permission_key in normalized_keys:
            if permission_key in existed_by_key:
                continue
            created_row = AccessPermission(key=permission_key)
            db.add(created_row)
            created_rows.append(created_row)

        if created_rows:
            db.flush()

        return db.query(AccessPermission).filter(AccessPermission.key.in_(normalized_keys)).all()

    def remove_permission_keys(self, db: Session, permission_keys: list[str]) -> int:
        # 目的：移除指定權限鍵與其角色/群組綁定。
        # 為什麼：實體資源刪除後需同步清理 execute 權限，避免殘留無效授權項目。
        normalized_keys = sorted({str(key or '').strip() for key in permission_keys if str(key or '').strip()})
        if not normalized_keys:
            return 0
        permission_rows = db.query(AccessPermission).filter(AccessPermission.key.in_(normalized_keys)).all()
        if not permission_rows:
            return 0
        permission_ids = [row.id for row in permission_rows]
        db.query(AccessRolePermission).filter(AccessRolePermission.permission_id.in_(permission_ids)).delete(synchronize_session=False)
        db.query(GroupPermissionBinding).filter(GroupPermissionBinding.permission_id.in_(permission_ids)).delete(synchronize_session=False)
        db.query(AccessPermission).filter(AccessPermission.id.in_(permission_ids)).delete(synchronize_session=False)
        db.flush()
        return len(permission_rows)

    def prune_orphan_entity_permissions(self, db: Session) -> int:
        # 目的：清除指向不存在實體的 entity 權限鍵。
        # 為什麼：資源刪除前曾被授權時會遺留鍵值，需定期自動清理避免 UI 顯示「已刪除實體」。
        entity_permission_rows = db.query(AccessPermission).filter(AccessPermission.key.like('entity.%.execute')).all()
        if not entity_permission_rows:
            return 0

        existing_dataset_ids = {str(row.id) for row in db.query(RagDataset.id).all()}
        existing_agent_ids = {str(row.id) for row in db.query(Agent.id).all()}
        removable_permission_keys: list[str] = []
        for permission_row in entity_permission_rows:
            key = str(permission_row.key or '').strip()
            key_parts = key.split('.')
            if len(key_parts) != 4:
                continue
            if key_parts[0] != 'entity' or key_parts[3] != 'execute':
                continue
            entity_type = str(key_parts[1])
            entity_identifier = str(key_parts[2])
            try:
                uuid.UUID(entity_identifier)
            except Exception:
                continue
            if entity_type == 'dataset' and entity_identifier not in existing_dataset_ids:
                removable_permission_keys.append(key)
            if entity_type == 'agent' and entity_identifier not in existing_agent_ids:
                removable_permission_keys.append(key)
        return self.remove_permission_keys(db, removable_permission_keys)

    def _load_permission_keys_by_role_codes(self, db: Session, role_codes: list[str]) -> set[str]:
        if not role_codes:
            return set()
        permission_rows = (
            db.query(AccessPermission.key)
            .join(AccessRolePermission, AccessRolePermission.permission_id == AccessPermission.id)
            .join(AccessRole, AccessRole.id == AccessRolePermission.role_id)
            .filter(AccessRole.code.in_(role_codes), AccessRole.enabled == True)  # noqa: E712
            .all()
        )
        return {str(row.key) for row in permission_rows}

    def _load_permission_keys_by_group_ids(self, db: Session, group_ids: list[str]) -> set[str]:
        if not group_ids:
            return set()
        permission_rows = (
            db.query(AccessPermission.key)
            .join(GroupPermissionBinding, GroupPermissionBinding.permission_id == AccessPermission.id)
            .filter(GroupPermissionBinding.group_id.in_(group_ids))
            .all()
        )
        return {str(row.key) for row in permission_rows}

    def _load_permission_sources_by_role_codes(self, db: Session, role_codes: list[str]) -> dict[str, set[str]]:
        if not role_codes:
            return {}
        source_rows = (
            db.query(AccessPermission.key, AccessRole.code)
            .join(AccessRolePermission, AccessRolePermission.permission_id == AccessPermission.id)
            .join(AccessRole, AccessRole.id == AccessRolePermission.role_id)
            .filter(AccessRole.code.in_(role_codes), AccessRole.enabled == True)  # noqa: E712
            .all()
        )
        source_map: dict[str, set[str]] = {}
        for source_row in source_rows:
            permission_key = str(source_row.key)
            source_map.setdefault(permission_key, set()).add(f'role:{str(source_row.code)}')
        return source_map

    def _load_permission_sources_by_group_ids(self, db: Session, group_ids: list[str]) -> dict[str, set[str]]:
        if not group_ids:
            return {}
        source_rows = (
            db.query(AccessPermission.key, AccessGroup.code)
            .join(GroupPermissionBinding, GroupPermissionBinding.permission_id == AccessPermission.id)
            .join(AccessGroup, AccessGroup.id == GroupPermissionBinding.group_id)
            .filter(GroupPermissionBinding.group_id.in_(group_ids), AccessGroup.enabled == True)  # noqa: E712
            .all()
        )
        source_map: dict[str, set[str]] = {}
        for source_row in source_rows:
            permission_key = str(source_row.key)
            source_map.setdefault(permission_key, set()).add(f'group:{str(source_row.code)}')
        return source_map

    def _merge_permission_sources(self, *source_maps: dict[str, set[str]]) -> dict[str, set[str]]:
        merged_sources: dict[str, set[str]] = {}
        for source_map in source_maps:
            for permission_key, source_values in source_map.items():
                merged_sources.setdefault(permission_key, set()).update(set(source_values))
        return merged_sources

    def _build_legacy_capability(self, *, user: User, group_codes: list[str]) -> EffectiveCapability:
        legacy_role = str(getattr(user, 'role', '') or 'user')
        if legacy_role not in LEGACY_ROLE_PERMISSIONS:
            legacy_role = 'user'
        legacy_permissions = set(LEGACY_ROLE_PERMISSIONS.get(legacy_role, set()))
        return EffectiveCapability(
            role_codes=[legacy_role],
            group_codes=group_codes,
            permissions=sorted(legacy_permissions),
            permission_sources={
                permission_key: [f'legacy:{legacy_role}']
                for permission_key in sorted(legacy_permissions)
            },
        )

    def _load_all_permission_keys_for_admin(self, db: Session) -> set[str]:
        try:
            permission_rows = db.query(AccessPermission.key).all()
        except (ProgrammingError, OperationalError):
            permission_rows = []
        permission_keys = {str(permission_row.key) for permission_row in permission_rows if str(permission_row.key or '').strip()}
        permission_keys.update(set(LEGACY_ROLE_PERMISSIONS.get('admin', set())))
        permission_keys.update(set(LEGACY_ROLE_PERMISSIONS.get('agent_admin', set())))
        permission_keys.update(set(LEGACY_ROLE_PERMISSIONS.get('user', set())))
        return permission_keys


access_control_service = AccessControlService()
