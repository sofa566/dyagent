# Quickstart: 動態代理者創建系統

**Date**: 2026-03-09

## 前置需求

- Python 3.11+
- Node.js 18+
- Git
- Docker (推薦用於服務執行)

## 安裝步驟

### 1. 複製專案

```bash
git clone <repository-url>
cd dyagent
```

### 2. 安裝後端依賴

```bash
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 啟動 PostgreSQL

確保 PostgreSQL 已安裝並運行：

```bash
# 建立資料庫
createdb dyagent
```

### 4. 啟動 Redis

```bash
# 使用 Docker
docker run -d -p 6379:6379 redis:7

# 或系統安裝
redis-server
```

### 5. 啟動 Qdrant (向量資料庫)

```bash
# 使用 Docker
docker run -d -p 6333:6333 qdrant/qdrant
```

### 6. 啟動 docling-serve (文件轉換服務)

```bash
# 複製 docling-serve 專案
git clone https://github.com/docling-project/docling-serve.git
cd docling-serve

# 安裝依賴
pip install -e .

# 啟動服務 (預設 http://localhost:3001)
docling-serve
```

### 7. 設定環境變數

在 `backend/` 目錄下建立 `.env` 檔案：

```env
# OpenAI (如使用 cloud 模型)
OPENAI_API_KEY=your-api-key

# LangChain 設定
LANGSMITH_API_KEY=your-langsmith-key

# 資料庫
DATABASE_URL=postgresql://user:password@localhost:5432/dyagent

# Redis (對話歷史)
REDIS_URL=redis://localhost:6379

# Qdrant (向量資料庫)
QDRANT_URL=http://localhost:6333

# 伺服器設定
HOST=0.0.0.0
PORT=8000
```

### 8. 啟動後端服務

```bash
cd backend
uvicorn src.api.main:app --reload
```

後端服務將在 http://localhost:8000 運行

API 文件可在 http://localhost:8000/docs 查看

### 9. 安裝前端依賴

```bash
cd frontend
npm install
```

### 10. 啟動前端開發伺服器

```bash
npm run dev
```

前端服務將在 http://localhost:5173 運行

### 11. 開始使用

1. 開啟瀏覽器訪問 http://localhost:5173
2. 點擊「創建代理者」
3. 填寫代理者名稱和描述
4. 選擇模型類型 (local/cloud) 並配置
5. 點擊「創建」
6. 選擇代理者並開始聊天

## 驗證安裝

### 檢查後端 API

```bash
curl http://localhost:8000/api/agents
```

預期回應：
```json
{"agents": []}
```

### 檢查 API 文件

訪問 http://localhost:8000/docs 應該能看到 Swagger UI

## 常見問題

### Q: 無法連接到模型

A: 請檢查：
1. API 金鑰是否正確設定在 `.env` 檔案
2. 網路是否能夠訪問模型提供者的伺服器
3. 查看後端日誌中的錯誤訊息

### Q: 文件上傳失敗

A: 請確認：
1. docling-serve 服務正在運行
2. 檔案大小不超過 50MB
3. 支援的格式：PDF, DOCX, HTML, Markdown

### Q: MCP 連接失敗

A: 請確認：
1. MCP server 正在運行
2. URL 和認證資訊正確
3. 防火牆允許連接

## 下一步

- 參考 `data-model.md` 了解資料結構
- 參考 `contracts/api-contracts.md` 了解 API 設計
- 執行 `/speckit.tasks` 產生實作任務清單
