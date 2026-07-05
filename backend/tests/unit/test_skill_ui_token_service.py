from src.services.skill_ui_token_service import create_skill_ui_token, verify_skill_ui_token


def test_skill_ui_token_create_and_verify():
    token = create_skill_ui_token(
        conversation_id='conv-1',
        tool_name='leave-request',
        interaction_id='inter-1',
        expires_in_seconds=300,
    )
    ok, error_text, payload = verify_skill_ui_token(token)
    assert ok is True
    assert error_text is None
    assert payload is not None
    assert payload.get('conversation_id') == 'conv-1'
    assert payload.get('tool_name') == 'leave-request'
    assert payload.get('interaction_id') == 'inter-1'


def test_skill_ui_token_invalid_signature():
    token = create_skill_ui_token(
        conversation_id='conv-2',
        tool_name='expense-request',
        interaction_id='inter-2',
        expires_in_seconds=300,
    )
    broken = token + 'x'
    ok, error_text, _payload = verify_skill_ui_token(broken)
    assert ok is False
    assert error_text == 'invalid_token_signature'
