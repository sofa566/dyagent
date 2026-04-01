"""
LLM 客戶端（骨架）

目的：
- 於 Session 建立時初始化雲端/地端 LLM 連線設定（langchain-litellm 將於後續實作接入）。
- 提供簡單的路由介面（cloud_default / onprem_default）以供 chat_router 委派呼叫。

注意：
- 本檔僅為骨架，尚未串接實際供應商 SDK 或 LangChain 實例。
- 依專案憲章，註解採繁體中文；實作細節於後續任務補上。
"""

from __future__ import annotations

from typing import Generator, Optional
import time
import os
import httpx
from src.core.config import settings, validate_llm_settings
from src.core.logging import get_logger
from src.services.secrets import SecretsProvider, get_secrets_provider
from src.utils.temp_env import temp_env
from urllib.parse import urlparse


class LLMClient:
    """LLM 客戶端骨架。

    層級（tier）
    - cloud：使用雲端提供者（由環境變數 LITELLM_CLOUD_PROVIDER 指定）
    - onprem：使用地端提供者（由環境變數 LITELLM_ONPREM_BASE_URL 指定）
    """

    def __init__(self) -> None:
        # 預留：可在此建立連線池、會話或憑證載入邏輯
        self._cloud_provider = settings.LITELLM_CLOUD_PROVIDER
        self._onprem_base_url = settings.LITELLM_ONPREM_BASE_URL
        self._default_tier = settings.LLM_DEFAULT_TIER
        self._timeout_s = settings.LLM_TIMEOUT_SECONDS
        self._log = get_logger("LLMClient")
        # 嘗試偵測 langchain-litellm（或等效）可用性
        self._lc_available = self._detect_langchain()
        self._secrets: SecretsProvider = get_secrets_provider()
        self._last_route: dict = {}

    def init_for_session(self, *, session_id: str, preferred_tier: Optional[str] = None, overrides: Optional[dict] = None) -> None:
        """於 Session 建立階段初始化必要狀態。

        備註：此處僅保留狀態欄位，實際 LangChain/LiteLLM 實例化於後續任務補上。
        """
        self._session_id = session_id
        self._session_tier = (overrides or {}).get("tier") or preferred_tier or self._default_tier
        # Session 級別覆蓋：允許 per-agent 設定供應商/模型/端點/API Key
        self._session_overrides = overrides or {}
        # 設定驗證（T010）
        validate_llm_settings()
        self._log.info(
            "llm.init_for_session",
            session_id=session_id,
            tier=self._session_tier,
            cloud_provider=(self._session_overrides.get("provider") or self._cloud_provider or ""),
            onprem=bool(self._session_overrides.get("onprem_base_url") or self._session_overrides.get("base_url") or self._onprem_base_url),
            timeout_s=self._timeout_s,
        )

    def cloud_default(self, *, prompt: str) -> str:
        """雲端預設路由（骨架）。

        注意：為避免誤判為「回聲」回覆，骨架不再回傳原始 prompt，
        僅給出最小可見佔位文字，提示尚未連接真實模型。
        後續將以 langchain-litellm 串流/完成呼叫替換此回傳。
        """
        # TODO: 以 LangChain + LiteLLM 取代，並加入串流/重試/逾時
        return "（模型尚未連接；已使用雲端骨架回覆）"

    def onprem_default(self, *, prompt: str) -> str:
        """地端預設路由（骨架）。

        同上，不回傳原始 prompt，避免出現與使用者輸入相同的回聲效果。
        """
        # TODO: 以 LangChain + LiteLLM 取代，並加入串流/重試/逾時
        return "（模型尚未連接；已使用地端骨架回覆）"

    def complete(self, *, prompt: str, tier: Optional[str] = None) -> str:
        """根據 tier（cloud/onprem）選擇路由執行。

        若未指定 tier，使用 Session 層級或系統預設層級。
        """
        route = (tier or getattr(self, "_session_tier", None) or self._default_tier).lower()
        if route == "onprem":
            return self.onprem_default(prompt=prompt)
        return self.cloud_default(prompt=prompt)

    def _extract_stream_text(self, chunk) -> str:
        content = getattr(chunk, "content", None)
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    text_parts.append(item)
                elif isinstance(item, dict):
                    item_text = item.get("text")
                    if isinstance(item_text, str) and item_text:
                        text_parts.append(item_text)
            merged = "".join(text_parts)
            if merged:
                return merged
        include_reasoning = bool(getattr(settings, "LLM_STREAM_INCLUDE_REASONING", False))
        if include_reasoning:
            reasoning_content = getattr(chunk, "reasoning_content", None)
            if isinstance(reasoning_content, str) and reasoning_content:
                return reasoning_content
            additional_kwargs = getattr(chunk, "additional_kwargs", None)
            if isinstance(additional_kwargs, dict):
                maybe_reasoning = additional_kwargs.get("reasoning_content")
                if isinstance(maybe_reasoning, str) and maybe_reasoning:
                    return maybe_reasoning
        return ""

    # T007/T008：簡化的串流封裝 + 重試/逾時 + 結構化日誌（start/finish/tokens/cost）
    def stream_complete(self, *, prompt: str, tier: Optional[str] = None, max_retries: int = 2) -> Generator[str, None, None]:
        """以簡化方式產生串流輸出（骨架）。

        - 採用指數退避策略於暫時性錯誤（此處以 RuntimeError 模擬）
        - 逾時秒數使用 settings.LLM_TIMEOUT_SECONDS（僅記錄，不做真實 I/O 逾時控制）
        - 回傳多段文字（此處僅拆分為 2 個 chunk 作為示例）
        - 完成時記錄 tokens/cost（估算）
        """
        start_ts = time.time()
        # 預設清空路由資訊；真實供應商/路徑會在各分支填入
        self._last_route = {}
        self._log.info("llm.stream.start", prompt_len=len(prompt), tier=tier or self._default_tier)
        attempt = 0
        while True:
            local_buf = ""
            try:
                used_langchain = False
                if self._lc_available:
                    try:
                        for chunk in self._stream_via_langchain(prompt=prompt, tier=tier):
                            used_langchain = True
                            local_buf += chunk
                            yield chunk
                    except Exception as e:
                        # LangChain 失敗則降級為本地切片，不丟出錯誤
                        self._log.warning("llm.langchain.fallback", error=str(e))
                if not used_langchain:
                    # 骨架：以本地字串切分模擬兩段串流
                    text = self.complete(prompt=prompt, tier=tier)
                    # 標記為本地降級路由
                    self._last_route = {
                        "tier": (tier or getattr(self, "_session_tier", None) or self._default_tier),
                        "provider": "",
                        "fallback": True,
                    }
                    mid = max(1, len(text) // 2)
                    part1, part2 = text[:mid], text[mid:]
                    local_buf = text
                    yield part1
                    yield part2
                # 結束：估算 tokens/cost 並記錄
                elapsed = time.time() - start_ts
                approx_tokens = max(1, len(local_buf) // 4)
                cost = 0.0  # 真實成本後續以供應商定價填入
                self._log.info(
                    "llm.stream.finish",
                    elapsed_ms=int(elapsed * 1000),
                    tokens={"input": 0, "output": approx_tokens, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                    cost=cost,
                )
                return
            except RuntimeError as e:  # 僅對可重試錯誤進行退避，否則仍降級
                if attempt >= max_retries:
                    # 最終降級為本地切片輸出而不是丟出
                    self._log.error("llm.stream.error.final_fallback", error=str(e), attempt=attempt)
                    text = self.complete(prompt=prompt, tier=tier)
                    self._last_route = {
                        "tier": (tier or getattr(self, "_session_tier", None) or self._default_tier),
                        "provider": "",
                        "fallback": True,
                    }
                    mid = max(1, len(text) // 2)
                    part1, part2 = text[:mid], text[mid:]
                    local_buf = text
                    yield part1
                    yield part2
                    elapsed = time.time() - start_ts
                    approx_tokens = max(1, len(local_buf) // 4)
                    self._log.info(
                        "llm.stream.finish",
                        elapsed_ms=int(elapsed * 1000),
                        tokens={"input": 0, "output": approx_tokens, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                        cost=0.0,
                    )
                    return
                backoff = 0.5 * (2 ** attempt)
                self._log.warning("llm.stream.retrying", error=str(e), attempt=attempt, backoff_s=backoff)
                time.sleep(backoff)
                attempt += 1

    # 嘗試偵測 langchain + 供應商是否可用
    def _detect_langchain(self) -> bool:
        try:
            import importlib
            # 任一模組可用即視為可嘗試（實際供應商與 tier 由環境/設定決定）
            importlib.import_module('langchain')
            # 供應商端以 lite/官方 SDK 視環境而定；此處僅檢測 langchain 本體
            return True
        except Exception:
            self._log.warning("langchain.not_available_fallback")
            return False

    def _stream_via_langchain(self, *, prompt: str, tier: Optional[str]) -> Generator[str, None, None]:
        """以 LangChain 嘗試真實串流（若環境可用）。失敗則丟擲以便上層重試/降級。"""
        from langchain_core.messages import HumanMessage

        provider_tier = (tier or self._session_tier or self._default_tier).lower()

        # Cloud：依供應商細分優先順序
        if provider_tier == "cloud":
            provider = (self._session_overrides.get("provider") or self._cloud_provider or "openai").lower()
            try:
                if provider == "openai":
                    from langchain_openai import ChatOpenAI
                    api_key = self._resolve_api_key("openai")
                    kwargs = {"temperature": 0, "streaming": True, "model": self._select_cloud_model()}
                    if api_key:
                        kwargs["api_key"] = api_key
                    llm = ChatOpenAI(**kwargs)
                    self._last_route = {"tier": "cloud", "provider": "openai", "model": kwargs.get("model")}
                    for chunk in llm.stream([HumanMessage(content=prompt)]):
                        text = self._extract_stream_text(chunk)
                        if text:
                            yield text
                    return
                elif provider == "anthropic":
                    from langchain_anthropic import ChatAnthropic
                    api_key = self._resolve_api_key("anthropic")
                    kwargs = {"temperature": 0, "model": self._select_cloud_model()}
                    if api_key:
                        kwargs["api_key"] = api_key
                    llm = ChatAnthropic(**kwargs)
                    self._last_route = {"tier": "cloud", "provider": "anthropic", "model": kwargs.get("model")}
                    for chunk in llm.stream([HumanMessage(content=prompt)]):
                        text = self._extract_stream_text(chunk)
                        if text:
                            yield text
                    return
                elif provider in {"google", "gemini", "google-gemini"}:
                    from langchain_google_genai import ChatGoogleGenerativeAI
                    api_key = self._resolve_api_key("gemini") or settings.GEMINI_API_KEY or None
                    kwargs = {"model": settings.GEMINI_MODEL, "temperature": 0}
                    if api_key:
                        kwargs["google_api_key"] = api_key
                    llm = ChatGoogleGenerativeAI(**kwargs)
                    self._last_route = {"tier": "cloud", "provider": "gemini", "model": kwargs.get("model")}
                    for chunk in llm.stream([HumanMessage(content=prompt)]):
                        text = self._extract_stream_text(chunk)
                        if text:
                            yield text
                    return
                elif provider in {"azure", "azure-openai"}:
                    from langchain_openai import AzureChatOpenAI
                    api_key = self._resolve_api_key("azure")
                    llm = AzureChatOpenAI(
                        azure_endpoint=(self._session_overrides.get("azure_endpoint") or self._session_overrides.get("base_url") or settings.AZURE_OPENAI_ENDPOINT or None),
                        api_key=api_key or None,
                        api_version=(self._session_overrides.get("azure_api_version") or settings.AZURE_OPENAI_API_VERSION or None),
                        azure_deployment=(self._session_overrides.get("azure_deployment") or settings.AZURE_OPENAI_DEPLOYMENT or None),
                        temperature=0,
                        streaming=True,
                    )
                    self._last_route = {"tier": "cloud", "provider": "azure", "deployment": settings.AZURE_OPENAI_DEPLOYMENT}
                    for chunk in llm.stream([HumanMessage(content=prompt)]):
                        text = self._extract_stream_text(chunk)
                        if text:
                            yield text
                    return
                elif provider == "openrouter":
                    from langchain_litellm import ChatLiteLLM
                    self._log.info("lc.cloud.stream.use", provider="openrouter", model=settings.OPENROUTER_MODEL)
                    with temp_env({
                        "OPENAI_API_BASE": (self._session_overrides.get("base_url") or settings.OPENROUTER_BASE_URL or "https://openrouter.ai/api/v1"),
                        "OPENAI_API_KEY": (self._session_overrides.get("api_key") or self._resolve_api_key("openrouter") or settings.OPENROUTER_API_KEY or ""),
                    }):
                        llm = ChatLiteLLM(model=settings.OPENROUTER_MODEL)
                        self._last_route = {"tier": "cloud", "provider": "openrouter", "model": settings.OPENROUTER_MODEL}
                        for chunk in llm.stream([HumanMessage(content=prompt)]):
                            text = self._extract_stream_text(chunk)
                            if text:
                                yield text
                    return
                elif provider in {"xai", "grok"}:
                    # Grok 走 ChatLiteLLM（需 XAI_API_KEY），用 temp_env 限縮作用域
                    from langchain_litellm import ChatLiteLLM
                    self._log.info("lc.cloud.stream.use", provider="grok", model=settings.GROK_MODEL)
                    with temp_env({
                        "OPENAI_API_KEY": (self._session_overrides.get("api_key") or self._resolve_api_key("grok") or settings.XAI_API_KEY or ""),
                    }):
                        llm = ChatLiteLLM(model=settings.GROK_MODEL)
                        self._last_route = {"tier": "cloud", "provider": "grok", "model": settings.GROK_MODEL}
                        for chunk in llm.stream([HumanMessage(content=prompt)]):
                            text = self._extract_stream_text(chunk)
                            if text:
                                yield text
                    return
            except Exception as e:
                self._log.warning("lc.cloud.stream.fallback", error=str(e), provider=provider)

        # On-prem：採 OpenAI 相容 API；透過環境變數 OPENAI_API_BASE 指向 vLLM/Ollama
        try:
            from langchain_litellm import ChatLiteLLM

            base = self._select_onprem_base()
            api_key = (self._session_overrides.get("api_key") or self._resolve_api_key("onprem") or settings.LITELLM_ONPREM_API_KEY or "none")
            model = (self._session_overrides.get("model") or settings.ONPREM_MODEL)
            # 若偵測為 Ollama 或常見 11434 端口，且未帶前綴，替換成 'ollama/<model>' 以提示 litellm provider
            prefixed_model = model
            onprem_provider = (self._session_overrides.get("onprem_provider") or settings.ONPREM_PROVIDER or "").lower()
            is_ollama_route = (onprem_provider == "ollama") or ("11434" in (base or ""))
            # Ollama 一律要求 model 帶 `ollama/` 前綴。
            # 例如：
            # - gemma3:4b -> ollama/gemma3:4b
            # - TwinkleAI/gemma-3-4B-T1-it:latest -> ollama/TwinkleAI/gemma-3-4B-T1-it:latest
            if is_ollama_route and not str(model).startswith("ollama/"):
                prefixed_model = f"ollama/{str(model).lstrip('/')}"
            self._log.info("lc.onprem.stream.use", base=os.environ.get("OPENAI_API_BASE", ""), model=prefixed_model)
            with temp_env({
                "OPENAI_API_BASE": base or "",
                "OPENAI_API_KEY": api_key,
            }):
                llm = ChatLiteLLM(model=prefixed_model)
                self._last_route = {"tier": "onprem", "provider": (self._session_overrides.get("onprem_provider") or settings.ONPREM_PROVIDER or ""), "model": prefixed_model, "base_hint": self._safe_base_hint(base or "")}
                for chunk in llm.stream([HumanMessage(content=prompt)]):
                    text = self._extract_stream_text(chunk)
                    if text:
                        yield text
            return
        except Exception as e:
            # 讓上層的重試/降級處理（將回退到本地切片策略）
            raise RuntimeError(str(e))

    def _select_cloud_model(self) -> str:
        # session override 模型優先
        override_model = self._session_overrides.get("model") if hasattr(self, "_session_overrides") else None
        if override_model:
            return override_model
        prov = (self._session_overrides.get("provider") or self._cloud_provider or "openai").lower()
        if prov == "openai":
            return settings.OPENAI_MODEL
        if prov == "anthropic":
            return settings.ANTHROPIC_MODEL
        if prov in {"google", "gemini", "google-gemini"}:
            return settings.GEMINI_MODEL
        if prov in {"xai", "grok"}:
            return settings.GROK_MODEL
        # 預設回退 openai
        return settings.OPENAI_MODEL

    def _select_onprem_base(self) -> str:
        # session override base/url/provider 優先
        base = None
        if hasattr(self, "_session_overrides"):
            base = self._session_overrides.get("onprem_base_url") or self._session_overrides.get("base_url")
        if base:
            return base
        prov = (self._session_overrides.get("onprem_provider") or settings.ONPREM_PROVIDER or "ollama").lower()
        if prov == "ollama":
            return settings.OLLAMA_BASE_URL or settings.LITELLM_ONPREM_BASE_URL
        if prov == "vllm":
            return settings.VLLM_BASE_URL or settings.LITELLM_ONPREM_BASE_URL
        return settings.LITELLM_ONPREM_BASE_URL

    # 結構化輸出：優先嘗試供應商原生/LC 路徑，失敗則回退最小可用物件
    def structured_output(self, *, prompt: str, schema: dict, tier: Optional[str] = None) -> dict:
        # 嘗試 OpenAI 的 json_schema response_format（若可用）
        try:
            if self._lc_available:
                provider_tier = (tier or self._default_tier).lower()
                if provider_tier == "cloud" and (self._session_overrides.get("provider") or self._cloud_provider or "openai").lower() == "openai":
                    from langchain_openai import ChatOpenAI
                    # 以 model_kwargs 傳遞 response_format；若不被支援將觸發例外
                    kwargs = {
                        "temperature": 0,
                        "model": self._select_cloud_model(),
                        "model_kwargs": {
                            "response_format": {
                                "type": "json_schema",
                                "json_schema": {
                                    "name": "structured",
                                    "schema": schema or {},
                                },
                            }
                        },
                    }
                    api_key = self._resolve_api_key("openai")
                    if api_key:
                        kwargs["api_key"] = api_key
                    llm = ChatOpenAI(**kwargs)
                    self._last_route = {"tier": "cloud", "provider": "openai", "model": kwargs.get("model"), "mode": "json_schema"}
                    msg = llm.invoke([{"role": "user", "content": prompt}])
                    import json
                    content = getattr(msg, "content", "")
                    if isinstance(content, list):
                        text = "".join(x for x in content if isinstance(x, str))
                    else:
                        text = content or ""
                    data = {}
                    if text:
                        try:
                            data = json.loads(text)
                        except Exception:
                            # 若服務端直接回傳結構已包在訊息中（有時 content 為 dict/list），嘗試以字典回傳
                            if isinstance(content, (dict, list)):
                                data = content  # type: ignore[assignment]
                    if isinstance(data, dict):
                        return data
        except Exception as e:
            self._log.warning("structured.openai.fallback", error=str(e))

        # 回退：最小可用結構
        return {
            "tool": "StructuredOutput",
            "ok": True,
            "summary": prompt[:200],
            "schema_keys": list(schema.keys()) if isinstance(schema, dict) else [],
        }

    def _resolve_api_key(self, provider: str) -> Optional[str]:
        # 先看 overrides.api_key；否則以 api_key_ref 經由 SecretsProvider 取得
        if hasattr(self, "_session_overrides"):
            direct = self._session_overrides.get("api_key")
            if direct:
                return str(direct)
            ref = self._session_overrides.get("api_key_ref")
            if ref:
                val = self._secrets.get(str(ref))
                if val:
                    return val
        # 回退：使用系統層級環境設定
        p = (provider or "").lower()
        if p == "openai":
            return settings.OPENAI_API_KEY or None
        if p == "anthropic":
            return settings.ANTHROPIC_API_KEY or None
        if p in {"google", "gemini", "google-gemini"}:
            return settings.GEMINI_API_KEY or None
        if p in {"xai", "grok"}:
            return settings.XAI_API_KEY or None
        if p in {"azure", "azure-openai"}:
            return settings.AZURE_OPENAI_API_KEY or None
        if p == "openrouter":
            return settings.OPENROUTER_API_KEY or None
        if p == "onprem":
            return settings.LITELLM_ONPREM_API_KEY or None
        return None

    def last_route_info(self) -> dict:
        info = dict(self._last_route) if isinstance(self._last_route, dict) else {}
        # 標示是否為 local slicing 降級（若沒有任何 lc.use 路由記錄且 stream.finish 仍完成）
        return info

    def _safe_base_hint(self, url: str) -> str:
        try:
            u = urlparse(url)
            host = u.hostname or ""
            port = f":{u.port}" if u.port else ""
            scheme = u.scheme or ""
            if scheme and host:
                return f"{scheme}://{host}{port}"
            return host or url[:32]
        except Exception:
            return url[:32]

    def health_check(self, mode: str = "soft") -> dict:
        """基本健康檢查。
        - soft：不做昂貴外呼；檢查關鍵參數是否存在
        - hard：對 on‑prem 進行 GET /api/tags（ollama）或 /v1/models（vllm），對 openai 嘗試 GET /v1/models
        """
        try:
            tier = (getattr(self, "_session_tier", None) or self._default_tier or "").lower()
            provider = (self._session_overrides.get("provider") or self._cloud_provider or "").lower() if hasattr(self, "_session_overrides") else (self._cloud_provider or "").lower()
            if tier == "onprem":
                base = self._select_onprem_base()
                if not base:
                    return {"ok": False, "error": "missing onprem base url"}
                if mode == "soft":
                    return {"ok": True, "tier": tier, "base_hint": self._safe_base_hint(base)}
                # hard
                path = "/api/tags" if ((self._session_overrides.get("onprem_provider") or settings.ONPREM_PROVIDER or "").lower() == "ollama") else "/v1/models"
                url = (base or '').rstrip("/") + path
                try:
                    r = httpx.get(url, timeout=5.0)
                    return {"ok": r.status_code in (200, 204), "status": r.status_code, "tier": tier, "base_hint": self._safe_base_hint(base or '')}
                except Exception as e:
                    return {"ok": False, "error": str(e), "tier": tier, "base_hint": self._safe_base_hint(base or '')}
            # cloud
            if mode == "soft":
                # openai 檢查金鑰是否存在
                if provider == "openai":
                    ok = bool(self._resolve_api_key("openai") or settings.OPENAI_API_KEY)
                    return {"ok": ok, "tier": "cloud", "provider": provider}
                return {"ok": True, "tier": "cloud", "provider": provider}
            # hard：嘗試 OpenAI models 列表
            if provider == "openai":
                key = self._resolve_api_key("openai") or settings.OPENAI_API_KEY
                if not key:
                    return {"ok": False, "error": "missing OPENAI_API_KEY"}
                try:
                    r = httpx.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=5.0)
                    return {"ok": r.status_code == 200, "status": r.status_code, "provider": provider, "tier": "cloud"}
                except Exception as e:
                    return {"ok": False, "error": str(e), "provider": provider, "tier": "cloud"}
            return {"ok": False, "error": f"hard check for {provider} not implemented", "tier": "cloud", "provider": provider}
        except Exception as e:
            return {"ok": False, "error": str(e)}
