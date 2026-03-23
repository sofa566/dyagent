import pytest


class TestAgentsList:
    def test_list_agents_admin(self, client, admin_user, admin_token, agent):
        response = client.get(
            '/api/agents',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert 'agents' in data
        assert len(data['agents']) >= 1

    def test_list_agents_regular_user_forbidden(self, client, regular_user, regular_user_token):
        response = client.get(
            '/api/agents',
            headers={'Authorization': f'Bearer {regular_user_token}'},
        )
        assert response.status_code == 403

    def test_list_agents_unauthorized(self, client):
        response = client.get('/api/agents')
        assert response.status_code == 403


class TestAgentsCreate:
    def test_create_agent_admin(self, client, admin_user, admin_token):
        response = client.post(
            '/api/agents',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={
                'name': 'New Agent',
                'description': 'A new test agent',
                'model_type': 'cloud',
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data['name'] == 'New Agent'
        assert data['description'] == 'A new test agent'

    def test_create_agent_empty_name(self, client, admin_user, admin_token):
        response = client.post(
            '/api/agents',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={
                'name': '',
                'description': 'Test',
            },
        )
        assert response.status_code == 400

    def test_create_agent_regular_user_forbidden(self, client, regular_user, regular_user_token):
        response = client.post(
            '/api/agents',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            params={'name': 'New Agent'},
        )
        assert response.status_code == 403


class TestAgentsGet:
    def test_get_agent_admin(self, client, admin_user, admin_token, agent):
        response = client.get(
            f'/api/agents/{agent.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert data['name'] == 'Test Agent'

    def test_get_agent_nonexistent(self, client, admin_user, admin_token):
        response = client.get(
            '/api/agents/nonexistent-id',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 404


class TestAgentsUpdate:
    def test_update_agent_admin(self, client, admin_user, admin_token, agent):
        response = client.put(
            f'/api/agents/{agent.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={
                'name': 'Updated Agent',
                'description': 'Updated description',
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data['name'] == 'Updated Agent'

    def test_update_agent_admin_with_json_body(self, client, admin_user, admin_token, agent):
        response = client.put(
            f'/api/agents/{agent.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'name': 'Body Updated Agent',
                'description': 'Body updated description',
                'model_type': 'local',
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data['name'] == 'Body Updated Agent'
        assert data['model_type'] == 'local'

    def test_update_agent_nonexistent(self, client, admin_user, admin_token):
        response = client.put(
            '/api/agents/nonexistent-id',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={'name': 'Updated'},
        )
        assert response.status_code == 404


class TestAgentsDelete:
    def test_delete_agent_admin(self, client, admin_user, admin_token, agent):
        response = client.delete(
            f'/api/agents/{agent.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200

    def test_delete_agent_nonexistent(self, client, admin_user, admin_token):
        response = client.delete(
            '/api/agents/nonexistent-id',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 404
