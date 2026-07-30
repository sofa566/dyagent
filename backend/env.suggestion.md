# 「最穩開發模式」的 .env 建議值（優先能跑、能測流程、不容易卡依賴）
請把 backend/.env 相關段落調整成這樣（重複項目只留一份）：
```env
# --- 基本 ---
PROJECT_NAME=dyagent
VERSION=0.1.0
DEBUG=true

DATABASE_URL=postgresql://user:password@localhost:5432/dyagent
REDIS_URL=redis://localhost:6379
QDRANT_URL=http://localhost:6333

SECRET_KEY=change-this-to-a-random-string
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# --- LLM 路由（只用 OpenAI）---
LITELLM_CLOUD_PROVIDER=openai
LLM_DEFAULT_TIER=cloud
OPENAI_API_KEY=你的_openai_api_key
OPENAI_MODEL=gpt-4o-mini

# 沒路由時不要硬失敗，避免測試中斷
LLM_HARD_FAIL_ON_NO_ROUTE=false

# --- 記憶層（穩定優先）---
# 先用 mock，完整流程可測、不依賴 mem0/向量設定
AGENT_MEMORY_PROVIDER=mock
AGENT_MEMORY_ROUTING_MODE=mem_hybrid
ROUTER_ASSIGNMENT_MODE=hybrid
AGENT_MEMORY_READ_ENABLED=true
AGENT_MEMORY_WRITE_ENABLED=true
AGENT_MEMORY_TOP_K=5
AGENT_MEMORY_APP_ID=dyagent
AGENT_MEMORY_WRITE_POLICY=user_assistant_pair

# --- 短期記憶 ---
SHORT_TERM_MEMORY_TTL_SEC=3600
SHORT_TERM_MEMORY_MAX_MESSAGES=10

# --- Embedding（穩定優先）---
# 只留一個，避免你現在那種前後覆寫
EMBEDDING_PROVIDER=deterministic
EMBEDDING_MODEL_NAME=BAAI/bge-m3

# --- 聊天歷史 ---
CHAT_HISTORY_MODE=recent
CHAT_HISTORY_MAX_MESSAGES=5
CHAT_HISTORY_MAX_TOKENS=1000
CHAT_HISTORY_INCLUDE_TOOL_TEXT=false

# --- 其他 ---
ROUTER_EMBEDDING_THRESHOLD=0.55
NO_COLOR=1
```

特別注意兩點：
- 把 .env 裡重複的 EMBEDDING_PROVIDER 刪到只剩一個（你現在同時有 deterministic 跟 sentence_transformers）
- 暫時不要用 mem0_oss，先 AGENT_MEMORY_PROVIDER=mock 最穩；等流程跑順再切 mem0_oss

啟動前建議：
```bash
pip install -r backend/requirements.txt
```
然後啟動前後台測試即可。

# 「切換到 mem0_oss（只靠 OpenAI key）」的第二版 .env

```bash
# --- OpenAI ---
OPENAI_API_KEY=你的_openai_api_key
OPENAI_MODEL=gpt-4o-mini
LITELLM_CLOUD_PROVIDER=openai
LLM_DEFAULT_TIER=cloud
LLM_HARD_FAIL_ON_NO_ROUTE=false

# --- 記憶層：Mem0 OSS ---
AGENT_MEMORY_PROVIDER=mem0_oss
AGENT_MEMORY_ROUTING_MODE=mem_hybrid
ROUTER_ASSIGNMENT_MODE=hybrid
AGENT_MEMORY_READ_ENABLED=true
AGENT_MEMORY_WRITE_ENABLED=true
AGENT_MEMORY_TOP_K=5
AGENT_MEMORY_APP_ID=dyagent
AGENT_MEMORY_WRITE_POLICY=user_assistant_pair

# --- 短期記憶 ---
SHORT_TERM_MEMORY_TTL_SEC=3600
SHORT_TERM_MEMORY_MAX_MESSAGES=10

# --- Mem0 OSS: Qdrant ---
MEM0_VECTOR_PROVIDER=qdrant
MEM0_QDRANT_HOST=localhost
MEM0_QDRANT_PORT=6333
MEM0_QDRANT_COLLECTION=dyagent_long_term_memories

# --- Mem0 OSS: LLM / Embedder 都走 OpenAI ---
MEM0_LLM_PROVIDER=openai
MEM0_LLM_API_BASE=
MEM0_LLM_MODEL=gpt-4o-mini
MEM0_LLM_API_KEY=你的_openai_api_key

MEM0_EMBEDDER_PROVIDER=openai
MEM0_EMBEDDER_API_BASE=
MEM0_EMBEDDER_MODEL=text-embedding-3-small
MEM0_EMBEDDER_API_KEY=你的_openai_api_key

# --- Embedding（主系統）---
EMBEDDING_PROVIDER=deterministic
EMBEDDING_MODEL_NAME=BAAI/bge-m3
```

啟用前確認：
#### 1. postgres / redis / qdrant 都有啟動
#### 2. .env 只保留一組 EMBEDDING_PROVIDER（不要重複）
#### 3. 安裝依賴：
```bash
python -m pip install -r backend/requirements.txt
```
快速自檢：
```bash
python -c "from mem0 import Memory, MemoryClient; print('mem0 ok')"
```
# 最小驗證腳本（mem0 版，5 步）

## 1) 啟動服務
- 後端（backend/）：
```bash
uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
```
- 前端（frontend/）：
```bash
npm run dev
```
## 2) 登入拿 token（先用現有管理員）
```bash
curl -s -X POST "http://localhost:8000/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'
```
把回傳的 access_token 存成 shell 變數：
```bash
TOKEN="貼上_access_token"
```

## 3) 檢查記憶健康
```bash
curl -s "http://localhost:8000/api/memory/health" \
  -H "Authorization: Bearer $TOKEN"
```
#### 預期重點：
- provider 是 mem0_oss
- read_enabled=true
- write_enabled=true
- ok / degraded 會反映當前狀態

## 4) 觸發一次聊天寫入記憶
先拿一個可用 agent id（如果你前端已有可略過）：
```bash
curl -s "http://localhost:8000/api/agents" -H "Authorization: Bearer $TOKEN"
```
假設 AGENT_ID=...，呼叫一次 chat stream：
```bash
curl -N "http://localhost:8000/api/agents/$AGENT_ID/chat/stream?message=%E6%88%91%E5%81%8F%E5%A5%BD%E7%94%A8%E7%B9%81%E9%AB%94%E4%B8%AD%E6%96%87%E4%B8%A6%E5%85%88%E7%B5%A6%E6%A2%9D%E5%88%97%E6%91%98%E8%A6%81" \
  -H "Authorization: Bearer $TOKEN"
```
#### 預期：SSE 最後有 type":"done"。

## 5) 用管理搜尋 API 驗證是否可檢索
```bash
USER_ID=$(python - <<'PY'
import jwt, os
t=os.environ.get("TOKEN","")
print(jwt.decode(t, options={"verify_signature": False}).get("sub",""))
PY
)

curl -s -X POST "http://localhost:8000/api/memory/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"繁體中文\",\"user_id\":\"$USER_ID\",\"top_k\":5}"

```
#### 預期：

snippets 至少 1 筆（若沒有，通常是 mem0 llm/embedder 設定或外部連線問題）。

---
