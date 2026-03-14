# Implementation Plan: 一般使用者聊天入口與聊天紀錄側欄

**Branch**: `002-user-chat-history` | **Date**: 2026-03-14 | **Spec**: specs/002-user-chat-history/spec.md
**Input**: Feature specification from `/specs/002-user-chat-history/spec.md`

**Note**: This template is filled in by the `/speckit.plan` command. See `.specify/templates/plan-template.md` for the execution workflow.

## Summary

讓一般使用者登入後直接進入聊天頁，並在聊天頁左側提供個人聊天紀錄清單（依最近互動排序，可切換會話）；初次使用需選擇代理者，回訪自動選擇上次使用的代理者。技術面聚焦於：前端導覽控管（依角色隱藏管理選單）、聊天頁載入時的會話清單與預設代理者決策、以及僅需 chat 權限的公開代理者清單。

## Technical Context

<!--
  ACTION REQUIRED: Replace the content in this section with the technical details
  for the project. The structure here is presented in advisory capacity to guide
  the iteration process.
-->

**Language/Version**: Python 3.11（後端）、JavaScript (ES2022)（前端）  
**Primary Dependencies**: FastAPI、SQLAlchemy、Vite 前端、瀏覽器 Fetch API（現有專案依賴）  
**Storage**: 既有 DB（Conversations/Messages 已於後端定義）  
**Testing**: PyTest（後端）、手動與 E2E 驗收（前端）  
**Target Platform**: Linux server（後端 API）、現代瀏覽器（前端）  
**Project Type**: Web 服務（前後端分離）  
**Performance Goals**: 首屏到聊天頁 ≤ 2s；清單載入 ≤ 1s；會話切換 ≤ 5s（見 Success Criteria）  
**Constraints**: UI/文字為繁體中文；一般使用者無管理導覽；公開代理者清單僅含必要欄位  
**Scale/Scope**: 單一組織內部使用，單位量級為百人等級（NEEDS CLARIFICATION 若更大規模需調整分頁/虛擬列表）

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

依據專案 Constitution：
- I. Code Quality：此計畫不引入高風險複雜度，僅在前端導覽與聊天頁增加 UI/查詢；維持清晰命名與單一職責。
- II. Testing Standards：後端已有會話/聊天整合測試基礎；本次新增公開代理者清單端點已具測試；建議新增前端驗收腳本（後續）。
- III. User Experience Consistency：聊天頁中文在地化、錯誤提示清楚（403→提示登入/權限不足），載入/空態顯示。
- IV. Performance Requirements：本計畫加入可量測目標（見 Success Criteria）。
- V. Observability & Maintainability：沿用結構化日誌，對新端點與導向行為維持既有日誌級別。
- VI. Documentation in Traditional Chinese：本規劃與後續文件均以繁體中文撰寫。

GATE 結論：通過。無需豁免。

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
backend/
├── src/
│   ├── api/
│   │   ├── routes/agents.py   # 提供 /agents/public、/agents/{id}/conversations、/agents/{id}/chat
│   │   └── routes/auth.py     # 登入/註冊
│   ├── models/                # User, Agent, Conversation, Message
│   └── services/              # ChatRouter/LLMClient 等（既有）
└── tests/
    ├── integration/
    └── unit/

frontend/
└── src/
    └── pages/
        ├── login.html         # 登入
        ├── chat.html          # 一般使用者登入落地頁；左側會話清單 + 右側對話
        └── agent-llm.html     # 管理用（一般使用者不顯示入口）
```

**Structure Decision**: [Document the selected structure and reference the real
directories captured above]
採用現有的 backend/frontend 專案結構；本功能主要影響 frontend/src/pages/chat.html 的導覽與清單行為，以及 backend/src/api/routes/agents.py 的公開清單端點。

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |
