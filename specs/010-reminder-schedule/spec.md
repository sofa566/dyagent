# 功能規格：系統共用提醒排程（Reminder Scheduler）

**Feature Branch**: `010-reminder-schedule`
**Created**: 2026-09-12
**Status**: Draft
**Input**: User description: "將 cron table 機制抽成系統共用能力，供腎友與後續流程共用。"

## 使用者情境與測試（必要）

### 使用者故事 1 - 營運/護理人員可使用統一規則配置提醒（Priority: P1）

作為營運或護理管理者，我希望用同一套資料結構配置提醒時段與訊息模板，避免每個模組各做一套。

**Why this priority**: 沒有統一策略層，就無法稱為平台能力。

**Independent Test**: 建立 policy 後可生成並派送 job，且留存投遞紀錄。

**Acceptance Scenarios**:

1. **Given** 建立啟用中的提醒 policy，**When** 觸發 dispatch，**Then** 系統為符合條件對象建立 job 並派送。
2. **Given** policy 停用，**When** 到達派送時間，**Then** 不產生新 job。

---

### 使用者故事 2 - 系統排程可自動觸發且避免重複派送（Priority: P1）

作為平台維運者，我希望排程器在固定時間自動派送，且多執行緒/多實例下不重複送。

**Why this priority**: 排程若不可靠，會直接造成使用者干擾與作業風險。

**Independent Test**: 同時觸發多次 dispatch，僅一筆實際派送生效。

**Acceptance Scenarios**:

1. **Given** 同一日期與時段重覆觸發，**When** force=false，**Then** 相同對象不重複建立有效 job。
2. **Given** Celery worker 多實例並行，**When** 同一 Cron 任務到點，**Then** 只會有一次有效執行紀錄。

---

### 使用者故事 3 - 產品團隊可觀測提醒成效與失敗原因（Priority: P2）

作為產品/營運人員，我希望看到提醒送達、略過與失敗統計，並追蹤失敗原因。

**Why this priority**: 沒有可觀測性就無法優化流程。

**Independent Test**: 查詢 API 可返回指定日期/時段的 sent/skipped/failed 統計。

**Acceptance Scenarios**:

1. **Given** 派送完成，**When** 查詢報表，**Then** 可看到 job 與 delivery log 指標。
2. **Given** 通道發送失敗，**When** 查詢 log，**Then** 可看到錯誤摘要與時間戳。

---

### 使用者故事 4 - 後台使用者可在 UI 直接管理提醒與補發（Priority: P1）

作為護理管理者或營運人員，我希望在後台頁面直接查看提醒狀態、手動補發、調整 policy，而不必每次打 API。

**Why this priority**: 若只有 API 而沒有作業 UI，跨角色操作成本高，無法落地日常流程。

**Independent Test**: 使用者可在同一頁完成「查詢完整性 -> 觸發派送 -> 查看結果」三步驟。

**Acceptance Scenarios**:

1. **Given** 使用者進入提醒管理頁，**When** 選擇日期與時段，**Then** 可查看合規摘要與病患明細。
2. **Given** 發現缺報病患，**When** 點擊手動派送，**Then** 可看到 sent/skipped/failed 統計並刷新列表。
3. **Given** 使用者調整 policy（模板/啟用狀態），**When** 儲存成功，**Then** 下次 dispatch 依新 policy 生效。

---

### 使用者故事 5 - 後台可用 Crontab 定義任務並呼叫 API（Priority: P1）

作為營運管理者，我希望在後台以 Crontab 定義任務啟動時間，任務到點後可執行指定動作（例如呼叫提醒 API）。

**Why this priority**: 沒有動態排程能力，就無法把提醒能力平台化給其他流程重用。

**Independent Test**: 建立 Crontab 任務後，Celery Beat（RedBeat）可持久化排程並在到點交由 Celery Worker 執行。

**Acceptance Scenarios**:

1. **Given** 使用者建立 `0 6 * * *` 的 `renal_reminder_dispatch` 任務，**When** 到達 06:00，**Then** 系統執行提醒派送並寫入 run log。
2. **Given** 使用者建立 `http_call` 任務，**When** 到點，**Then** 系統呼叫目標 API 並記錄成功/失敗摘要。
3. **Given** 任務被停用，**When** worker 同步，**Then** 該任務不再觸發。

## 邊界情境

- 派送時間設定格式錯誤（非 HH:MM）時，系統應回退安全預設並記錄警告。
- 通道 token 缺失時，job 應標示 `failed` 而非中斷整批。
- 對象未綁定可派送身分（如 line_user_id 缺失）時，應標示 `skipped`。
- 同日期同時段重複觸發時，系統不應重複派送（除非 `force=true`）。
- UI 執行手動派送後，若網路中斷，前端應可重新查詢最新結果，避免重複按送。
- Crontab 表達式錯誤時，API 應拒絕儲存並回傳可理解錯誤。
- Worker 與 API 內部 token 不一致時，worker 請求應拒絕。

## 需求（必要）

### 功能需求

- **FR-001**: 系統 MUST 提供統一提醒政策實體（policy）以定義時段、通道與模板。
- **FR-002**: 系統 MUST 提供提醒任務實體（job）保存派送排程與狀態。
- **FR-003**: 系統 MUST 提供投遞稽核實體（delivery log）保存送達/失敗細節。
- **FR-004**: 系統 MUST 提供背景 dispatcher 依設定時段自動觸發派送。
- **FR-005**: 系統 MUST 在同日同時段提供去重能力（資料層 + 分散式鎖）。
- **FR-006**: 系統 MUST 提供手動 dispatch API 供營運補發與驗收。
- **FR-007**: 系統 MUST 提供日報完整性/派送結果查詢 API。
- **FR-008**: 系統 SHOULD 支援後續擴充其他通道（Email/SMS）而不破壞既有 LINE 流程。
- **FR-009**: 系統 MUST 提供提醒管理 UI，至少含日期/時段篩選、手動派送、結果摘要。
- **FR-010**: 系統 MUST 提供 policy 管理 UI，至少含啟用/停用、模板編輯、目標時段設定。
- **FR-011**: 系統 MUST 提供派送紀錄 UI，至少可查看 sent/skipped/failed 與錯誤摘要。
- **FR-012**: 系統 MUST 提供 `scheduled_tasks` 與 `scheduled_task_runs` 實體，支援 Crontab 任務定義與執行追蹤。
- **FR-013**: 系統 MUST 提供排程任務 API（建立/更新/啟停/手動執行/查詢執行紀錄）。
- **FR-014**: 系統 MUST 以 Celery Worker 分離執行任務，API 節點不得直接承擔定時工作。
- **FR-015**: 系統 MUST 以 Celery Beat + RedBeat（Redis）持久化 Crontab 排程，取代既有固定輪詢 dispatcher。
- **FR-016**: 系統 MUST 將「LINE 早晚提醒」改為預設 Crontab 任務（可在 UI 啟停與調整 cron）。

### 關鍵實體

- **MonitoringReminderPolicy**: 提醒規則與模板。
- **MonitoringReminderJob**: 單次排程任務與狀態。
- **MonitoringReminderDeliveryLog**: 投遞明細與錯誤資訊。
- **ScheduledTask**: Crontab 任務定義（task type、cron、timezone、payload）。
- **ScheduledTaskRun**: 任務執行紀錄（status、duration、error、output）。

## 成功指標（必要）

### 可量測成果

- **SC-001**: Dispatcher 在設定時段自動觸發成功率 >= 99%（測試/預備環境）。
- **SC-002**: 同日同時段重覆觸發去重正確率 100%（force=false）。
- **SC-003**: 每次派送都有 job 與 delivery log，覆蓋率 100%。
- **SC-004**: 單次派送失敗不影響同批其他對象，批次完成率 >= 99%。
- **SC-005**: 非工程角色可在 2 分鐘內完成一次手動補發流程（查詢 -> 派送 -> 驗證結果）。
- **SC-006**: 提醒管理 UI 操作失敗後可重試且不重複派送，誤派送率為 0%。
