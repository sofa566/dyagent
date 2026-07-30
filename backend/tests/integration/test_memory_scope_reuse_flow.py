from __future__ import annotations

from src.api.routes import chat as chat_routes
from src.services.memory_providers.mem0_provider import Mem0Provider


class _InMemoryMem0Client:
    def __init__(self) -> None:
        self._rows: list[dict] = []

    def add(self, messages, **kwargs):
        metadata = dict(kwargs.get('metadata') or {})
        text_parts: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get('role') or '').strip()
            content = str(msg.get('content') or '').strip()
            if not content:
                continue
            text_parts.append(f'{role}: {content}' if role else content)
        self._rows.append(
            {
                'memory': '\n'.join(text_parts),
                'score': 1.0,
                'metadata': metadata,
                'app_id': str(kwargs.get('app_id') or ''),
            }
        )

    def search(self, query, filters=None, top_k=5):
        app_id = str((filters or {}).get('app_id') or '')
        normalized_query = str(query or '').strip().lower()
        results: list[dict] = []
        for row in self._rows:
            if app_id and str(row.get('app_id') or '') != app_id:
                continue
            text = str(row.get('memory') or '')
            if normalized_query and normalized_query not in text.lower():
                continue
            results.append(row)
            if len(results) >= max(1, int(top_k or 1)):
                break
        return {'results': results}

    def delete_all(self, **kwargs):
        app_id = str(kwargs.get('app_id') or '')
        user_id = str(kwargs.get('user_id') or '')
        remained: list[dict] = []
        for row in self._rows:
            metadata = dict(row.get('metadata') or {})
            if app_id and str(row.get('app_id') or '') != app_id:
                remained.append(row)
                continue
            if user_id and str(metadata.get('user_id') or '') == user_id:
                continue
            remained.append(row)
        self._rows = remained


def _install_fake_mem0_provider():
    provider = Mem0Provider('unsupported_mode_for_test')
    provider._client = _InMemoryMem0Client()
    provider._init_error = None
    provider._init_error_code = None
    return provider


def test_user_scope_preference_reused_across_agents(client, admin_token, regular_user):
    # 目的：驗證 user_scope 記憶可跨代理重用。
    # 為什麼：同一使用者偏好應跨代理延續，不應綁死單一 agent。
    original_provider = chat_routes.memory_service._provider
    chat_routes.memory_service._provider = _install_fake_mem0_provider()

    try:
        write_result = chat_routes.memory_service.write(
            messages=[{'role': 'user', 'content': '偏好先給摘要'}],
            user_id=str(regular_user.id),
            agent_id='agent-source',
            run_id='run-source',
            metadata={'scope_type': 'user_scope'},
        )
        assert write_result.ok is True

        search_response = client.post(
            '/api/memory/search',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'query': '摘要',
                'user_id': str(regular_user.id),
                'agent_id': 'agent-other',
                'top_k': 5,
            },
        )
        assert search_response.status_code == 200
        snippets = search_response.json().get('snippets') or []
        assert len(snippets) >= 1
        assert snippets[0].get('scope_type') == 'user_scope'
    finally:
        chat_routes.memory_service._provider = original_provider


def test_agent_scope_memory_reused_across_users(client, admin_token, regular_user):
    # 目的：驗證 agent_scope 記憶可跨使用者重用。
    # 為什麼：代理專業知識應由代理共享，不受單一使用者綁定。
    original_provider = chat_routes.memory_service._provider
    chat_routes.memory_service._provider = _install_fake_mem0_provider()

    try:
        write_result = chat_routes.memory_service.write(
            messages=[{'role': 'assistant', 'content': '代理專業 SOP：先做風險評估'}],
            user_id=str(regular_user.id),
            agent_id='agent-finance',
            run_id='run-a',
            metadata={'scope_type': 'agent_scope'},
        )
        assert write_result.ok is True

        cross_user_response = client.post(
            '/api/memory/search',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'query': '風險評估',
                'user_id': 'another-user',
                'agent_id': 'agent-finance',
                'top_k': 5,
            },
        )
        assert cross_user_response.status_code == 200
        cross_user_snippets = cross_user_response.json().get('snippets') or []
        assert len(cross_user_snippets) >= 1
        assert cross_user_snippets[0].get('scope_type') == 'agent_scope'

        other_agent_response = client.post(
            '/api/memory/search',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'query': '風險評估',
                'user_id': 'another-user',
                'agent_id': 'agent-sales',
                'top_k': 5,
            },
        )
        assert other_agent_response.status_code == 200
        assert (other_agent_response.json().get('snippets') or []) == []
    finally:
        chat_routes.memory_service._provider = original_provider
