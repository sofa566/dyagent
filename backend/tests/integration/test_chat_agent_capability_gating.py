from __future__ import annotations

from unittest.mock import patch

from src.models import Conversation, MCPConnection, SkillEntry
from src.services.skill_executor import SkillExecutionResult


def test_chat_tools_are_global_not_agent_bound(client, db, regular_user, regular_user_token, agent):
    # 目的：驗證聊天工具改為全域可用，不受 Agent 綁定 skill_ids/mcp_ids 限制。
    # 為什麼：new_permissions 決策改為工具公用，執行權限由策略層與實體權限治理。
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

    still_allowed_skill_response = client.post(
        f'/api/conversations/{conversation.id}/tools/{skill.name}',
        headers={'Authorization': f'Bearer {regular_user_token}'},
        json={},
    )
    assert still_allowed_skill_response.status_code == 200
    assert still_allowed_skill_response.json().get('error') != 'tool_not_allowed'

    still_allowed_mcp_response = client.post(
        f'/api/conversations/{conversation.id}/tools/mcp:{mcp.name}',
        headers={'Authorization': f'Bearer {regular_user_token}'},
        json={},
    )
    assert still_allowed_mcp_response.status_code == 200
    assert still_allowed_mcp_response.json().get('error') != 'tool_not_allowed'
