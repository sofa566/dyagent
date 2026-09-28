# Implementation Plan: LINE 雙向通道與人工接手機制

**Branch**: `008-line` | **Date**: 2026-09-02 | **Spec**: `specs/008-line/spec.md`
**Input**: Feature specification from `/specs/008-line/spec.md`

## Summary

本變更新增 LINE 通道整合層，讓使用者可直接在 LINE 對話，並讓操作員透過新後台介面接手回覆：

1. 後端新增 LINE webhook + session/message 資料模型。
2. 新增操作員 API：列表、歷史、模式切換、Agent 指派、人工 push。
3. 新增前端 `line-console.html` 作為人工接手介面。
4. 保持既有聊天核心不重寫，復用 `ChatRouter.single_turn`。

## Technical Context

**Language/Version**: Python 3.11（後端） / JavaScript ES2022（前端）
**Primary Dependencies**: FastAPI、SQLAlchemy、httpx、PyTest、Vite
**Storage**: PostgreSQL（主） / SQLite（測試）
**Testing**: PyTest
**Target Platform**: Linux
**Project Type**: Web 服務（backend/frontend）
**Constraints**: 全文件與介面文案使用繁體中文；LINE 外部 API 需安全驗簽
**Scope**: LINE webhook、雙向訊息、人工接手後台

## Constitution Check

- I. Code Quality：LINE 邏輯集中於 `line_integration` 路由，避免侵入既有 chat 大檔。
- II. Testing Standards：新增 webhook 與 operator API 測試。
- III. User Experience Consistency：保持既有 Conversation/Message 可觀測性。
- IV. Performance Requirements：MVP 先以同步流程，後續可演進 queue worker。
- V. Observability & Maintainability：所有 LINE 訊息落地 `line_messages` 並同步到 Message。
- VI. Documentation in Traditional Chinese：規格、計畫、任務與 UI 文案全繁中。

GATE 結論：通過。

## Project Structure

### Documentation (this feature)

```text
specs/008-line/
├── change.md
├── spec.md
├── plan.md
└── tasks.md
```

### Source Code (repository root)

```text
backend/
├── src/
│   ├── api/routes/
│   │   └── line_integration.py
│   ├── models/
│   │   └── __init__.py
│   └── core/
│       └── config.py
├── alembic/versions/
│   └── 20260902_0020_line_channel_sessions_messages.py
└── tests/integration/
    └── test_line_integration_api.py

frontend/
└── src/pages/
    └── line-console.html
```

## Phase Plan

### Phase 1：資料模型與設定

1. 新增 `LineChannelSession`、`LineMessage` 模型。
2. 新增 Alembic migration 建立資料表與索引。
3. 新增 LINE 設定欄位（secret、token、verify、default agent）。

### Phase 2：Webhook 與自動回覆

1. 新增 `/api/line/webhook`。
2. 實作簽章驗證。
3. 實作入站訊息寫盤 -> Agent 回覆 -> LINE reply。

### Phase 3：人工接手 API

1. 新增 session 列表與訊息查詢 API。
2. 新增 mode/agent 更新 API。
3. 新增操作員 push 訊息 API。

### Phase 4：前端操作台

1. 新增 `line-console.html`。
2. 連接上述 API，完成列表、查看、切換、發送流程。

### Phase 5：驗證

1. 新增整合測試（webhook、human mode、operator push）。
2. 執行 `pytest` 與 `npm run build`。

### Phase 6：路徑收斂計畫（文件階段）

1. 定義「必須收斂」與「保留差異」清單。
2. 設計共用聊天入口服務（僅設計，不動程式）。
3. 規劃 LINE 與 `/api/chat` 參數對齊策略（RAG、工具、路由）。
4. 定義灰度開關與回退策略，避免一次性切換風險。

#### 6.1 收斂範圍

- 必須收斂：
  - 工具/技能執行前後處理邏輯。
  - 錯誤訊息轉譯與使用者可讀回覆品質。
  - RAG 參數注入與資料集授權判定策略。
  - 路由決策核心（至少單代理決策語意一致）。
- 保留差異：
  - LINE webhook 驗簽流程。
  - LINE Reply/Push 傳輸模式。
  - `bot/human` 人工接手機制。
  - LINE 專屬稽核模型（`LineChannelSession`、`LineMessage`）。

#### 6.2 分階段策略

1. **P1（低風險）**：先抽共用服務介面，讓 `line_integration` 與 `chat` 可共用單輪回覆流程。
2. **P2（中風險）**：讓 LINE session 支援預設資料集策略，對齊 `/api/chat` 的 RAG 參數語意。
3. **P3（中高風險）**：加入可切換執行模式（`single_turn` / `chat_entry_compatible`）並灰度驗證。
4. **P4（營運穩定）**：補齊錯誤補償、重送機制與可觀測事件。

#### 6.3 驗收指標

- 同問題在 LINE bot 模式與 chat.html 的回覆一致性可量測提升。
- `schema_validation_failed` 與工具參數錯誤率下降。
- webhook 延遲維持可接受範圍（以 p95 為主）。
- 發生 Reply/Push 失敗時可追蹤、可重送、可人工接管。

## Complexity Tracking

| Risk/Complexity | Why Needed | Mitigation |
|---|---|---|
| webhook 安全性 | 外部入口易被偽造請求攻擊 | 強制簽章驗證開關與預設開啟 |
| LINE reply/push 失敗 | 網路或 token 錯誤造成送達失敗 | 結構化錯誤訊息與稽核落地 |
| 人工與機器人競態 | 同一會話模式切換時可能重複回覆 | 用 `mode` 強制分流，human 模式不自動回覆 |
| 同步呼叫模型延遲 | webhook 路徑可能較慢 | MVP 先可用，後續可改 queue 化 |
| 收斂改造回歸風險 | 路徑對齊涉及多模組、易影響既有行為 | 先文件化，後灰度上線，保留回退開關 |
