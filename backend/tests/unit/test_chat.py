import pytest
from datetime import datetime, timedelta

from src.models import ChatAttachment, Conversation, LlmTurn, Message, MultiAgentSession, MultiAgentTask, SkillInteraction
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
