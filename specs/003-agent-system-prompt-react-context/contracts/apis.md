# API 契約草案

## A. Functions（全域）

### GET /api/functions

回傳可用 Function Profiles 清單。

### POST /api/functions

建立 Function Profile。

權限：`admin`

Request
```json
{
  "name": "generic-strict",
  "provider": "generic",
  "template": "...",
  "description": "...",
  "enabled": true
}
```

### PUT /api/functions/{id}

權限：`admin`

### DELETE /api/functions/{id}

權限：`admin`

### GET /api/functions/selectable

用途：給代理者設定頁可綁定的清單。

權限：`read_agent` 或 `update_agent`

## B. Agent Prompt 與 Function 綁定

### GET /api/agents/{id}/prompt

```json
{
  "agent_id": "uuid",
  "system_prompt": "...",
  "source": "agent|model_config|description|default"
}
```

### PUT /api/agents/{id}/prompt

```json
{
  "system_prompt": "..."
}
```

### PUT /api/agents/{id}/function-profile

```json
{
  "function_profile_id": "uuid",
  "custom_function_guide": "optional"
}
```

權限：`agent_admin` / `admin`

## C. RAG 資料集

### GET /api/rag/datasets?scope=global|agent_private&agent_id=...

查詢資料集。

### POST /api/rag/datasets

建立公有資料集。

權限：`admin`

### POST /api/agents/{id}/rag/datasets

建立代理者私有資料集（`scope=agent_private`）。

權限：`agent_admin` / `admin`

### PUT /api/agents/{id}/rag/bindings

```json
{
  "global_dataset_ids": ["uuid"],
  "private_dataset_ids": ["uuid"]
}
```

## D. Router 統一入口

### POST /api/chat

用途：一般使用者聊天入口，不需選代理者。

```json
{
  "message": "我要請假，請幫我處理"
}
```

回應為 SSE 或標準 JSON（依既有系統選型，建議 SSE 一致化）。

## E. 路由審計（管理用途）

### GET /api/conversations/{id}/routing-events

回傳 `route.decision` / `route.forward` / `route.fallback` 事件。
