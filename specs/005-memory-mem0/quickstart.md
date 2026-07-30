# Quickstart: 多代理雙層記憶（Mem0）

## 1. 安裝依賴

```bash
cd backend
pip install -r requirements.txt
```

## 2. 設定環境變數（範例）

```env
# 記憶總開關與供應器
AGENT_MEMORY_PROVIDER=mem0_oss
AGENT_MEMORY_ROUTING_MODE=hybrid
AGENT_MEMORY_READ_ENABLED=true
AGENT_MEMORY_WRITE_ENABLED=true
AGENT_MEMORY_TOP_K=5
AGENT_MEMORY_APP_ID=dyagent

# Mem0 OSS + Qdrant
MEM0_VECTOR_PROVIDER=qdrant
MEM0_QDRANT_HOST=localhost
MEM0_QDRANT_PORT=6333
MEM0_QDRANT_COLLECTION=dyagent_long_term_memories

# Mem0 所需 LLM/Embedding（可接既有 OpenAI 相容端點）
MEM0_LLM_PROVIDER=openai
MEM0_LLM_API_BASE=
MEM0_LLM_MODEL=
MEM0_LLM_API_KEY=

MEM0_EMBEDDER_PROVIDER=openai
MEM0_EMBEDDER_API_BASE=
MEM0_EMBEDDER_MODEL=
MEM0_EMBEDDER_API_KEY=

# 短期記憶
SHORT_TERM_MEMORY_TTL_SEC=3600
SHORT_TERM_MEMORY_MAX_MESSAGES=10
```

## 3. 啟動服務

```bash
./start-backend.sh
```

## 4. 驗證流程

1. 用同一帳號向代理 A 說明偏好（例如「回覆要精簡」）。
2. 再送一個請求讓 Master 分派到代理 B。
3. 確認代理 B 仍遵守該偏好。
4. 人為中斷 Mem0 或設定錯誤，確認聊天仍可回覆（降級）。

## 5. 驗證忘記我

1. 呼叫 `POST /api/memory/users/{user_id}/forget`。
2. 再次提問，確認先前長期偏好不再被注入。
