# dyagent Development Guidelines

Auto-generated from all feature plans. Last updated: 2026-03-09

## Active Technologies
- Python 3.11 + FastAPI、LangChain、LiteLLM（langchain-litellm）、SQLAlchemy、PyTest、Ruff (001-chat-skills-mcp)
- 既有資料庫（Conversation/Message 實體於 backend 代碼中定義） (001-chat-skills-mcp)
- Python 3.11（後端）、JavaScript ES2022（前端，Vite） + FastAPI、SQLAlchemy、LangChain/LiteLLM、PyTest、Ruff、Vite (001-agent-mcp-skills-rag)
- PostgreSQL（主）、Redis（快取/暫存）、Qdrant（向量檢索，RAG） (001-agent-mcp-skills-rag)

- Python 3.11+ / JavaScript (ES2022) + LangChain (RAG), docling-serve (文件轉換), FastAPI (後端框架), Vite (前端構建) (001-dynamic-agents)

## Project Structure

```text
backend/
frontend/
tests/
```

## Commands

cd src [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] pytest [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] ruff check .

## Code Style

Python 3.11+ / JavaScript (ES2022): Follow standard conventions

## Recent Changes
- 001-agent-mcp-skills-rag: Added Python 3.11（後端）、JavaScript ES2022（前端，Vite） + FastAPI、SQLAlchemy、LangChain/LiteLLM、PyTest、Ruff、Vite
- 001-chat-skills-mcp: Added Python 3.11 + FastAPI、LangChain、LiteLLM（langchain-litellm）、SQLAlchemy、PyTest、Ruff

- 001-dynamic-agents: Added Python 3.11+ / JavaScript (ES2022) + LangChain (RAG), docling-serve (文件轉換), FastAPI (後端框架), Vite (前端構建)

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
