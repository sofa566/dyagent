# API Contracts: 記憶管理與可觀測

## 1) 記憶健康檢查

`GET /api/memory/health`

### Response 200

```json
{
  "ok": true,
  "provider": "mem0_oss",
  "read_enabled": true,
  "write_enabled": true,
  "degraded": false,
  "error": null
}
```

### Response 503

```json
{
  "ok": false,
  "provider": "mem0_oss",
  "degraded": true,
  "error": "memory_provider_unavailable"
}
```

## 2) 記憶搜尋（管理/除錯）

`POST /api/memory/search`

### Request

```json
{
  "query": "使用者偏好",
  "user_id": "<uuid>",
  "agent_id": "<uuid>",
  "scope_mode": "hybrid",
  "top_k": 5
}
```

### Response 200

```json
{
  "ok": true,
  "results": [
    {
      "text": "使用者偏好繁體中文且回答精簡",
      "score": 0.91,
      "scope_type": "user_scope",
      "source": "mem0"
    }
  ],
  "count": 1
}
```

## 3) 忘記我（刪除指定 user 記憶）

`POST /api/memory/users/{user_id}/forget`

### Response 200

```json
{
  "ok": true,
  "user_id": "<uuid>",
  "deleted_long_term": true,
  "deleted_short_term": true
}
```

## 4) 聊天流程整合（既有 API 擴充）

`GET /api/agents/{agent_id}/chat/stream`

### 行為契約（新增）

- 系統在回覆前嘗試檢索記憶並組裝 `memory_context`。
- 系統在回覆後嘗試寫入長期記憶與短期記憶。
- 記憶失敗時仍需回傳正常聊天 SSE。

### SSE 擴充事件

`memory.retrieve`:

```json
{
  "type": "memory.retrieve",
  "ok": true,
  "hit_count": 3,
  "latency_ms": 42
}
```

`memory.write`:

```json
{
  "type": "memory.write",
  "ok": true,
  "scope_type": "interaction_scope",
  "write_reason": "interaction_preference",
  "latency_ms": 35
}
```
