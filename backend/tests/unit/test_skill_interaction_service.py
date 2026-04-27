from datetime import datetime, timedelta

from src.models import Conversation, SkillEntry
from src.services.skill_interaction_service import SkillInteractionService


def test_prepare_start_and_finalize_ui(db, agent):
    conversation = Conversation(agent_id=agent.id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    skill = SkillEntry(name='leave-request', enabled=True, type='python', python_handler='x:y')
    db.add(skill)
    db.commit()
    db.refresh(skill)

    service = SkillInteractionService(db)
    prepared = service.prepare_request(
        conversation_id=str(conversation.id),
        tool_name='leave-request',
        skill_id=str(skill.id),
        payload={'action': 'start', 'form_data': {'employeeId': 'E001'}},
    )
    assert prepared.ok is True
    assert prepared.interaction is not None
    assert prepared.skill_payload.get('employeeId') == 'E001'
    assert isinstance(prepared.skill_payload.get('_interaction'), dict)

    result_payload = {
        'ok': True,
        'result': {
            'mode': 'ui',
            'step': 'step-1',
            'ui': {
                'entry': 'ui/index.html',
                'title': '員工請假申請',
                'state': {'employeeId': 'E001'},
            },
        },
    }
    finalized = service.finalize_success(prepare_result=prepared, result_payload=result_payload)
    result_obj = finalized.get('result') if isinstance(finalized.get('result'), dict) else {}
    ui_obj = result_obj.get('ui') if isinstance(result_obj.get('ui'), dict) else {}
    assert result_obj.get('interaction_id')
    assert result_obj.get('channel_nonce')
    assert str(ui_obj.get('ui_url') or '').startswith('/api/skills/ui/')


def test_prepare_submit_expired_interaction(db, agent):
    conversation = Conversation(agent_id=agent.id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    skill = SkillEntry(name='expense-request', enabled=True, type='python', python_handler='x:y')
    db.add(skill)
    db.commit()
    db.refresh(skill)

    service = SkillInteractionService(db)
    prepared = service.prepare_request(
        conversation_id=str(conversation.id),
        tool_name='expense-request',
        skill_id=str(skill.id),
        payload={'action': 'start', 'form_data': {}},
    )
    interaction = prepared.interaction
    assert interaction is not None
    interaction.expires_at = datetime.now() - timedelta(seconds=5)
    db.commit()

    retry = service.prepare_request(
        conversation_id=str(conversation.id),
        tool_name='expense-request',
        skill_id=str(skill.id),
        payload={'action': 'submit', 'interaction_id': str(interaction.id), 'form_data': {'amount': 100}},
    )
    assert retry.ok is False
    assert retry.error == 'interaction_expired'
