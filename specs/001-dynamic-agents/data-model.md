# Data Model: 動態代理者創建系統

**Date**: 2026-03-09

## Entities

### Agent (代理者)

代表一個 AI 代理實體。

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| id | UUID | 是 | 唯一識別碼 |
| name | string | 是 | 代理者名稱 (1-100 字) |
| description | string | 否 | 功能描述 (0-1000 字) |
| model_type | enum | 是 | 模型類型: `local` / `cloud` |
| model_config | object | 是 | 模型連線配置 |
| mcp_config | object | 否 | MCP server 配置 |
| skills | array | 否 | 技能清單 |
| tools | array | 否 | 工具清單 |
| rag_config | object | 否 | RAG 知識庫配置 |
| workspace_id | UUID | 是 | 所屬工作區 ID |
| created_at | datetime | 是 | 建立時間 |
| updated_at | datetime | 是 | 更新時間 |

**Model Config 結構**:
```json
{
  "provider": "openai / anthropic / ollama / local",
  "model_name": "gpt-4 / claude-3 / llama2",
  "api_endpoint": "https://api.example.com/v1",
  "api_key": "sk-..."
}
```

**MCP Config 結構**:
```json
{
  "server_url": "http://localhost:3000",
  "auth_token": "...",
  "tools": ["tool1", "tool2"]
}
```

**RAG Config 結構**:
```json
{
  "vector_store": "chroma / faiss",
  "chunk_size": 1000,
  "chunk_overlap": 200,
  "embeddings_model": "text-embedding-ada-002"
}
```

---

### Workspace (工作區)

代理者的獨立執行環境。

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| id | UUID | 是 | 唯一識別碼 |
| name | string | 是 | 工作區名稱 |
| created_at | datetime | 是 | 建立時間 |

---

### Conversation (對話)

使用者與代理者之間的互動記錄。

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| id | UUID | 是 | 唯一識別碼 |
| agent_id | UUID | 是 | 所屬代理者 ID |
| created_at | datetime | 是 | 建立時間 |

---

### Message (訊息)

對話中的單一訊息。

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| id | UUID | 是 | 唯一識別碼 |
| conversation_id | UUID | 是 | 所屬對話 ID |
| role | enum | 是 | 角色: `user` / `assistant` |
| content | string | 是 | 訊息內容 |
| timestamp | datetime | 是 | 時間戳記 |

---

### Document (文檔)

RAG 知識庫中的文件。

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| id | UUID | 是 | 唯一識別碼 |
| agent_id | UUID | 是 | 所屬代理者 ID |
| filename | string | 是 | 原始檔案名稱 |
| file_path | string | 是 | 儲存路徑 |
| file_type | string | 是 | 檔案類型 |
| uploaded_at | datetime | 是 | 上傳時間 |

---

### User (使用者)

系統使用者帳號。

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| id | UUID | 是 | 唯一識別碼 |
| username | string | 是 | 使用者名稱 (唯一) |
| email | string | 是 | 電子郵件 (唯一) |
| password_hash | string | 是 | 密碼雜湊 |
| role | enum | 是 | 角色: `admin` / `agent_admin` / `user` |
| created_at | datetime | 是 | 建立時間 |
| updated_at | datetime | 是 | 更新時間 |

---

### Role (角色)

使用者權限角色定義。

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| id | UUID | 是 | 唯一識別碼 |
| name | enum | 是 | 角色名稱: `admin` / `agent_admin` / `user` |
| permissions | array | 是 | 權限清單 |

**權限矩陣**:
| 角色 | 建立代理者 | 刪除代理者 | 修改代理者 | 聊天 | 查看日誌 |
|------|-----------|-----------|-----------|------|----------|
| admin | ✓ | ✓ | ✓ | ✓ | ✓ |
| agent_admin | ✗ | ✗ | ✓ | ✓ | ✗ |
| user | ✗ | ✗ | ✗ | ✓ | ✗ |

---

### Log (日誌)

系統操作日誌記錄。

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| id | UUID | 是 | 唯一識別碼 |
| user_id | UUID | 是 | 操作者 ID |
| level | enum | 是 | 日誌級別: `debug` / `info` / `warning` / `error` |
| action | string | 是 | 操作類型 |
| resource_type | string | 是 | 資源類型 |
| resource_id | UUID | 是 | 資源 ID |
| details | object | 否 | 詳細資訊 |
| ip_address | string | 否 | 客戶端 IP |
| timestamp | datetime | 是 | 時間戳記 |

---

## Relationships

```
Workspace (1) ──< Agent (N)
Agent (1) ──< Conversation (N)
Conversation (1) ──< Message (N)
Agent (1) ──< Document (N)
User (1) ──< Role (N)
User (1) ──< Log (N)
```

---

## Validation Rules

- **Agent.name**: 1-100 characters, not empty
- **Agent.description**: 0-1000 characters
- **Message.content**: 1-10000 characters
- **Workspace.name**: 1-50 characters, not empty

---

## State Transitions

### Agent Lifecycle

```
Created → Active → (Deleted)
```

### Conversation Lifecycle

```
Created → Active → Archived
```

---

## Index Recommendations

- `agents.workspace_id` - 查詢工作區內的代理者
- `conversations.agent_id` - 查詢代理者的對話歷史
- `messages.conversation_id` - 查詢對話的訊息
- `documents.agent_id` - 查詢代理者的知識庫文件
