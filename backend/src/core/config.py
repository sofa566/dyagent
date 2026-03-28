from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from typing import Optional
import logging


class Settings(BaseSettings):
    PROJECT_NAME: str = 'dyagent'
    VERSION: str = '0.1.0'
    DEBUG: bool = True

    DATABASE_URL: str = 'postgresql://user:password@localhost:5432/dyagent'

    REDIS_URL: str = 'redis://localhost:6379'

    QDRANT_URL: str = 'http://localhost:6333'

    SECRET_KEY: str = 'your-secret-key-change-in-production'
    ALGORITHM: str = 'HS256'
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    OPENAI_API_KEY: str = ''
    ANTHROPIC_API_KEY: str = ''
    GEMINI_API_KEY: str = ''  # Google Gemini
    XAI_API_KEY: str = ''     # Grok (xAI)
    # Azure OpenAI
    AZURE_OPENAI_API_KEY: str = ''
    AZURE_OPENAI_ENDPOINT: str = ''
    AZURE_OPENAI_API_VERSION: str = '2024-02-15-preview'
    AZURE_OPENAI_DEPLOYMENT: str = 'gpt-4o'
    # OpenRouter
    OPENROUTER_API_KEY: str = ''
    OPENROUTER_BASE_URL: str = 'https://openrouter.ai/api/v1'

    # LLM / LiteLLM（langchain-litellm）設定
    # 說明：
    # - LITELLM_CLOUD_PROVIDER：雲端提供者鍵（例如 openai、azure、openrouter）
    # - LITELLM_ONPREM_BASE_URL：地端推理服務的 API base_url（例如 http://localhost:8000/v1）
    # - LLM_DEFAULT_TIER：預設路由層級（cloud | onprem）
    # - LLM_TIMEOUT_SECONDS：LLM 逾時秒數（整數）
    LITELLM_CLOUD_PROVIDER: str = 'openai'
    LITELLM_ONPREM_BASE_URL: str = ''
    LLM_DEFAULT_TIER: str = 'cloud'
    LLM_TIMEOUT_SECONDS: int = 60
    # Doom loop 防護門檻（連續相同工具+輸入觸發）
    DOOM_LOOP_THRESHOLD: int = 3
    # 無可用模型路由時是否改為「硬失敗」：
    # False = 回傳骨架佔位文字（開發期不中斷；預設值以維持測試穩定）
    # True  = 回傳 503 並不寫入助理訊息
    LLM_HARD_FAIL_ON_NO_ROUTE: bool = False
    # MCP 連線測試/呼叫逾時（毫秒）
    MCP_HTTP_TIMEOUT_MS: int = 4000
    # MCP 白名單（以逗號分隔的 base_url 列表），供 /mcp/servers 顯示
    MCP_WHITELIST: str = ""
    # MCP WebSocket JSON-RPC 端點（相對路徑），如服務支援 ws(s)
    MCP_WS_PATH: str = "/ws"
    # 是否在提示中注入工具呼叫協定指引
    LLM_TOOLCALL_GUIDE: bool = True
    # ReAct 合成層設定
    REACT_MAX_STEPS: int = 3
    REACT_MAX_OBSERVATION_CHARS: int = 4000
    # LLM 成本估算表（JSON）
    # 格式：{"provider:model": {"input_per_1k": 0.005, "output_per_1k": 0.015}}
    LLM_COST_TABLE_JSON: str = ''

    # Embedding 設定
    # provider: deterministic | sentence_transformers | ollama | vllm
    EMBEDDING_PROVIDER: str = 'deterministic'
    EMBEDDING_MODEL_NAME: str = 'BAAI/bge-m3'
    EMBEDDING_OLLAMA_BASE_URL: str = 'http://localhost:11434'
    EMBEDDING_VLLM_BASE_URL: str = 'http://localhost:8000/v1'

    # Router 路由門檻（0~1）
    ROUTER_EMBEDDING_THRESHOLD: float = 0.55

    # 預設模型（依供應商與地端引擎）
    OPENAI_MODEL: str = 'gpt-4o'
    ANTHROPIC_MODEL: str = 'claude-3-5-sonnet-20240620'
    GEMINI_MODEL: str = 'gemini-1.5-pro'
    GROK_MODEL: str = 'grok-2'
    OPENROUTER_MODEL: str = 'gpt-4o'

    ONPREM_PROVIDER: str = 'ollama'  # ollama | vllm
    ONPREM_MODEL: str = 'qwen2.5:7b'
    OLLAMA_BASE_URL: str = ''   # 例如 http://localhost:11434
    VLLM_BASE_URL: str = ''     # 例如 http://localhost:8000/v1
    LITELLM_ONPREM_API_KEY: str = 'none'  # 如需 OpenAI 相容 API，可用佔位鍵

    # 資料快取目錄（ZIP 解壓縮、暫存檔）
    DATA_CACHE_PATH: str = '/tmp/dyagent-cache'

    # Secrets provider 設定
    # env | vault | aws | gcp | k8s
    SECRETS_PROVIDER: str = 'env'
    # 預留連線設定（實際實作可替換為對應 SDK），此處僅作為占位
    VAULT_ADDR: str = ''
    VAULT_TOKEN: str = ''
    VAULT_PATH_PREFIX: str = ''
    AWS_REGION: str = ''
    GCP_PROJECT_ID: str = ''
    K8S_NAMESPACE: str = ''

    # Pydantic v2 設定（取代舊式 class Config）
    model_config = SettingsConfigDict(env_file='.env', case_sensitive=True, extra='allow')


@lru_cache()
def get_settings():
    return Settings()


settings = get_settings()


def validate_llm_settings() -> None:
    """檢查 LLM 相關設定並記錄提示。

    - 若同時缺少雲端與地端端點，僅記錄警告（開發環境允許）
    - 檢查 LLM_DEFAULT_TIER 是否為 cloud/onprem
    - 確保逾時為正整數
    """
    # 注意：此處避免引用 src.core.logging 以免與 settings 形成循環依賴
    log = logging.getLogger("config")
    cloud = settings.LITELLM_CLOUD_PROVIDER
    onprem = settings.LITELLM_ONPREM_BASE_URL
    tier = (settings.LLM_DEFAULT_TIER or "").lower()
    timeout = settings.LLM_TIMEOUT_SECONDS

    if not cloud and not onprem:
        log.warning("llm.config.missing_providers: 未設定雲端與地端提供者，僅能回傳骨架輸出")
    if tier not in {"cloud", "onprem"}:
        log.warning("llm.config.invalid_default_tier: value=%s expect=%s", tier, "cloud|onprem")
    if not isinstance(timeout, int) or timeout <= 0:
        log.warning("llm.config.invalid_timeout: value=%s expect=>0 int", timeout)
    if settings.DOOM_LOOP_THRESHOLD <= 0:
        log.warning("config.invalid_doom_loop_threshold: value=%s expect=>0 int", settings.DOOM_LOOP_THRESHOLD)
    if settings.REACT_MAX_STEPS <= 0:
        log.warning("config.invalid_react_max_steps: value=%s expect=>0 int", settings.REACT_MAX_STEPS)
    if settings.REACT_MAX_OBSERVATION_CHARS < 500:
        log.warning("config.invalid_react_max_observation_chars: value=%s expect=>=500 int", settings.REACT_MAX_OBSERVATION_CHARS)
