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
