from __future__ import annotations

from src.api.routes import chat as chat_routes
from src.services.memory_providers.mock_provider import MockMemoryProvider


def test_forget_user_memory_removes_searchable_snippets(client, admin_token, regular_user):
    # 目的：驗證 forget user 後，該使用者記憶無法再被檢索。
    # 為什麼：資料治理要求清除後不可再命中，必須有端到端整合驗證。
    mock_provider = MockMemoryProvider()
    original_provider = chat_routes.memory_service._provider
    chat_routes.memory_service._provider = mock_provider

    try:
        write_result = chat_routes.memory_service.write(
            messages=[
                {'role': 'user', 'content': '我偏好條列式回覆'},
                {'role': 'assistant', 'content': '收到，後續我會先給重點清單'},
            ],
            user_id=str(regular_user.id),
            agent_id='agent-a',
            run_id='run-a',
            metadata={'scope_type': 'user_scope'},
        )
        assert write_result.ok is True

        before_forget = client.post(
            '/api/memory/search',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'query': '條列式', 'user_id': str(regular_user.id), 'top_k': 5},
        )
        assert before_forget.status_code == 200
        assert len(before_forget.json().get('snippets') or []) >= 1

        forget_response = client.post(
            f'/api/memory/users/{regular_user.id}/forget',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={},
        )
        assert forget_response.status_code == 200
        assert forget_response.json().get('ok') is True

        after_forget = client.post(
            '/api/memory/search',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'query': '條列式', 'user_id': str(regular_user.id), 'top_k': 5},
        )
        assert after_forget.status_code == 200
        assert (after_forget.json().get('snippets') or []) == []
    finally:
        chat_routes.memory_service._provider = original_provider
