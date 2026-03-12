# Research: 動態代理者創建系統

**Date**: 2026-03-09

## Technical Stack Decisions

### 1. 前端框架

**Decision**: Vite + 原生 HTML/CSS/JS

**Rationale**: 
- 使用者指定 Vite 框架
- 最小化依賴，使用原生技術減少維護成本
- Vite 提供快速的開發體驗和優化的生產構建

**Alternatives considered**:
- React/Vue/Angular: 需要額外依賴，不符合最小化原則

---

### 2. 後端框架

**Decision**: FastAPI + Python 3.11+

**Rationale**:
- 使用者指定 Python
- LangChain 需要 Python 環境
- FastAPI 提供自動 API 文件生成 (Swagger UI)

**Alternatives considered**:
- Django: 較重，不需要 ORM 功能
- Flask: 缺少類型驗證和自動文件

---

### 3. RAG 實現

**Decision**: LangChain

**Rationale**:
- 使用者指定 LangChain
- LangChain 提供完整的 RAG 工具鏈
- 支援多種文件格式和 embedding 模型

**Alternatives considered**:
- LlamaIndex: 功能類似，LangChain 生態更完整

---

### 4. 文件轉換

**Decision**: docling-serve

**Rationale**:
- 使用者指定 docling-serve
- 支援多種文件格式轉換 (PDF, DOCX, HTML, Markdown 等)
- 提供 REST API 接口

**Alternatives considered**:
- 其他轉換庫: 功能較少

**Reference**: https://github.com/docling-project/docling-serve.git

---

### 5. MCP Server 整合

**Decision**: MCP (Model Context Protocol) 原生支援

**Rationale**:
- 使用者指定 MCP server
- MCP 提供標準化的工具調用協議
- 支援動態工具發現

**Alternatives considered**:
- 自定義工具系統: 需要額外開發

---

### 6. 存儲

**Selected**: PostgreSQL

**Rationale**: 使用者選擇 PostgreSQL，提供多使用者支援和強大的查詢能力。

---

### 7. 對話歷史存儲

**Selected**: Redis

**Rationale**: 使用者選擇 Redis，提供高速讀取，適合對話歷史的頻繁存取。

---

### 8. 向量資料庫 (RAG)

**Selected**: Qdrant

**Rationale**: 使用者選擇 Qdrant，功能完整的向量資料庫，支援多種部署方式。

---

## Best Practices

### LangChain RAG
- 使用 LangChain 的 `RetrievalQA` 實現問答
- 使用 `TextLoader` + `RecursiveCharacterTextSplitter` 處理文件
- 使用 `Chroma` 或 `FAISS` 作為向量資料庫

### MCP Integration
- 使用 MCP Python SDK 連接 server
- 實現動態工具發現和調用

### Vite 前端
- 使用原生 fetch API 進行 HTTP 請求
- 使用 CSS Variables 實現主題
- 最小化第三方依賴

---

## References

- LangChain Documentation: https://python.langchain.com/
- docling-serve: https://github.com/docling-project/docling-serve.git
- FastAPI: https://fastapi.tiangolo.com/
- Vite: https://vitejs.dev/
