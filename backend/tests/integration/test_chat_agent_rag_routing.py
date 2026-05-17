from __future__ import annotations

from unittest.mock import patch


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
