from __future__ import annotations

from src.services.memory_providers.mem0_provider import Mem0Provider
from src.services.memory_service import MemoryService


class _FakeMem0Client:
    def __init__(self) -> None:
        self.add_calls: list[tuple[list[dict[str, str]], dict]] = []
        self.search_calls: list[dict] = []
        self.delete_all_calls: list[dict] = []
        self.search_rows: list[dict] = []
        self.add_error: Exception | None = None
        self.search_error: Exception | None = None
        self.delete_error: Exception | None = None

    def add(self, messages, **kwargs):
        if self.add_error is not None:
            raise self.add_error
        self.add_calls.append((messages, kwargs))

    def search(self, query, filters=None, top_k=5):
        if self.search_error is not None:
            raise self.search_error
        self.search_calls.append({'query': query, 'filters': filters, 'top_k': top_k})
        return {'results': list(self.search_rows)}

    def delete_all(self, **kwargs):
        if self.delete_error is not None:
            raise self.delete_error
        self.delete_all_calls.append(kwargs)


def _build_provider_with_fake_client() -> tuple[Mem0Provider, _FakeMem0Client]:
    provider = Mem0Provider('unsupported_mode_for_test')
    client = _FakeMem0Client()
    provider._client = client
    return provider, client


def test_add_user_scope_maps_only_user_dimension() -> None:
    provider, client = _build_provider_with_fake_client()
    result = provider.add(
        messages=[{'role': 'user', 'content': '偏好繁體中文'}],
        user_id='user-a',
        agent_id='agent-a',
        run_id='run-a',
        app_id='dyagent',
        metadata={'scope_type': 'user_scope'},
    )

    assert result.ok is True
    _, kwargs = client.add_calls[0]
    assert kwargs.get('user_id') == 'user-a'
    assert 'agent_id' not in kwargs
    assert 'run_id' not in kwargs
    assert kwargs['metadata']['scope_type'] == 'user_scope'


def test_add_global_scope_does_not_bind_user_or_agent() -> None:
    provider, client = _build_provider_with_fake_client()
    result = provider.add(
        messages=[{'role': 'assistant', 'content': '全域規範'}],
        user_id='user-a',
        agent_id='agent-a',
        run_id='run-a',
        app_id='dyagent',
        metadata={'scope_type': 'global_scope'},
    )

    assert result.ok is True
    _, kwargs = client.add_calls[0]
    assert 'user_id' not in kwargs
    assert 'agent_id' not in kwargs
    assert 'run_id' not in kwargs
    assert kwargs['metadata']['scope_type'] == 'global_scope'


def test_search_filters_out_invisible_scope_rows() -> None:
    provider, client = _build_provider_with_fake_client()
    client.search_rows = [
        {
            'memory': '使用者偏好',
            'score': 0.9,
            'metadata': {'scope_type': 'user_scope', 'user_id': 'user-a'},
        },
        {
            'memory': '他人偏好',
            'score': 0.9,
            'metadata': {'scope_type': 'user_scope', 'user_id': 'user-b'},
        },
        {
            'memory': '代理專業記憶',
            'score': 0.8,
            'metadata': {'scope_type': 'agent_scope', 'agent_id': 'agent-a'},
        },
        {
            'memory': '全域規範',
            'score': 0.7,
            'metadata': {'scope_type': 'global_scope'},
        },
    ]

    snippets = provider.search(
        query='記憶',
        user_id='user-a',
        agent_id='agent-a',
        run_id='run-a',
        app_id='dyagent',
        top_k=10,
    )

    snippet_texts = [item.text for item in snippets]
    assert '使用者偏好' in snippet_texts
    assert '代理專業記憶' in snippet_texts
    assert '全域規範' in snippet_texts
    assert '他人偏好' not in snippet_texts


def test_search_interaction_scope_visible_when_only_user_id_provided() -> None:
    provider, client = _build_provider_with_fake_client()
    client.search_rows = [
        {
            'memory': '我的互動記憶',
            'score': 0.9,
            'metadata': {'scope_type': 'interaction_scope', 'user_id': 'user-a', 'agent_id': 'agent-a', 'run_id': 'run-a'},
        },
        {
            'memory': '他人的互動記憶',
            'score': 0.8,
            'metadata': {'scope_type': 'interaction_scope', 'user_id': 'user-b', 'agent_id': 'agent-b'},
        },
    ]

    snippets = provider.search(
        query='記憶',
        user_id='user-a',
        agent_id=None,
        run_id=None,
        app_id='dyagent',
        top_k=10,
    )

    snippet_texts = [item.text for item in snippets]
    assert '我的互動記憶' in snippet_texts
    assert '他人的互動記憶' not in snippet_texts


def test_service_retrieve_fail_open_contains_timeout_code() -> None:
    provider, client = _build_provider_with_fake_client()
    client.search_error = RuntimeError('connect timeout while calling provider')
    service = MemoryService(provider=provider)

    result = service.retrieve(
        query='偏好',
        user_id='user-a',
        agent_id='agent-a',
        run_id='run-a',
    )

    assert result.ok is False
    assert result.snippets == []
    assert result.error_code == 'timeout'


def test_provider_add_error_classifies_auth() -> None:
    provider, client = _build_provider_with_fake_client()
    client.add_error = RuntimeError('401 unauthorized: invalid api key')

    result = provider.add(
        messages=[{'role': 'user', 'content': '測試'}],
        user_id='user-a',
        agent_id='agent-a',
        run_id='run-a',
        app_id='dyagent',
        metadata={'scope_type': 'interaction_scope'},
    )

    assert result.ok is False
    assert result.error_code == 'auth'


def test_provider_add_ignores_unsupported_app_id_argument() -> None:
    class _StrictAddClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def add(self, messages, user_id=None, metadata=None):
            self.calls.append({'messages': messages, 'user_id': user_id, 'metadata': metadata})

    provider = Mem0Provider('unsupported_mode_for_test')
    strict_client = _StrictAddClient()
    provider._client = strict_client

    result = provider.add(
        messages=[{'role': 'user', 'content': '測試'}],
        user_id='user-a',
        agent_id='agent-a',
        run_id='run-a',
        app_id='dyagent',
        metadata={'scope_type': 'user_scope'},
    )

    assert result.ok is True
    assert len(strict_client.calls) == 1
    assert strict_client.calls[0]['user_id'] == 'user-a'


def test_provider_forget_user_ignores_unsupported_app_id_argument() -> None:
    class _StrictDeleteClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def delete_all(self, user_id=None, confirm=False):
            self.calls.append({'user_id': user_id, 'confirm': confirm})

    provider = Mem0Provider('unsupported_mode_for_test')
    strict_client = _StrictDeleteClient()
    provider._client = strict_client

    result = provider.forget_user(user_id='user-a', app_id='dyagent')

    assert result.ok is True
    assert len(strict_client.calls) == 1
    assert strict_client.calls[0]['user_id'] == 'user-a'
    assert strict_client.calls[0]['confirm'] is True
