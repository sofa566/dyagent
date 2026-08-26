from __future__ import annotations

from unittest.mock import patch

from src.models import Conversation, SkillEntry
from src.services.skill_executor import SkillExecutionResult


def test_confirmation_token_requires_single_use(client, db, regular_user, regular_user_token, agent):
    skill = SkillEntry(
        name='dangerous-confirm-skill',
        description='need confirm token',
        enabled=True,
        skill_type='prompt',
        prompt_template='mock',
        execution_policy={
            'risk_level': 'dangerous',
            'requires_confirmation': True,
        },
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)

    agent.model_config = {'skill_ids': [str(skill.id)], 'mcp_ids': []}
    db.commit()

    conversation = Conversation(agent_id=agent.id, user_id=regular_user.id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    first_response = client.post(
        f'/api/conversations/{conversation.id}/tools/{skill.name}',
        headers={'Authorization': f'Bearer {regular_user_token}'},
        json={'action': 'dangerous_write'},
    )
    assert first_response.status_code == 200
    first_payload = first_response.json()
    assert first_payload.get('ok') is False
    assert first_payload.get('error') == 'confirmation_required'
    confirm_token = str(((first_payload.get('policy') or {}).get('confirm_token') or '')).strip()
    assert confirm_token

    with patch(
        'src.services.chat_router.execute_skill',
        return_value=SkillExecutionResult(ok=True, prompt='skill ok', script_outputs=[]),
    ):
        confirmed_response = client.post(
            f'/api/conversations/{conversation.id}/tools/{skill.name}',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            json={
                'action': 'dangerous_write',
                '_policy_confirm_token': confirm_token,
            },
        )
    assert confirmed_response.status_code == 200
    assert confirmed_response.json().get('ok') is True

    replay_response = client.post(
        f'/api/conversations/{conversation.id}/tools/{skill.name}',
        headers={'Authorization': f'Bearer {regular_user_token}'},
        json={
            'action': 'dangerous_write',
            '_policy_confirm_token': confirm_token,
        },
    )
    assert replay_response.status_code == 200
    replay_payload = replay_response.json()
    assert replay_payload.get('ok') is False
    assert replay_payload.get('error') == 'confirmation_required'
