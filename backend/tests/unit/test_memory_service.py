from __future__ import annotations

from src.core.config import settings
from src.services.memory_providers.mock_provider import MockMemoryProvider
from src.services.memory_service import MemoryService, OffMemoryProvider


def test_off_provider_retrieve_returns_empty() -> None:
    service = MemoryService(provider=OffMemoryProvider())
    result = service.retrieve(
        query='偏好',
        user_id='user-a',
        agent_id='agent-a',
        run_id='conv-a',
    )
    assert result.ok is True
    assert result.provider == 'off'
    assert result.snippets == []


def test_mock_provider_write_then_retrieve() -> None:
    provider = MockMemoryProvider()
    service = MemoryService(provider=provider)

    write_result = service.write(
        messages=[
            {'role': 'user', 'content': '我偏好繁體中文'},
            {'role': 'assistant', 'content': '收到，後續將使用繁體中文'},
        ],
        user_id='user-a',
        agent_id='agent-a',
        run_id='conv-a',
        metadata={'scope_type': 'interaction_scope'},
    )
    assert write_result.ok is True

    result = service.retrieve(
        query='繁體中文',
        user_id='user-a',
        agent_id='agent-a',
        run_id='conv-a',
    )
    assert result.ok is True
    assert len(result.snippets) == 1
    assert '繁體中文' in result.snippets[0].text
    assert result.snippets[0].scope_type == 'interaction_scope'


def test_read_disabled_returns_empty(monkeypatch) -> None:
    provider = MockMemoryProvider()
    service = MemoryService(provider=provider)
    service.write(
        messages=[{'role': 'user', 'content': '這是一段可搜尋內容'}],
        user_id='user-a',
        agent_id='agent-a',
        run_id='conv-a',
    )

    monkeypatch.setattr(settings, 'AGENT_MEMORY_READ_ENABLED', False)
    result = service.retrieve(
        query='可搜尋內容',
        user_id='user-a',
        agent_id='agent-a',
        run_id='conv-a',
    )
    assert result.ok is True
    assert result.snippets == []
