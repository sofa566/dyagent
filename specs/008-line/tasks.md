# Tasks: LINE 雙向通道與人工接手機制

**Input**: `specs/008-line/spec.md`, `specs/008-line/plan.md`

## Phase 1 - 模型與設定

- [x] T001 新增 `LineChannelSession` / `LineMessage` 資料模型
- [x] T002 新增 migration 建立 LINE session/message 資料表與索引
- [x] T003 新增 LINE 設定欄位（secret、token、verify、default agent）

## Phase 2 - Webhook 與自動回覆

- [x] T004 新增 `POST /api/line/webhook`
- [x] T005 實作 `X-Line-Signature` 驗證
- [x] T006 實作入站訊息寫盤與 Conversation/Message 同步
- [x] T007 實作 bot 模式下 Agent 自動回覆與 LINE reply API

## Phase 3 - 人工接手 API

- [x] T008 新增 `GET /api/line/sessions`
- [x] T009 新增 `GET /api/line/sessions/{session_id}/messages`
- [x] T010 新增 `PUT /api/line/sessions/{session_id}/mode`
- [x] T011 新增 `PUT /api/line/sessions/{session_id}/agent`
- [x] T012 新增 `POST /api/line/sessions/{session_id}/messages`（operator push）

## Phase 4 - 前端操作台

- [x] T013 新增 `frontend/src/pages/line-console.html`
- [x] T014 完成會話清單/訊息檢視/模式切換/Agent 指派/人工送訊 UI

## Phase 5 - 測試與驗證

- [x] T015 新增 LINE webhook 與 operator API 整合測試
- [x] T016 補齊 webhook 簽章驗證失敗案例測試
- [x] T017 執行 `pytest` 與 `npm run build`

## Phase 6 - 路徑收斂計畫（僅文件）

- [x] T018 定義 LINE 與 `/api/chat` 的收斂邊界（收斂項目/保留差異）
- [x] T019 制定 P1-P4 收斂分階段策略與風險控管
- [x] T020 補齊收斂驗收指標（回覆一致性、錯誤率、延遲、可回退）
