from __future__ import annotations


def test_user_can_read_but_cannot_update_agent_integrations(client, regular_user_token, agent):
    # 目的：驗證 user 在 LLM 設定上具唯讀權限。
    # 為什麼：US3 要求 user 可檢視設定但不可修改，避免權限過度放寬。
    read_response = client.get(
        f'/api/agents/{agent.id}/integrations',
        headers={'Authorization': f'Bearer {regular_user_token}'},
    )
    assert read_response.status_code == 200
    assert read_response.json().get('can_read') is True
    assert read_response.json().get('can_update') is False

    update_response = client.put(
        f'/api/agents/{agent.id}/integrations',
        headers={'Authorization': f'Bearer {regular_user_token}'},
        json={'skills': ['no-permission-update']},
    )
    assert update_response.status_code == 403
