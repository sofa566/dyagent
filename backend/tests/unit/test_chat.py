import pytest


class TestChat:
    def test_chat_stream_with_agent(self, client, admin_user, admin_token, agent):
        response = client.get(
            f'/api/agents/{agent.id}/chat/stream',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={'message': 'Hello, agent!'},
        )
        assert response.status_code == 200
        body = response.text
        assert '"type":"done"' in body
        assert 'conversation_id' in body

    def test_chat_stream_with_nonexistent_agent(self, client, admin_user, admin_token):
        response = client.get(
            '/api/agents/nonexistent-id/chat/stream',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={'message': 'Hello!'},
        )
        assert response.status_code == 404

    def test_chat_stream_empty_message(self, client, admin_user, admin_token, agent):
        response = client.get(
            f'/api/agents/{agent.id}/chat/stream',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={'message': ''},
        )
        assert response.status_code == 400


class TestConversations:
    def test_get_conversations(self, client, admin_user, admin_token, agent):
        response = client.get(
            f'/api/agents/{agent.id}/conversations',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert 'conversations' in data

    def test_get_conversations_nonexistent_agent(self, client, admin_user, admin_token):
        response = client.get(
            '/api/agents/nonexistent-id/conversations',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 404
