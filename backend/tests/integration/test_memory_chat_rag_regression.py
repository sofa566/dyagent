from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def test_chat_rag_path_remains_available_when_memory_retrieve_fails(client, db, regular_user_token, agent):
    # 目的：驗證記憶檢索失敗時，既有 chat/rag 路徑仍可正常回覆。
    # 為什麼：T045 要求記憶層新增後，不能讓原本 RAG 啟用流程退化。
    captured_prompts: list[str] = []

    async def _fake_stream_complete_async(_llm_client, *, prompt: str, tier: str | None):
        captured_prompts.append(prompt)
        yield '回歸測試回覆'

    fail_open_result = SimpleNamespace(
        ok=False,
        provider='mock',
        snippets=[],
        elapsed_ms=1,
        error='provider_unavailable',
        error_code='provider_unavailable',
    )

    with (
        patch('src.api.routes.chat._stream_complete_async', new=_fake_stream_complete_async),
        patch('src.api.routes.chat.memory_service.retrieve', return_value=fail_open_result),
    ):
        agent.rag_config = {'enabled': True, 'sources': ['kb-regression'], 'topK': 4}
        db.commit()

        response = client.get(
            f'/api/agents/{agent.id}/chat/stream',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            params={'message': '請回覆這個回歸測試問題'},
        )

    assert response.status_code == 200
    assert '"type":"done"' in response.text
    assert any('[RAG] sources=kb-regression topK=4' in prompt for prompt in captured_prompts)


def test_chat_path_without_rag_keeps_original_prompt_behavior(client, db, regular_user_token, agent):
    # 目的：驗證未啟用 RAG 時，記憶流程不會誤注入 RAG 區塊。
    # 為什麼：回歸測試需覆蓋 chat/rag 兩條主要提示組裝路徑。
    captured_prompts: list[str] = []

    async def _fake_stream_complete_async(_llm_client, *, prompt: str, tier: str | None):
        captured_prompts.append(prompt)
        yield '一般回覆'

    ok_result = SimpleNamespace(
        ok=True,
        provider='mock',
        snippets=[],
        elapsed_ms=1,
        error=None,
        error_code=None,
    )

    with (
        patch('src.api.routes.chat._stream_complete_async', new=_fake_stream_complete_async),
        patch('src.api.routes.chat.memory_service.retrieve', return_value=ok_result),
    ):
        agent.rag_config = {'enabled': False, 'sources': ['kb-regression'], 'topK': 4}
        db.commit()

        response = client.get(
            f'/api/agents/{agent.id}/chat/stream',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            params={'message': '請回覆這個一般測試問題'},
        )

    assert response.status_code == 200
    assert '"type":"done"' in response.text
    assert all('[RAG] sources=' not in prompt for prompt in captured_prompts)
