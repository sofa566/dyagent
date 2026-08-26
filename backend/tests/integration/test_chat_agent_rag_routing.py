from __future__ import annotations

from unittest.mock import patch

from src.api.routes import chat as chat_routes
from src.models import RagDataset


def test_chat_rag_capability_prompt_respects_enabled_flag(client, db, regular_user_token, agent):
    # 目的：驗證聊天 prompt 的 RAG 區塊與代理者設定一致。
    # 為什麼：US2 要求 RAG 啟停與來源一致，避免未啟用時誤注入檢索資訊。
    captured_prompts: list[str] = []

    async def _fake_stream_complete_async(_llm_client, *, prompt: str, tier: str | None):
        captured_prompts.append(prompt)
        yield '測試回覆'

    with patch('src.api.routes.chat._stream_complete_async', new=_fake_stream_complete_async):
        agent.rag_config = {'enabled': True, 'sources': ['kb-alpha', 'faq'], 'topK': 7}
        db.commit()
        response_enabled = client.get(
            f'/api/agents/{agent.id}/chat/stream',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            params={'message': '請回答 RAG 問題'},
        )
        assert response_enabled.status_code == 200
        assert any('[RAG] sources=kb-alpha,faq topK=7' in prompt for prompt in captured_prompts)

        captured_prompts.clear()
        agent.rag_config = {'enabled': False, 'sources': ['kb-alpha'], 'topK': 7}
        db.commit()
        response_disabled = client.get(
            f'/api/agents/{agent.id}/chat/stream',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            params={'message': '請回答一般問題'},
        )
        assert response_disabled.status_code == 200
        assert all('[RAG] sources=' not in prompt for prompt in captured_prompts)


def test_chat_rag_does_not_fallback_when_selected_dataset_ids_empty(client, db, regular_user_token, agent, monkeypatch):
    # 目的：驗證 selected_dataset_ids 為空時不做任何資料集檢索。
    # 為什麼：避免誤回退到全域資料集，造成越權與答非所問。
    dataset = RagDataset(
        name='全域資料集A',
        scope='global',
        sensitivity='normal',
        enabled=True,
    )
    db.add(dataset)
    db.commit()

    agent.rag_config = {
        'enabled': True,
        'sources': ['kb-alpha'],
        'topK': 5,
        'global_dataset_ids': [str(dataset.id)],
    }
    db.commit()

    async def _fake_stream_complete_async(_llm_client, *, prompt: str, tier: str | None):
        yield '測試回覆'

    def _raise_if_search_called(*_args, **_kwargs):
        raise AssertionError('selected_dataset_ids 為空時不應執行 qdrant 搜尋')

    monkeypatch.setattr('src.api.routes.chat.qdrant_service.search', _raise_if_search_called)
    with patch('src.api.routes.chat._stream_complete_async', new=_fake_stream_complete_async):
        response = client.get(
            f'/api/agents/{agent.id}/chat/stream',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            params={'message': '一般問題，不帶資料集'},
        )

    assert response.status_code == 200


def test_chat_rag_global_dataset_accessible_without_entity_permission(db, regular_user, agent, monkeypatch):
    # 目的：驗證未授予 entity.dataset.* 時，公有資料集仍可被聊天明確選用。
    # 為什麼：公有資料集策略改為所有使用者可用，僅私有資料集才需實體權限控管。
    dataset = RagDataset(
        name='全域資料集可用',
        scope='global',
        sensitivity='normal',
        enabled=True,
    )
    db.add(dataset)
    db.commit()

    agent.rag_config = {
        'enabled': True,
        'topK': 5,
    }
    db.commit()

    monkeypatch.setattr('src.api.routes.chat.embedding_service.embed_one', lambda _query: [0.01, 0.02, 0.03])

    def _fake_search(collection_name, query_vector, limit=5):
        return [
            {
                'id': 'hit-1',
                'score': 0.88,
                'payload': {
                    'filename': 'global-doc.txt',
                    'snippet': '這是全域資料集命中的片段',
                },
            },
        ][:limit]

    monkeypatch.setattr('src.api.routes.chat.qdrant_service.search', _fake_search)

    rag_context, rag_runtime = chat_routes._build_chat_rag_context(
        db=db,
        current_user=regular_user,
        agent=agent,
        query='請根據資料集回答',
        selected_dataset_ids=[str(dataset.id)],
    )

    assert str(dataset.id) in list(rag_runtime.get('effective_dataset_ids') or [])
    assert int(rag_runtime.get('hits') or 0) > 0
    assert bool(rag_context)
