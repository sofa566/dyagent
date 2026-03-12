import pytest


class TestMCPServers:
    def test_list_mcp_servers_admin(self, client, admin_user, admin_token):
        response = client.get(
            '/api/mcp/servers',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert 'servers' in data

    def test_list_mcp_servers_regular_user_forbidden(self, client, regular_user, regular_user_token):
        response = client.get(
            '/api/mcp/servers',
            headers={'Authorization': f'Bearer {regular_user_token}'},
        )
        assert response.status_code == 403


class TestMCPConnect:
    def test_connect_mcp_admin(self, client, admin_user, admin_token):
        response = client.post(
            '/api/mcp/connect',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={
                'server_url': 'http://localhost:8080',
                'auth_token': 'test-token',
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert 'server_id' in data
        assert data['status'] == 'connected'

    def test_connect_mcp_regular_user_forbidden(self, client, regular_user, regular_user_token):
        response = client.post(
            '/api/mcp/connect',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            params={'server_url': 'http://localhost:8080'},
        )
        assert response.status_code == 403


class TestMCPTools:
    def test_list_mcp_tools_admin(self, client, admin_user, admin_token):
        response = client.get(
            '/api/mcp/tools',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert 'tools' in data

    def test_list_mcp_tools_with_server_id(self, client, admin_user, admin_token):
        response = client.get(
            '/api/mcp/tools?server_id=test-server',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
