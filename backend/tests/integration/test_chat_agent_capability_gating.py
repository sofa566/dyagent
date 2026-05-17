from __future__ import annotations

from unittest.mock import patch

from src.models import Conversation, MCPConnection, SkillEntry
from src.services.skill_executor import SkillExecutionResult


def test_chat_capability_gating_for_skill_and_mcp(client, db, regular_user, regular_user_token, agent):
    # 目的：驗證聊天工具呼叫會受代理者 MCP/Skills 啟停設定限制。
    # 為什麼：US2 要求不同代理者能力隔離，未啟用能力不可在聊天被呼叫。
    skill = SkillEntry(name='gating-skill', description='gating skill', enabled=True, skill_type='prompt', prompt_template='mock')
    mcp = MCPConnection(name='gating-mcp', description='gating mcp', enabled=True, transport='remote', base_url='https://mcp.gating.test')
    db.add_all([skill, mcp])
    db.commit()
    db.refresh(skill)
    db.refresh(mcp)

    agent.model_config = {
        'skill_ids': [str(skill.id)],
        'mcp_ids': [str(mcp.id)],
    }
    db.commit()

    conversation = Conversation(agent_id=agent.id, user_id=regular_user.id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    with patch(
        'src.services.chat_router.execute_skill',
        return_value=SkillExecutionResult(ok=True, prompt='skill ok', script_outputs=[]),
    ):
        allowed_skill_response = client.post(
            f'/api/conversations/{conversation.id}/tools/{skill.name}',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            json={},
        )
    assert allowed_skill_response.status_code == 200
    assert allowed_skill_response.json().get('ok') is True

    allowed_mcp_response = client.post(
        f'/api/conversations/{conversation.id}/tools/mcp:{mcp.name}',
        headers={'Authorization': f'Bearer {regular_user_token}'},
        json={},
    )
    assert allowed_mcp_response.status_code == 200
    assert allowed_mcp_response.json().get('error') != 'tool_not_allowed'

    agent.model_config = {'skill_ids': [], 'mcp_ids': []}
    db.commit()

    denied_skill_response = client.post(
        f'/api/conversations/{conversation.id}/tools/{skill.name}',
        headers={'Authorization': f'Bearer {regular_user_token}'},
        json={},
    )
    assert denied_skill_response.status_code == 200
    assert denied_skill_response.json().get('ok') is False
    assert denied_skill_response.json().get('error') == 'tool_not_allowed'

    denied_mcp_response = client.post(
        f'/api/conversations/{conversation.id}/tools/mcp:{mcp.name}',
        headers={'Authorization': f'Bearer {regular_user_token}'},
        json={},
    )
    assert denied_mcp_response.status_code == 200
    assert denied_mcp_response.json().get('ok') is False
    assert denied_mcp_response.json().get('error') == 'tool_not_allowed'
