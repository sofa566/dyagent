import uuid

from src.models import (
    AccessGroup,
    AccessPermission,
    AccessRole,
    AccessRolePermission,
    GroupPermissionBinding,
    GroupRoleBinding,
    UserGroupBinding,
    UserRoleBinding,
)


class TestAccessControlCapabilities:
    def test_me_capabilities_returns_legacy_permissions_when_no_dynamic_bindings(
        self,
        client,
        regular_user_token,
    ):
        response = client.get(
            '/api/me/capabilities',
            headers={'Authorization': f'Bearer {regular_user_token}'},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload['roles'] == ['user']
        assert 'chat' in payload['permissions']
        assert 'permission_sources' in payload
        assert 'chat' in payload['permission_sources']

    def test_me_capabilities_includes_union_of_direct_and_group_role_permissions(
        self,
        client,
        db,
        regular_user,
        regular_user_token,
    ):
        direct_role = AccessRole(code='report_viewer', name='Report Viewer', enabled=True)
        group_role = AccessRole(code='agent_editor', name='Agent Editor', enabled=True)
        permission_reports = AccessPermission(key='reports.read')
        permission_agents = AccessPermission(key='agents.update')
        group_row = AccessGroup(code='ops_group', name='Ops Group', enabled=True)
        db.add_all([direct_role, group_role, permission_reports, permission_agents, group_row])
        db.flush()

        db.add_all(
            [
                AccessRolePermission(role_id=direct_role.id, permission_id=permission_reports.id),
                AccessRolePermission(role_id=group_role.id, permission_id=permission_agents.id),
                UserRoleBinding(user_id=regular_user.id, role_id=direct_role.id),
                UserGroupBinding(user_id=regular_user.id, group_id=group_row.id),
                GroupRoleBinding(group_id=group_row.id, role_id=group_role.id),
            ]
        )
        db.commit()

        response = client.get(
            '/api/me/capabilities',
            headers={'Authorization': f'Bearer {regular_user_token}'},
        )

        assert response.status_code == 200
        payload = response.json()
        assert set(payload['roles']) == {'report_viewer', 'agent_editor'}
        assert set(payload['groups']) == {'ops_group'}
        assert set(payload['permissions']) == {'reports.read', 'agents.update'}
        assert set(payload['permission_sources']['reports.read']) == {'role:report_viewer'}
        assert set(payload['permission_sources']['agents.update']) == {'role:agent_editor'}

    def test_me_capabilities_includes_group_direct_permissions(
        self,
        client,
        db,
        regular_user,
        regular_user_token,
    ):
        target_group = AccessGroup(code='mis_group', name='MIS Group', enabled=True)
        group_permission = AccessPermission(key='inventory.export')
        db.add_all([target_group, group_permission])
        db.flush()

        db.add(UserGroupBinding(user_id=regular_user.id, group_id=target_group.id))
        db.add(GroupPermissionBinding(group_id=target_group.id, permission_id=group_permission.id))
        db.commit()

        response = client.get(
            '/api/me/capabilities',
            headers={'Authorization': f'Bearer {regular_user_token}'},
        )

        assert response.status_code == 200
        payload = response.json()
        assert set(payload['groups']) == {'mis_group'}
        assert 'inventory.export' in set(payload['permissions'])
        assert set(payload['permission_sources']['inventory.export']) == {'group:mis_group'}

    def test_me_capabilities_permission_sources_merge_role_and_group(
        self,
        client,
        db,
        regular_user,
        regular_user_token,
    ):
        target_role = AccessRole(code='approver_role', name='Approver Role', enabled=True)
        target_group = AccessGroup(code='approver_group', name='Approver Group', enabled=True)
        target_permission = AccessPermission(key='approval.submit')
        db.add_all([target_role, target_group, target_permission])
        db.flush()

        db.add_all(
            [
                AccessRolePermission(role_id=target_role.id, permission_id=target_permission.id),
                UserRoleBinding(user_id=regular_user.id, role_id=target_role.id),
                UserGroupBinding(user_id=regular_user.id, group_id=target_group.id),
                GroupPermissionBinding(group_id=target_group.id, permission_id=target_permission.id),
            ]
        )
        db.commit()

        response = client.get(
            '/api/me/capabilities',
            headers={'Authorization': f'Bearer {regular_user_token}'},
        )

        assert response.status_code == 200
        payload = response.json()
        assert 'approval.submit' in set(payload['permissions'])
        assert set(payload['permission_sources']['approval.submit']) == {
            'group:approver_group',
            'role:approver_role',
        }


class TestAccessControlBindingApi:
    def test_list_permissions_includes_legacy_permission_keys(
        self,
        client,
        admin_token,
    ):
        response = client.get(
            '/api/access/permissions',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 200
        payload = response.json()
        assert 'chat' in payload['permissions']
        assert 'read_user' in payload['permissions']

    def test_list_permissions_prunes_orphan_dataset_entity_permissions(self, client, db, admin_token):
        orphan_permission = AccessPermission(key=f'entity.dataset.{uuid.uuid4()}.execute')
        db.add(orphan_permission)
        db.commit()
        orphan_permission_key = orphan_permission.key
        orphan_permission_id = orphan_permission.id

        response = client.get(
            '/api/access/permissions',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 200
        payload = response.json()
        assert orphan_permission_key not in set(payload['permissions'])
        assert db.query(AccessPermission).filter(AccessPermission.id == orphan_permission_id).first() is None

    def test_bind_user_roles_replaces_existing_bindings(
        self,
        client,
        db,
        admin_token,
        regular_user,
    ):
        # 目的：驗證替換綁定 API 會覆蓋舊 user-role 關係。
        # 為什麼：角色管理需支援「單次提交即成為最終狀態」，避免殘留過期權限。
        first_role = AccessRole(code='role_a', name='Role A', enabled=True)
        second_role = AccessRole(code='role_b', name='Role B', enabled=True)
        db.add_all([first_role, second_role])
        db.commit()

        first_response = client.post(
            f'/api/access/users/{regular_user.id}/roles',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'role_ids': [str(first_role.id)]},
        )
        assert first_response.status_code == 200

        second_response = client.post(
            f'/api/access/users/{regular_user.id}/roles',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'role_ids': [str(second_role.id)]},
        )
        assert second_response.status_code == 200

        binding_rows = db.query(UserRoleBinding).filter(UserRoleBinding.user_id == regular_user.id).all()
        assert len(binding_rows) == 1
        assert str(binding_rows[0].role_id) == str(second_role.id)

    def test_bind_user_roles_rejects_invalid_role_id(
        self,
        client,
        admin_token,
        regular_user,
    ):
        random_role_id = str(uuid.uuid4())
        response = client.post(
            f'/api/access/users/{regular_user.id}/roles',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'role_ids': [random_role_id]},
        )

        assert response.status_code == 400

    def test_bind_group_permissions_replaces_existing_bindings(
        self,
        client,
        db,
        admin_token,
    ):
        target_group = AccessGroup(code='ops_permission_group', name='Ops Permission Group', enabled=True)
        first_permission = AccessPermission(key='ops.export')
        second_permission = AccessPermission(key='ops.approve')
        db.add_all([target_group, first_permission, second_permission])
        db.commit()

        first_response = client.post(
            f'/api/access/groups/{target_group.id}/permissions',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'permission_ids': [str(first_permission.id)]},
        )
        assert first_response.status_code == 200

        second_response = client.post(
            f'/api/access/groups/{target_group.id}/permissions',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'permission_ids': [str(second_permission.id)]},
        )
        assert second_response.status_code == 200

        binding_rows = db.query(GroupPermissionBinding).filter(GroupPermissionBinding.group_id == target_group.id).all()
        assert len(binding_rows) == 1
        assert str(binding_rows[0].permission_id) == str(second_permission.id)

    def test_bind_group_permissions_rejects_invalid_permission_id(
        self,
        client,
        db,
        admin_token,
    ):
        target_group = AccessGroup(code='ops_invalid_permission_group', name='Ops Invalid Permission Group', enabled=True)
        db.add(target_group)
        db.commit()

        random_permission_id = str(uuid.uuid4())
        response = client.post(
            f'/api/access/groups/{target_group.id}/permissions',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'permission_ids': [random_permission_id]},
        )
        assert response.status_code == 400

    def test_get_group_permissions_returns_bound_permission_ids(
        self,
        client,
        db,
        admin_token,
    ):
        target_group = AccessGroup(code='ops_read_permission_group', name='Ops Read Permission Group', enabled=True)
        target_permission = AccessPermission(key='ops.read')
        db.add_all([target_group, target_permission])
        db.flush()
        db.add(GroupPermissionBinding(group_id=target_group.id, permission_id=target_permission.id))
        db.commit()

        response = client.get(
            f'/api/access/groups/{target_group.id}/permissions',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload['group_id'] == str(target_group.id)
        assert payload['permission_ids'] == [str(target_permission.id)]
        assert payload['permission_keys'] == ['ops.read']

    def test_bind_group_permissions_accepts_permission_keys(self, client, db, admin_token):
        target_group = AccessGroup(code='ops_key_group', name='Ops Key Group', enabled=True)
        db.add(target_group)
        db.commit()

        response = client.post(
            f'/api/access/groups/{target_group.id}/permissions',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'permission_keys': ['report.download']},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload['permission_keys'] == ['report.download']

        reloaded = client.get(
            f'/api/access/groups/{target_group.id}/permissions',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert reloaded.status_code == 200
        reloaded_payload = reloaded.json()
        assert 'report.download' in set(reloaded_payload['permission_keys'])

    def test_get_user_capabilities_by_username(self, client, admin_token, regular_user):
        response = client.get(
            '/api/access/users/capabilities',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={'identifier': regular_user.username},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload['username'] == regular_user.username
        assert payload['user_id'] == str(regular_user.id)
        assert 'permissions' in payload

    def test_get_user_capabilities_forbidden_for_regular_user(
        self,
        client,
        regular_user_token,
        regular_user,
    ):
        response = client.get(
            '/api/access/users/capabilities',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            params={'identifier': regular_user.username},
        )
        assert response.status_code == 403


class TestAccessRoleSystemGuard:
    def test_update_system_role_is_rejected(self, client, db, admin_token):
        system_role = AccessRole(code='fixed_system_role', name='系統保留角色', enabled=True, is_system=True)
        db.add(system_role)
        db.commit()

        response = client.put(
            f'/api/access/roles/{system_role.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'name': '不可修改', 'enabled': False, 'permissions': ['chat']},
        )

        assert response.status_code == 400

    def test_delete_system_role_is_rejected(self, client, db, admin_token):
        system_role = AccessRole(code='fixed_system_role_delete', name='系統保留角色刪除', enabled=True, is_system=True)
        db.add(system_role)
        db.commit()

        response = client.delete(
            f'/api/access/roles/{system_role.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 400


class TestAccessRoleDeleteApi:
    def test_delete_custom_role_removes_related_bindings(self, client, db, admin_token, regular_user):
        role_row = AccessRole(code='deletable_role', name='Deletable Role', enabled=True)
        group_row = AccessGroup(code='deletable_role_group', name='Deletable Role Group', enabled=True)
        permission_row = AccessPermission(key='deletable.permission')
        db.add_all([role_row, group_row, permission_row])
        db.flush()
        db.add(AccessRolePermission(role_id=role_row.id, permission_id=permission_row.id))
        db.add(UserRoleBinding(user_id=regular_user.id, role_id=role_row.id))
        db.add(GroupRoleBinding(group_id=group_row.id, role_id=role_row.id))
        db.commit()

        response = client.delete(
            f'/api/access/roles/{role_row.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 200
        assert db.query(AccessRole).filter(AccessRole.id == role_row.id).first() is None
        assert db.query(AccessRolePermission).filter(AccessRolePermission.role_id == role_row.id).count() == 0
        assert db.query(UserRoleBinding).filter(UserRoleBinding.role_id == role_row.id).count() == 0
        assert db.query(GroupRoleBinding).filter(GroupRoleBinding.role_id == role_row.id).count() == 0


class TestAccessGroupCrudApi:
    def test_create_group_generates_code_when_missing(self, client, db, admin_token):
        response = client.post(
            '/api/access/groups',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'name': '營運團隊', 'enabled': True},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload['code'] == 'group'

    def test_update_group_name_and_enabled(self, client, db, admin_token):
        group_row = AccessGroup(code='ops_editable', name='Ops Editable', enabled=True)
        db.add(group_row)
        db.commit()

        response = client.put(
            f'/api/access/groups/{group_row.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'name': 'Ops Updated', 'enabled': False},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload['name'] == 'Ops Updated'
        assert payload['enabled'] is False

    def test_delete_group_removes_group_and_related_bindings(self, client, db, admin_token, regular_user):
        role_row = AccessRole(code='ops_role', name='Ops Role', enabled=True)
        group_row = AccessGroup(code='ops_drop', name='Ops Drop', enabled=True)
        permission_row = AccessPermission(key='ops.drop.permission')
        db.add_all([role_row, group_row])
        db.flush()
        db.add(permission_row)
        db.flush()
        db.add(UserGroupBinding(user_id=regular_user.id, group_id=group_row.id))
        db.add(GroupRoleBinding(group_id=group_row.id, role_id=role_row.id))
        db.add(GroupPermissionBinding(group_id=group_row.id, permission_id=permission_row.id))
        db.commit()

        response = client.delete(
            f'/api/access/groups/{group_row.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 200
        assert db.query(AccessGroup).filter(AccessGroup.id == group_row.id).first() is None
        assert db.query(UserGroupBinding).filter(UserGroupBinding.group_id == group_row.id).count() == 0
        assert db.query(GroupRoleBinding).filter(GroupRoleBinding.group_id == group_row.id).count() == 0
        assert db.query(GroupPermissionBinding).filter(GroupPermissionBinding.group_id == group_row.id).count() == 0
