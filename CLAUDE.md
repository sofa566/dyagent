# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 語言

本專案所有文件、註解、說明統一使用**繁體中文**。

## 常用命令

### 開發環境啟動

```bash
# 啟動 Docker 服務（PostgreSQL、Redis、Qdrant）
docker compose up -d

# 執行資料庫遷移
cd backend && alembic upgrade head

# 啟動完整開發環境（後端 + 前端）
./start-dev.sh

# 只重啟後端
./restart-backend.sh
```

### 後端測試與檢查

```bash
cd backend

# 執行所有測試
pytest -v

# 執行單一測試檔案
pytest tests/unit/test_chat.py

# 執行特定測試
pytest tests/unit/test_chat.py::test_function_name

# 代碼檢查
ruff check .

# 代碼格式化
black .
```

### 前端測試與檢查

```bash
cd frontend

# 開發模式
npm run dev

# 構建
npm run build

# 代碼檢查與修復
npm run lint
npm run lint:fix

# 格式化
npm run format

# 測試
npm run test
```

## 架構概覽

### 技術棧

- **後端**：Python 3.11+ / FastAPI / SQLAlchemy / Alembic
- **前端**：JavaScript ES2022 / Vite（原生 JS，無框架）
- **資料庫**：PostgreSQL 16（主）/ Redis（快取）/ Qdrant（向量）
- **AI/LLM**：LangChain / LiteLLM（多 provider 支援）

### 後端分層架構

```
backend/src/
├── api/routes/          # HTTP 端點（參數驗證、權限檢查）
├── services/            # 業務邏輯（chat_router、llm_client、rag_vectorizer）
├── models/              # SQLAlchemy 實體（Agent、Conversation、Message、Document）
├── schemas/             # Pydantic 請求/回應 DTO
├── middleware/          # auth.py（JWT）、rbac.py（角色權限）
└── core/                # config、database、logging
```

### 核心流程：Router-Worker 聊天

1. 用戶訊息進入 `POST /api/chat`
2. 主代理（Router）執行路由決策：規則匹配 → embedding 相似度 → LLM 裁決
3. 轉發到對應 Worker Agent 執行
4. 透過 SSE 串流回傳結果（包含 `route.decision` 事件）

### 權限角色

| 角色 | 權限範圍 |
|------|---------|
| admin | 全控：Agent/User/Logs/Functions/RAG 公有資料集 |
| agent_admin | 讀寫 agent、綁定 Functions、建立私有 RAG 資料集 |
| user | 僅聊天（自動路由） |

### Functions/RAG 優先序

- **Functions**：agent 自訂模板 > 綁定 profile > 系統預設
- **RAG**：公有（`scope=global`）與私有（`scope=agent_private`）資料集

## 代碼規範

### 註解要求

- **函式超過 10 行**：開頭須註解「目的」與「為什麼」
- **所有 Class**：開頭須註解職責與存在原因
- 優先讓代碼自我說明，註解解釋「為什麼」而非「是什麼」

### 函式設計

- 單一職責：若能用「和」描述，應拆分
- 參數盡量少（0-2 個），3 個以上包成物件
- 避免 boolean flag 參數，拆成兩個函式
- 刪除未使用的函式

### 測試

- 遵循 **Given/When/Then** 結構
- 每個測試只驗證一個概念
- pytest 使用 `asyncio_mode = "auto"`

## 環境變數（重點）

```env
# backend/.env
DATABASE_URL=postgresql://user:password@localhost:5432/dyagent
REDIS_URL=redis://localhost:6379
QDRANT_URL=http://localhost:6333

# Embedding 設定
EMBEDDING_PROVIDER=sentence_transformers  # 或 deterministic/ollama/vllm
EMBEDDING_MODEL_NAME=BAAI/bge-m3
ROUTER_EMBEDDING_THRESHOLD=0.55

# LLM（需自行填入 API key）
OPENAI_API_KEY=...
```

## 預設認證

- 用戶名：`admin` / 密碼：`admin`

## 服務位址

- 後端 API：http://127.0.0.1:8000
- 前端：http://127.0.0.1:5173
- API 文件：http://127.0.0.1:8000/docs
