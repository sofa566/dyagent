# Implementation Plan: Per-Agent MCP/Skills/RAG Config

**Branch**: `001-agent-mcp-skills-rag` | **Date**: 2026-03-15 | **Spec**: specs/001-agent-mcp-skills-rag/spec.md
**Input**: Feature specification from `/specs/001-agent-mcp-skills-rag/spec.md`

## Summary

- 目標：讓每個代理者可於「LLM 設定」頁分別設定 MCP、Skills、RAG，並於聊天時僅使用該代理者已啟用之能力。
- 技術路線：沿用 Agent 既有欄位（model_config、mcp_config、skills、rag_config），新增/擴充 API 與前端設定頁；加入測試與審計。

## Technical Context

<!--
  ACTION REQUIRED: Replace the content in this section with the technical details
  for the project. The structure here is presented in advisory capacity to guide
  the iteration process.
-->

**Language/Version**: Python 3.11（後端）、JavaScript ES2022（前端，Vite）  
**Primary Dependencies**: FastAPI、SQLAlchemy、LangChain/LiteLLM、PyTest、Ruff、Vite  
**Storage**: PostgreSQL（主）、Redis（快取/暫存）、Qdrant（向量檢索，RAG）  
**Testing**: pytest（後端）、vitest（前端）  
**Target Platform**: Linux（服務端）、Browser（前端）  
**Project Type**: Web service + 前端應用  
**Performance Goals**: 設定頁讀寫 API p95 < 300ms；聊天路徑首段 TTFB < 1.5s（不含模型推論）  
**Constraints**: 繁中一致文案；結構化日誌；不破壞既有代理者行為  
**Scale/Scope**: 初期單工作區，代理者 < 100；可擴展

## Constitution Check

Gates（必須通過；Phase 1 後再複核）：
- I. Code Quality：單一職責；避免重複；Ruff 乾淨；變更需 Code Review。
- II. Testing：新增/變更 API 與聊天行為皆需測試（單元/整合）。
- III. UX 一致：設定頁/聊天頁載入/錯誤/成功回饋完整，繁中文案。
- IV. 效能：設定 API p95 < 300ms；不因未啟用能力阻塞聊天。
- V. 可觀測性：結構化日誌 + 設定審計（操作者/時間/重點）。
- VI. 繁中文件：文件、註解、錯誤訊息皆繁體中文。

狀態：通過。備註：specs 內 001-* 前綴重複（歷史資料夾）；不阻擋本功能，後續整理。

## Project Structure

### Documentation (this feature)

```text
specs/[###-feature]/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)
<!--
  ACTION REQUIRED: Replace the placeholder tree below with the concrete layout
  for this feature. Delete unused options and expand the chosen structure with
  real paths (e.g., apps/admin, packages/something). The delivered plan must
  not include Option labels.
-->

```text
# [REMOVE IF UNUSED] Option 1: Single project (DEFAULT)
src/
├── models/
├── services/
├── cli/
└── lib/

tests/
├── contract/
├── integration/
└── unit/

# [REMOVE IF UNUSED] Option 2: Web application (when "frontend" + "backend" detected)
backend/
├── src/
│   ├── models/
│   ├── services/
│   └── api/
└── tests/

frontend/
├── src/
│   ├── components/
│   ├── pages/
│   └── services/
└── tests/

# [REMOVE IF UNUSED] Option 3: Mobile + API (when "iOS/Android" detected)
api/
└── [same as backend above]

ios/ or android/
└── [platform-specific structure: feature modules, UI flows, platform tests]
```

**Structure Decision**: 採用 Web application 結構；實際位置：
- backend/src/api/routes/agents.py（新增/擴充整合設定 API）
- backend/src/services/*（MCP/RAG 測試服務與審計）
- frontend/src/pages/agent-llm-config.html（設定頁）
- frontend/src/services/api.js（封裝呼叫）

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |

## Constitution Re-check (Post-Design)

- Code Quality：維持通過（沿用欄位、單一職責、Ruff）。
- Testing：需為新 API/聊天行為補齊測試（列於後續 tasks）。
- UX 一致：設定頁/聊天頁行為明確；文案繁中。
- 效能：設定 API 不涉重 I/O；無風險。
- 可觀測性：規劃新增審計與日誌點位。
- 繁中文件：本規劃與後續文件皆以繁中撰寫。
