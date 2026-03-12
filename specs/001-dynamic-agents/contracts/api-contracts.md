# API Contracts: 動態代理者創建系統

**Date**: 2026-03-09

## Overview

本文件定義系統的 API 合約。所有 API 使用 JSON 格式進行請求和回應。

---

## 代理者管理 API

### 取得代理者列表

```
GET /api/agents
```

**Response 200**:
```json
{
  "agents": [
    {
      "id": "uuid",
      "name": "客服助手",
      "description": "回答產品相關問題",
      "model_type": "cloud",
      "created_at": "2026-03-09T10:00:00Z",
      "updated_at": "2026-03-09T10:00:00Z"
    }
  ]
}
```

---

### 創建代理者

```
POST /api/agents
```

**Request Body**:
```json
{
  "name": "客服助手",
  "description": "回答產品相關問題",
  "model_type": "cloud",
  "model_config": {
    "provider": "openai",
    "model_name": "gpt-4"
  }
}
```

**Response 201**:
```json
{
  "id": "uuid",
  "name": "客服助手",
  "description": "回答產品相關問題",
  "model_type": "cloud",
  "model_config": { ... },
  "created_at": "2026-03-09T10:00:00Z",
  "updated_at": "2026-03-09T10:00:00Z"
}
```

**Response 400**:
```json
{
  "error": "名稱為必填欄位"
}
```

---

### 取得代理者詳情

```
GET /api/agents/{id}
```

**Response 200**:
```json
{
  "id": "uuid",
  "name": "客服助手",
  "description": "回答產品相關問題",
  "model_type": "cloud",
  "model_config": { ... },
  "mcp_config": { ... },
  "skills": [],
  "tools": [],
  "rag_config": { ... },
  "workspace_id": "uuid",
  "created_at": "2026-03-09T10:00:00Z",
  "updated_at": "2026-03-09T10:00:00Z"
}
```

**Response 404**:
```json
{
  "error": "代理者不存在"
}
```

---

### 更新代理者

```
PUT /api/agents/{id}
```

**Request Body**:
```json
{
  "name": "新名稱",
  "description": "新描述",
  "model_config": { ... }
}
```

**Response 200**:
```json
{
  "id": "uuid",
  "name": "新名稱",
  ...
}
```

---

### 刪除代理者

```
DELETE /api/agents/{id}
```

**Response 204**: No Content

---

## 聊天 API

### 發送訊息

```
POST /api/agents/{id}/chat
```

**Request Body**:
```json
{
  "message": "你好，請問你能做什麼？"
}
```

**Response 200**:
```json
{
  "response": "您好！我是客服助手，我可以回答產品相關問題...",
  "conversation_id": "uuid"
}
```

**Response 400**:
```json
{
  "error": "訊息內容不能為空"
}
```

---

### 取得對話歷史

```
GET /api/agents/{id}/conversations
```

**Response 200**:
```json
{
  "conversations": [
    {
      "id": "uuid",
      "agent_id": "uuid",
      "messages": [
        {
          "id": "uuid",
          "role": "user",
          "content": "你好",
          "timestamp": "2026-03-09T10:00:00Z"
        },
        {
          "id": "uuid",
          "role": "assistant",
          "content": "您好！",
          "timestamp": "2026-03-09T10:00:01Z"
        }
      ],
      "created_at": "2026-03-09T10:00:00Z"
    }
  ]
}
```

---

## MCP API

### 取得可用 MCP Servers

```
GET /api/mcp/servers
```

**Response 200**:
```json
{
  "servers": [
    {
      "id": "uuid",
      "name": "File System MCP",
      "url": "http://localhost:3000",
      "status": "connected"
    }
  ]
}
```

---

### 連接 MCP Server

```
POST /api/mcp/connect
```

**Request Body**:
```json
{
  "server_url": "http://localhost:3000",
  "auth_token": "..."
}
```

**Response 200**:
```json
{
  "server_id": "uuid",
  "status": "connected",
  "available_tools": ["read_file", "write_file"]
}
```

---

### 取得可用工具

```
GET /api/mcp/tools?server_id={server_id}
```

**Response 200**:
```json
{
  "tools": [
    {
      "name": "read_file",
      "description": "讀取檔案內容",
      "parameters": {
        "path": "string"
      }
    }
  ]
}
```

---

## RAG API

### 上傳文件

```
POST /api/rag/upload
```

**Request**: multipart/form-data

| Field | Type | Description |
|-------|------|-------------|
| agent_id | string | 代理者 ID |
| file | file | 上傳的檔案 |

**Response 200**:
```json
{
  "document_id": "uuid",
  "filename": "example.pdf",
  "status": "processing"
}
```

---

### 取得知識庫文件列表

```
GET /api/rag/{agent_id}/documents
```

**Response 200**:
```json
{
  "documents": [
    {
      "id": "uuid",
      "filename": "example.pdf",
      "file_type": "application/pdf",
      "uploaded_at": "2026-03-09T10:00:00Z",
      "status": "ready"
    }
  ]
}
```

---

### 刪除知識庫文件

```
DELETE /api/rag/{agent_id}/documents/{doc_id}
```

**Response 204**: No Content

---

## 錯誤回應格式

所有錯誤回應遵循以下格式：

```json
{
  "error": "錯誤訊息",
  "code": "ERROR_CODE"
}
```

### 常見錯誤碼

| Code | HTTP Status | 說明 |
|------|-------------|------|
| NOT_FOUND | 404 | 資源不存在 |
| VALIDATION_ERROR | 400 | 驗證失敗 |
| INTERNAL_ERROR | 500 | 伺服器錯誤 |
| UNAUTHORIZED | 401 | 未經授權 |
| FORBIDDEN | 403 | 權限不足 |
| RATE_LIMITED | 429 | 請求過多 |

---

## 使用者管理 API

### 註冊使用者

```
POST /api/auth/register
```

**Request Body**:
```json
{
  "username": "john_doe",
  "email": "john@example.com",
  "password": "secure_password",
  "role": "user"
}
```

**Response 201**:
```json
{
  "id": "uuid",
  "username": "john_doe",
  "email": "john@example.com",
  "role": "user",
  "created_at": "2026-03-09T10:00:00Z"
}
```

---

### 登入

```
POST /api/auth/login
```

**Request Body**:
```json
{
  "email": "john@example.com",
  "password": "secure_password"
}
```

**Response 200**:
```json
{
  "token": "jwt_token",
  "user": {
    "id": "uuid",
    "username": "john_doe",
    "role": "user"
  }
}
```

---

### 取得使用者資訊

```
GET /api/users/me
```

**Response 200**:
```json
{
  "id": "uuid",
  "username": "john_doe",
  "email": "john@example.com",
  "role": "user",
  "created_at": "2026-03-09T10:00:00Z"
}
```

---

### 取得使用者列表 (僅管理者)

```
GET /api/users
```

**Response 200**:
```json
{
  "users": [
    {
      "id": "uuid",
      "username": "admin",
      "email": "admin@example.com",
      "role": "admin",
      "created_at": "2026-03-09T10:00:00Z"
    }
  ]
}
```

---

### 更新使用者角色 (僅管理者)

```
PUT /api/users/{id}/role
```

**Request Body**:
```json
{
  "role": "agent_admin"
}
```

**Response 200**:
```json
{
  "id": "uuid",
  "username": "john_doe",
  "role": "agent_admin"
}
```

---

## 日誌 API

### 取得日誌列表 (僅管理者)

```
GET /api/logs
```

**Query Parameters**:
| Parameter | Type | Description |
|-----------|------|-------------|
| level | string | 日誌級別 (debug/info/warning/error) |
| action | string | 操作類型 |
| user_id | UUID | 過濾特定使用者 |
| from | datetime | 開始時間 |
| to | datetime | 結束時間 |
| page | int | 頁碼 |
| limit | int | 每頁數量 |

**Response 200**:
```json
{
  "logs": [
    {
      "id": "uuid",
      "user_id": "uuid",
      "level": "info",
      "action": "chat",
      "resource_type": "agent",
      "resource_id": "uuid",
      "details": { "message": "User sent a message" },
      "ip_address": "192.168.1.1",
      "timestamp": "2026-03-09T10:00:00Z"
    }
  ],
  "total": 100,
  "page": 1,
  "limit": 20
}
```

---

### 取得日誌詳情 (僅管理者)

```
GET /api/logs/{id}
```

**Response 200**:
```json
{
  "id": "uuid",
  "user_id": "uuid",
  "level": "info",
  "action": "chat",
  "resource_type": "agent",
  "resource_id": "uuid",
  "details": {
    "message": "User sent a message",
    "response_time_ms": 1500
  },
  "ip_address": "192.168.1.1",
  "timestamp": "2026-03-09T10:00:00Z"
}
```

---

## 權限矩陣

| API | admin | agent_admin | user |
|-----|-------|-------------|------|
| POST /api/agents | ✓ | ✗ | ✗ |
| PUT /api/agents/{id} | ✓ | ✓ | ✗ |
| DELETE /api/agents/{id} | ✓ | ✗ | ✗ |
| POST /api/agents/{id}/chat | ✓ | ✓ | ✓ |
| GET /api/logs | ✓ | ✗ | ✗ |
| GET /api/users | ✓ | ✗ | ✗ |
| PUT /api/users/{id}/role | ✓ | ✗ | ✗ |
