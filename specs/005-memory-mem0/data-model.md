# Data Model: 多代理雙層記憶（Mem0）

## 1. 邏輯實體

### 1.1 MemoryScope

- `scope_type`: `user_scope | agent_scope | interaction_scope | global_scope`
- `user_id`: 可為空（agent/global 時）
- `agent_id`: 可為空（純 user/global 時）
- `run_id`: 對話/流程識別（對應 conversation_id）
- `app_id`: 應用識別（建議固定 `dyagent`）

約束：
- `user_scope` 必須有 `user_id`
- `agent_scope` 必須有 `agent_id`
- `interaction_scope` 必須同時有 `user_id` 與 `agent_id`

### 1.2 MemoryQueryContext

- `query`: 當前使用者問題
- `top_k`: 檢索數量上限
- `scope_filters`: 由 MemoryScope 生成之條件
- `max_chars`: 注入 prompt 的最大長度

### 1.3 MemorySnippet

- `text`: 記憶內容
- `score`: 相似度分數
- `scope_type`: 來源範疇
- `source`: `mem0`
- `metadata`: 原始附帶資訊

### 1.4 MemoryWritePayload

- `messages`: `[{role, content}, ...]`
- `write_reason`: `user_preference | agent_experience | interaction_preference | generic_dialogue`
- `scope`: 對應 MemoryScope
- `metadata`: 追蹤欄位（如 conversation_id, route_reason）

### 1.5 ShortTermSessionState（Redis）

- `redis_key`: `short_term:user:{user_id}:agent:{agent_id}`
- `messages`: 最近 N 則 user/assistant 對話
- `ttl_sec`: 預設 3600

## 2. 現有資料對映

- `User.id` -> `user_id`
- `Agent.id` -> `agent_id`
- `Conversation.id` -> `run_id`
- `settings.PROJECT_NAME` or 固定值 -> `app_id`

## 3. 持久層策略

- PostgreSQL：不新增主資料表（本期以既有模型與事件紀錄為主）。
- Redis：新增短期記憶 key 規範與讀寫 helper。
- Qdrant：由 Mem0 使用指定 collection（或依 provider 配置）。

## 4. 可觀測資料（沿用 EventPart）

新增事件型別（payload schema）：

- `memory.retrieve`
  - `ok: bool`
  - `user_id: str`
  - `agent_id: str`
  - `top_k: int`
  - `hit_count: int`
  - `latency_ms: int`
  - `error: str | null`

- `memory.write`
  - `ok: bool`
  - `write_reason: str`
  - `scope_type: str`
  - `latency_ms: int`
  - `error: str | null`

## 5. 資料刪除（合規）

- `forget_user(user_id)`：刪除 user_scope 與 interaction_scope 下該 user 的長期記憶。
- Redis 同步清除：刪除 `short_term:user:{user_id}:*` 鍵。
