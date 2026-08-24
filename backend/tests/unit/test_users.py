import pytest


class TestUsersList:
    def test_list_users_admin(self, client, admin_user, admin_token, regular_user):
        response = client.get(
            '/api/users',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert 'users' in data
        assert len(data['users']) >= 2

    def test_list_users_regular_user_forbidden(self, client, regular_user, regular_user_token):
        response = client.get(
            '/api/users',
            headers={'Authorization': f'Bearer {regular_user_token}'},
        )
        assert response.status_code == 403


class TestUserRoleUpdate:
    def test_update_user_role_admin(self, client, admin_user, admin_token, regular_user):
        response = client.put(
            f'/api/users/{regular_user.id}/role',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={'role': 'agent_admin'},
        )
        assert response.status_code == 200
        data = response.json()
        assert data['role'] == 'agent_admin'

    def test_update_user_role_nonexistent(self, client, admin_user, admin_token):
        response = client.put(
            '/api/users/nonexistent-id/role',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={'role': 'admin'},
        )
        assert response.status_code == 404

    def test_update_user_role_regular_user_forbidden(self, client, regular_user, regular_user_token):
        response = client.put(
            f'/api/users/{regular_user.id}/role',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            params={'role': 'admin'},
        )
        assert response.status_code == 403


class TestUserProfileUpdate:
    def test_update_user_profile_admin(self, client, admin_token, regular_user):
        response = client.put(
            f'/api/users/{regular_user.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'username': 'updated_regular_user',
                'email': 'updated_regular_user@test.com',
                'enabled': False,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload['username'] == 'updated_regular_user'
        assert payload['email'] == 'updated_regular_user@test.com'
        assert payload['enabled'] is False

    def test_update_user_profile_regular_user_forbidden(self, client, regular_user_token, admin_user):
        response = client.put(
            f'/api/users/{admin_user.id}',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            json={
                'username': 'admin_update_failed',
                'email': 'admin_update_failed@test.com',
                'enabled': True,
            },
        )
        assert response.status_code == 403


class TestUserDelete:
    def test_delete_user_admin(self, client, admin_token, regular_user):
        response = client.delete(
            f'/api/users/{regular_user.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload['ok'] is True
        assert payload['user_id'] == str(regular_user.id)

    def test_delete_user_regular_user_forbidden(self, client, regular_user_token, admin_user):
        response = client.delete(
            f'/api/users/{admin_user.id}',
            headers={'Authorization': f'Bearer {regular_user_token}'},
        )
        assert response.status_code == 403

    def test_delete_current_user_rejected(self, client, admin_token, admin_user):
        response = client.delete(
            f'/api/users/{admin_user.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 400
