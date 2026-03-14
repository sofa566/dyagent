import os

from src.services.chat_router import ChatRouter


def test_multi_agent_provider_isolation_env_restored(monkeypatch):
    # 準備全域環境基準值
    sentinel = "SENTINEL_BASE"
    monkeypatch.setenv("OPENAI_API_BASE", sentinel)
    monkeypatch.setenv("OPENAI_API_KEY_DUMMY", "sk-dummy")

    router = ChatRouter()

    # 雲端 OpenAI 覆蓋（使用 api_key_ref 指向 DUMMY，不要求真連線）
    ov_cloud = {
        "tier": "cloud",
        "provider": "openai",
        "model": "gpt-3.5-turbo",
        "api_key_ref": "OPENAI_API_KEY_DUMMY",
    }
    out1 = router.single_turn(
        session_id="s-cloud",
        agent_id="a-cloud",
        user_message="Say hi",
        tier=ov_cloud.get("tier"),
        llm_overrides=ov_cloud,
    )
    assert isinstance(out1, str)
    info1 = router._llm.last_route_info()  # type: ignore[attr-defined]
    # 可能因無法連 OpenAI 而退回 on‑prem 嘗試；此處容忍 tier 為 cloud 或 onprem
    assert info1.get("tier") in ("cloud", "onprem")
    # provider 可能為 openai 或（退回）ollama，皆可接受
    assert info1.get("provider") in ("openai", "ollama", "azure", "gemini", "anthropic", "openrouter", "grok", "")

    # 確認全域環境未被永久污染
    assert os.environ.get("OPENAI_API_BASE") == sentinel

    # 地端 Ollama 覆蓋（不要求真連線）
    ov_onprem = {
        "tier": "onprem",
        "onprem_provider": "ollama",
        "onprem_base_url": "http://localhost:11434",
        "model": "qwen3:8b-16k",
    }
    out2 = router.single_turn(
        session_id="s-onprem",
        agent_id="a-onprem",
        user_message="Say hi",
        tier=ov_onprem.get("tier"),
        llm_overrides=ov_onprem,
    )
    assert isinstance(out2, str)
    info2 = router._llm.last_route_info()  # type: ignore[attr-defined]
    assert info2.get("tier") == "onprem"
    # base_hint 僅為提示字串，至少不應為空
    assert isinstance(info2.get("base_hint"), str)

    # 再次確認全域環境未被永久污染
    assert os.environ.get("OPENAI_API_BASE") == sentinel
