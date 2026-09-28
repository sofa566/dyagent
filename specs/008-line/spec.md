# 功能規格：LINE 雙向通道與人工接手機制

**Feature Branch**: `008-line`
**Created**: 2026-09-02
**Status**: Draft
**Input**: User description: "使用者透過 LINE 聊天，取得 Agent 回覆；操作員可在新聊天界面主動回覆使用者。"

## 使用者情境與測試（必要）

### 使用者故事 1 - LINE 使用者可直接與 Agent 對話（Priority: P1）

作為 LINE 使用者，我希望在手機 LINE 傳送訊息後，能立即收到系統 Agent 回覆。

**Why this priority**: 這是整個 LINE 通道的基礎價值，沒有雙向對話就沒有實際可用性。

**Independent Test**: 模擬 webhook `message:text` 事件，確認系統建立 session、寫入訊息，並呼叫 reply API。

**Acceptance Scenarios**:

1. **Given** LINE webhook 收到文字訊息，**When** session 為 `bot` 模式，**Then** 系統回覆 Agent 文字並落地審計。
2. **Given** webhook 簽章錯誤，**When** 啟用簽章驗證，**Then** 請求被拒絕。

---

### 使用者故事 2 - 操作員可人工接手並主動回覆（Priority: P1）

作為系統操作員，我希望在後台看到 LINE 對話並可主動送訊息給使用者。

**Why this priority**: 客服與營運場景必須支援人工接手，不能只靠 Agent 自動回覆。

**Independent Test**: 後台 API 切換 `human` 模式後，操作員可透過 push API 送出訊息。

**Acceptance Scenarios**:

1. **Given** session 為 `human`，**When** LINE 使用者再傳訊息，**Then** 系統只收訊不自動回覆。
2. **Given** 操作員送出訊息，**When** push 成功，**Then** LINE 使用者可收到訊息且後台訊息記錄新增一筆 `operator` 出站。

---

### 使用者故事 3 - 操作員可切換 Agent 與查看歷史（Priority: P2）

作為操作員，我希望可切換某一條 LINE 會話的 Agent，並查看完整訊息歷史。

**Why this priority**: 實務上同一使用者可能要轉接到不同專長 Agent。

**Independent Test**: 呼叫 agent 指派 API 後，下一輪 bot 回覆由新 Agent 產生。

**Acceptance Scenarios**:

1. **Given** 已存在 LINE session，**When** 操作員更新 `assigned_agent_id`，**Then** 設定成功且可於列表看到新 Agent。
2. **Given** 操作員打開對話，**When** 查詢訊息 API，**Then** 取得按時間排序的 inbound/outbound 記錄。

## 邊界情境

- LINE `source.userId` 缺失（群組或特殊事件）時，系統應忽略或記錄而不崩潰。
- reply token 過期或缺失時，應回報錯誤並可改用 push 路徑補發。
- LINE channel secret/token 未設定時，不應嘗試發送外部請求。
- 操作員同時操作同一會話的 mode/agent 更新時，最終狀態以最後一次寫入為準。

## 需求（必要）

### 功能需求

- **FR-001**: 系統 MUST 提供 `POST /api/line/webhook` 接收 LINE 事件。
- **FR-002**: 系統 MUST 支援 `X-Line-Signature` 驗證（可透過設定開關控制）。
- **FR-003**: 系統 MUST 為每個 LINE `userId` 建立或復用固定 session。
- **FR-004**: 系統 MUST 將 LINE 入站訊息寫入獨立稽核表。
- **FR-005**: 系統 MUST 將 LINE 訊息同步到既有 `Conversation/Message`。
- **FR-006**: session 為 `bot` 時，系統 MUST 產生 Agent 回覆並透過 LINE reply API 回傳。
- **FR-007**: session 為 `human` 時，系統 MUST 停止自動回覆。
- **FR-008**: 系統 MUST 提供操作員查詢 session 清單與訊息歷史 API。
- **FR-009**: 系統 MUST 提供操作員切換 `mode=bot|human` API。
- **FR-010**: 系統 MUST 提供操作員指派 Agent API。
- **FR-011**: 系統 MUST 提供操作員 push 訊息 API。
- **FR-012**: 只有具管理權限者 MUST 可執行人工送訊與設定更新。

### 關鍵實體

- **LineChannelSession**: LINE 使用者到 Conversation/Agent 的映射與模式狀態。
- **LineMessage**: LINE 通道訊息稽核（inbound/outbound、sender_type、內容、時間）。
- **Conversation / Message**: 既有聊天模型，作為統一對話資料層。

## 成功指標（必要）

### 可量測成果

- **SC-001**: webhook 文字訊息處理成功率 >= 99%（測試與預備環境）。
- **SC-002**: 操作員可在 10 秒內完成人工接手（選會話 -> 切 human -> 發送）。
- **SC-003**: 所有 LINE 入出站訊息都有對應稽核紀錄，覆蓋率 100%。
- **SC-004**: `human` 模式下自動回覆誤發率為 0%。
