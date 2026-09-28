# 功能規格：衛教內容提醒（Health Reminder）

**Feature Branch**: `011-health-reminder`
**Created**: 2026-09-16
**Status**: Draft
**Input**: User description: "不定時或定時發送腎友衛教文章/連結，先由 Agent 搜尋，後台核准後再送 LINE。"

## 使用者情境與測試（必要）

### 使用者故事 1 - 護理/營運可建立候選內容並審核（Priority: P1）

作為護理或營運人員，我希望把候選衛教內容集中管理，並在核准前不可被發送。

**Why this priority**: 沒有審核閘門就有醫療內容風險。

**Independent Test**: 建立 draft 內容後，狀態可被核准/退回，且未核准內容無法發送。

**Acceptance Scenarios**:

1. **Given** 一筆 `draft` 內容，**When** 執行核准，**Then** 內容狀態變為 `approved` 並記錄核准者。
2. **Given** 一筆 `draft` 內容，**When** 嘗試發送，**Then** API 回傳錯誤拒絕。

---

### 使用者故事 2 - 可立即或排程發送衛教內容（Priority: P1）

作為營運人員，我希望核准後可立即推播，或設定定時發送到 LINE。

**Why this priority**: 要同時支援臨時通知與固定衛教節奏。

**Independent Test**: `send-now` 與 `schedule` 都能建立可追蹤結果。

**Acceptance Scenarios**:

1. **Given** 已核准內容，**When** 呼叫 send-now，**Then** 產生 delivery log 並返回 sent/failed/skipped 統計。
2. **Given** 已核准內容，**When** 建立 cron 任務，**Then** 產生 `scheduled_tasks` 任務且可由 Celery 執行。

---

### 使用者故事 3 - 可回查每篇衛教內容的投遞結果（Priority: P2）

作為產品與護理管理者，我希望看到每篇內容投遞結果與失敗原因。

**Why this priority**: 沒有紀錄就無法驗證衛教傳遞品質。

**Independent Test**: 查詢 logs 可返回該篇內容的 sent/failed/skipped 與明細。

**Acceptance Scenarios**:

1. **Given** 衛教內容已發送，**When** 查詢 logs，**Then** 可看到投遞時間、對象與狀態。

## 邊界情境

- `url` 非 http/https 時拒絕建立內容。
- 同一 `source_url` 重複新增時拒絕（避免重覆灌入候選池）。
- 無 `line_user_id` 的病患發送時標記 `skipped`，不影響其他對象。
- 已退回內容不得直接排程或立即發送。

## 需求（必要）

### 功能需求

- **FR-001**: 系統 MUST 提供衛教內容實體，至少包含標題、來源 URL、摘要、狀態。
- **FR-002**: 系統 MUST 提供內容狀態流轉：`draft -> approved/rejected`。
- **FR-003**: 系統 MUST 於發送前檢查內容狀態必須為 `approved`。
- **FR-004**: 系統 MUST 提供立即發送 API，並產生投遞紀錄。
- **FR-005**: 系統 MUST 提供排程發送 API，並重用 `scheduled_tasks`。
- **FR-006**: 系統 MUST 提供內容列表與狀態篩選查詢。
- **FR-007**: 系統 MUST 提供單篇內容投遞紀錄查詢。

### 關鍵實體

- **HealthEducationContent**：衛教內容主檔。
- **HealthEducationDeliveryLog**：單篇內容對各病患的投遞紀錄。

## 成功指標（必要）

### 可量測成果

- **SC-001**: 未核准內容發送阻擋率 100%。
- **SC-002**: 已核准內容發送後，紀錄覆蓋率 100%。
- **SC-003**: 後台使用者可在 3 分鐘內完成「建立內容 -> 核准 -> 發送」流程。
