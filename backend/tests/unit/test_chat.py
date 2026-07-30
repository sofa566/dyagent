from datetime import datetime, timedelta
from types import SimpleNamespace

from src.models import (
    ChatAttachment,
    Conversation,
    LlmTurn,
    Message,
    MultiAgentSession,
    MultiAgentTask,
    SkillInteraction,
)
from src.models.events import EventPart


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

    def test_delete_conversation_with_related_rows(self, client, db, admin_user, admin_token, agent):
        conversation = Conversation(agent_id=agent.id, user_id=admin_user.id)
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

        message = Message(conversation_id=conversation.id, role='user', content='hi')
        event_part = EventPart(conversation_id=conversation.id, type='tool', payload={'ok': True})
        llm_turn = LlmTurn(conversation_id=conversation.id, agent_id=agent.id, status='success')
        multi_session = MultiAgentSession(conversation_id=conversation.id, router_agent_id=agent.id, user_message='plan')
        skill_interaction = SkillInteraction(
            conversation_id=conversation.id,
            tool_name='demo_tool',
            status='active',
            expires_at=datetime.now() + timedelta(minutes=5),
        )
        chat_attachment = ChatAttachment(
            conversation_id=conversation.id,
            user_id=admin_user.id,
            filename='a.txt',
            ext='txt',
            mime_type='text/plain',
            file_path='/tmp/a.txt',
            size_bytes=1,
            status='uploaded',
        )

        db.add_all([message, event_part, llm_turn, multi_session, skill_interaction, chat_attachment])
        db.commit()
        db.refresh(multi_session)

        multi_task = MultiAgentTask(session_id=multi_session.id, task_index=0, agent_id=agent.id, task_desc='task')
        db.add(multi_task)
        db.commit()

        response = client.delete(
            f'/api/conversations/{conversation.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 200
        assert response.json().get('ok') is True
        assert db.query(Conversation).filter(Conversation.id == conversation.id).first() is None
        assert db.query(Message).filter(Message.conversation_id == conversation.id).count() == 0
        assert db.query(EventPart).filter(EventPart.conversation_id == conversation.id).count() == 0
        assert db.query(LlmTurn).filter(LlmTurn.conversation_id == conversation.id).count() == 0
        assert db.query(SkillInteraction).filter(SkillInteraction.conversation_id == conversation.id).count() == 0
        assert db.query(ChatAttachment).filter(ChatAttachment.conversation_id == conversation.id).count() == 0


class TestMemory:
    def test_forget_my_long_term_memory_success(self, client, admin_token, monkeypatch):
        from src.api.routes import chat as chat_routes

        monkeypatch.setattr(
            chat_routes.memory_service,
            'forget_user',
            lambda *, user_id, app_id=None: SimpleNamespace(ok=True, error=None),
        )
        response = client.post(
            '/api/chat/memory/forget',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={},
        )

        assert response.status_code == 200
        assert response.json().get('ok') is True

    def test_forget_my_long_term_memory_failed(self, client, admin_token, monkeypatch):
        from src.api.routes import chat as chat_routes

        monkeypatch.setattr(
            chat_routes.memory_service,
            'forget_user',
            lambda *, user_id, app_id=None: SimpleNamespace(ok=False, error='mock_error'),
        )
        response = client.post(
            '/api/chat/memory/forget',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={},
        )

        assert response.status_code == 400
        assert response.json().get('detail', {}).get('code') == 'VALIDATION_ERROR'

    def test_memory_health_requires_admin(self, client, regular_user_token):
        response = client.get(
            '/api/memory/health',
            headers={'Authorization': f'Bearer {regular_user_token}'},
        )
        assert response.status_code == 403

    def test_memory_health_success(self, client, admin_token, monkeypatch):
        from src.api.routes import chat as chat_routes

        monkeypatch.setattr(
            chat_routes.memory_service,
            'health',
            lambda: SimpleNamespace(ok=True, provider='mock', degraded=False, error=None, error_code=None),
        )
        response = client.get(
            '/api/memory/health',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload.get('ok') is True
        assert payload.get('provider') == 'mock'

    def test_memory_search_success(self, client, admin_token, monkeypatch):
        from src.api.routes import chat as chat_routes

        monkeypatch.setattr(
            chat_routes.memory_service,
            'retrieve',
            lambda **kwargs: SimpleNamespace(
                ok=True,
                provider='mock',
                elapsed_ms=3,
                error=None,
                error_code=None,
                snippets=[
                    SimpleNamespace(
                        text='user: 喜歡簡潔回覆',
                        score=0.95,
                        scope_type='user_scope',
                        source='mock',
                        metadata={'tag': 'preference'},
                    )
                ],
            ),
        )
        response = client.post(
            '/api/memory/search',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'query': '簡潔', 'top_k': 5},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload.get('ok') is True
        assert len(payload.get('snippets') or []) == 1
        assert payload['snippets'][0]['scope_type'] == 'user_scope'

    def test_memory_search_query_required(self, client, admin_token):
        response = client.post(
            '/api/memory/search',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'query': ''},
        )
        assert response.status_code == 400

    def test_memory_forget_user_by_admin_success(self, client, admin_token, regular_user, monkeypatch):
        from src.api.routes import chat as chat_routes

        monkeypatch.setattr(
            chat_routes.memory_service,
            'forget_user',
            lambda *, user_id, app_id=None: SimpleNamespace(ok=True, error=None),
        )
        response = client.post(
            f'/api/memory/users/{regular_user.id}/forget',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={},
        )

        assert response.status_code == 200
        assert response.json().get('ok') is True

    def test_memory_forget_user_by_admin_not_found(self, client, admin_token):
        response = client.post(
            '/api/memory/users/00000000-0000-0000-0000-000000000000/forget',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={},
        )
        assert response.status_code == 404
