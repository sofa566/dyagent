# 功能規格：LLM Token/Cost 四層治理

**Feature Branch**: `007-llm-cost`
**Created**: 2026-08-30
**Status**: Draft
**Input**: User description: "希望做四層：公司＋群組＋個人＋Agent，並以每次送給 LLM 的 token 累計作為成本估算基礎。"

## 使用者情境與測試（必要）

### 使用者故事 1 - 管理者可看見四層 token/cost 概況（Priority: P1）

作為管理者，我希望在同一套儀表板中看到公司、群組、個人與代理者四層的 token 與成本，快速知道成本主要來自哪裡。

**Why this priority**: 沒有可見性就無法治理，四層報表是所有後續策略的基礎。

**Independent Test**: 產生多位使用者與多代理者對話後，查詢 API 與儀表板四層數據一致。

**Acceptance Scenarios**:

1. **Given** 系統已有近 24 小時 LLM 呼叫資料，**When** 管理者開啟成本儀表板，**Then** 可看到公司總覽與群組/個人/代理者排行。
2. **Given** 指定時間窗（例如本月），**When** 查詢四層 breakdown API，**Then** 回傳每層的 `input_tokens`、`output_tokens`、`total_tokens`、`cost_usd`。

---

### 使用者故事 2 - 使用者可看見自己的成本使用量（Priority: P1）

作為一般使用者，我希望看到自己本月已使用 token 與估算成本，避免超出組織預算規範。

**Why this priority**: 個人透明度能降低濫用與超支，並減少管理溝通成本。

**Independent Test**: 同一帳號連續發送多輪訊息後，`/me` 成本 API 的累計值增加且與後端資料一致。

**Acceptance Scenarios**:

1. **Given** 使用者已登入且有 LLM 使用紀錄，**When** 呼叫個人成本 API，**Then** 回傳當月 token 與成本累計。
2. **Given** 使用者當月無 LLM 使用，**When** 呼叫個人成本 API，**Then** 回傳 0 而非錯誤。

---

### 使用者故事 3 - 管理者可設定四層預算與告警門檻（Priority: P2）

作為管理者，我希望可設定公司/群組/個人/代理者的 token 與成本預算，並在接近上限時收到告警。

**Why this priority**: 先告警再阻擋可降低誤判風險，讓治理逐步上線。

**Independent Test**: 設定門檻後，當某層使用率達 50%/80%/100% 時，系統能產生對應告警事件。

**Acceptance Scenarios**:

1. **Given** 已設定群組月成本上限，**When** 該群組累計成本超過 80%，**Then** 產生 80% 告警事件。
2. **Given** 系統處於 warn-only 模式，**When** 達到 100% 上限，**Then** 僅告警不阻擋請求。

---

### 使用者故事 4 - 系統可切換為硬限制模式（Priority: P3）

作為治理管理者，我希望在策略成熟後啟用硬限制，確保預算不可被突破。

**Why this priority**: 成本治理最終需有強制手段，但應在口徑穩定後啟用。

**Independent Test**: 啟用 hard-limit 後，任一層超限的請求會被拒絕並寫入稽核。

**Acceptance Scenarios**:

1. **Given** hard-limit 已啟用且個人月成本已超限，**When** 使用者再次觸發 LLM 呼叫，**Then** 系統回應拒絕並附原因碼。
2. **Given** 代理者月 token 已超限，**When** 任何使用者透過該代理者發送訊息，**Then** 該請求被拒絕且可追蹤。

## 邊界情境

- 供應商未回傳 usage 時，系統需用估算策略補足並標記 `estimated=true`。
- 供應商回傳成本與估算成本同時存在時，優先使用供應商成本。
- 使用者沒有群組時，群組維度應安全回傳空集合。
- 同一使用者跨多群組的歸屬規則需固定一致，避免月報對帳漂移。
- 時區跨日/月邊界（例如 UTC 與本地時區）需有一致切窗規則。

## 需求（必要）

### 功能需求

- **FR-001**: 系統 MUST 以 `llm_turns` 作為 LLM token/cost 的唯一查詢來源。
- **FR-002**: 系統 MUST 儲存並可查詢每輪 `input_tokens`、`output_tokens`、`total_tokens`。
- **FR-003**: 系統 MUST 支援公司層級 token/cost 聚合查詢。
- **FR-004**: 系統 MUST 支援群組層級 token/cost 聚合查詢。
- **FR-005**: 系統 MUST 支援個人層級 token/cost 聚合查詢。
- **FR-006**: 系統 MUST 支援代理者層級 token/cost 聚合查詢。
- **FR-007**: 系統 MUST 提供管理端四層 breakdown API，支援時間窗、排序與分頁。
- **FR-008**: 系統 MUST 提供使用者個人成本 API（當月累計與使用率）。
- **FR-009**: 系統 MUST 支援四層預算政策設定（token 與 USD）。
- **FR-010**: 系統 MUST 支援 50%/80%/100% 門檻告警，並具備稽核紀錄。
- **FR-011**: 系統 MUST 提供 warn-only 與 hard-limit 兩種模式切換。
- **FR-012**: 系統 MUST 在 hard-limit 模式下，任一層超限即拒絕 LLM 呼叫。
- **FR-013**: 系統 MUST 將拒絕結果寫入稽核資料，包含觸發層級與原因碼。
- **FR-014**: 前端 MUST 在儀表板呈現四層成本可視化（總覽 + 排行 + 告警）。

### 關鍵實體

- **LlmTurn**: 每次 LLM 呼叫審計資料，包含 usage 與 cost。
- **CostUsageSnapshot**: 報表查詢回傳用彙總資料結構（非必要實體表）。
- **CostPolicy**: 四層預算政策資料（可儲存在設定表或設定檔）。
- **CostAlertEvent**: 成本門檻告警事件（可複用既有稽核表或事件表）。

## 成功指標（必要）

### 可量測成果

- **SC-001**: 四層 API 在同一時間窗下，token 與 cost 查詢結果可重現且與 `llm_turns` 對帳誤差為 0。
- **SC-002**: 管理者可在 5 秒內於儀表板辨識 Top 5 成本來源（群組/個人/代理者）。
- **SC-003**: 門檻告警觸發準確率達 100%（測試資料集）。
- **SC-004**: hard-limit 模式下超限請求攔截率 100%。

## 資料口徑定義

- `input_tokens`: 請求送給 LLM 的 token 總數。
- `output_tokens`: LLM 回覆生成的 token 總數。
- `total_tokens`: `input_tokens + output_tokens`。
- `cost_usd`: 優先使用供應商回傳值；若缺值則按單價表估算。
- `estimated`: 當回合任一關鍵值由估算補足時為 `true`。

## 權限需求

- 管理端成本 API：需 `dashboard.read`（後續可拆 `cost.read` / `cost.manage`）。
- 政策更新 API：需管理權限（建議新增 `cost.manage`）。
- 個人成本 API：登入使用者可查詢自身資料。
