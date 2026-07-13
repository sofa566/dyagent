# 功能規格：多代理雙層記憶（Mem0 + Redis）

**Feature Branch**: `005-memory-mem0`  
**Created**: 2026-07-12  
**Status**: Draft  
**Input**: User description: "修正記憶問題，讓每個 Agent 可從過去記憶學習，採用 Mem0 管理長期記憶"

## 使用者情境與測試（必要）

### 使用者故事 1 - 任務分派後仍保有個人偏好（Priority: P1）

作為一般使用者，我希望不論 Master 將任務分派給哪個次代理，回覆都能延續我的偏好與背景，避免每次重新教一次。

**Why this priority**: 這是「記憶可用」的核心價值，若無法跨代理延續，用戶體感等同沒有長期記憶。

**Independent Test**: 建立同一使用者跨兩個不同代理的連續請求，第二個代理能引用第一個代理已學到的使用者偏好。

**Acceptance Scenarios**:

1. **Given** 使用者已在代理 A 表達偏好「回覆使用繁體中文且精簡」，**When** 後續請求被分派到代理 B，**Then** 代理 B 回覆仍符合該偏好。
2. **Given** 使用者未曾建立偏好記錄，**When** 首次對話，**Then** 系統可正常回覆且不因記憶檢索缺資料而失敗。

---

### 使用者故事 2 - 代理會從歷史互動修正專業行為（Priority: P1）

作為產品管理者，我希望各次代理可累積專業經驗（例如工具錯誤修正、較佳執行策略），讓相似任務的成功率逐步提升。

**Why this priority**: 此故事直接對應「每個 Agent 能從過去學習並修正問題」的目標。

**Independent Test**: 先製造一次工具失敗並記錄修正經驗，再送入相似任務，觀察代理是否採用修正策略。

**Acceptance Scenarios**:

1. **Given** 代理曾在某工具參數格式上失敗並寫入修正記憶，**When** 收到相似問題，**Then** 代理優先使用修正後參數格式。
2. **Given** 代理專業記憶存在，**When** 不同使用者觸發同類任務，**Then** 代理可使用共享專業記憶但不引用他人個資。

---

### 使用者故事 3 - 記憶具備隔離、可刪除與可觀測（Priority: P2）

作為系統管理者，我需要確認記憶在 user/agent 維度隔離，並可執行刪除（忘記我）與追蹤記憶讀寫行為。

**Why this priority**: 涉及隱私合規與營運維護，屬於上線必要保護機制。

**Independent Test**: 對特定 user_id 執行記憶重置後，該使用者歷史偏好不再被檢索；事件流可查到記憶讀寫結果。

**Acceptance Scenarios**:

1. **Given** user_A 與 user_B 都有記憶，**When** 以 user_A 查詢記憶，**Then** 不得返回 user_B 記錄。
2. **Given** 對 user_A 執行記憶重置，**When** user_A 再次提問，**Then** 系統不再注入其舊長期記憶。

---

## 邊界情境

- Mem0/Qdrant 暫時不可用時，聊天流程必須降級為無長期記憶模式，不得中斷主流程。
- 多代理並行時，同一 user_id 在不同 agent_id 的短期記憶鍵不得互相覆寫。
- 使用者明確要求「不要記住這段對話」時，該輪不得寫入長期記憶。
- 同一訊息同時包含個人偏好與代理專業經驗時，需正確分流到對應 scope。

## 需求（必要）

### 功能需求

- **FR-001**: 系統 MUST 提供可切換的記憶供應模式：`off`、`mem0_oss`、`mem0_platform`、`mock`。
- **FR-002**: 系統 MUST 支援記憶路由策略模式：`description_only`、`hybrid`、`skill_first`、`memory_first`。
- **FR-003**: 系統 MUST 在 Master 分派後組裝雙層上下文：短期記憶（Redis）+ 長期記憶（Mem0）。
- **FR-004**: 系統 MUST 以 `user_id`、`agent_id`、`conversation_id(run_id)`、`app_id` 做檢索隔離。
- **FR-005**: 系統 MUST 支援三種長期記憶範疇：使用者記憶、代理專業記憶、使用者-代理交叉記憶。
- **FR-006**: 系統 MUST 在回覆完成後寫回記憶，並依內容性質分流到正確範疇。
- **FR-006-A**: 系統 MUST 由 Master/Orchestrator 層作為唯一長期記憶寫入閘口，Worker 僅輸出候選結果不得直接寫入長期記憶。
- **FR-007**: 系統 MUST 對工具失敗/重試結果沉澱可重用修正經驗至代理專業記憶。
- **FR-008**: 系統 MUST 提供讀寫開關（read/write 可獨立停用）。
- **FR-009**: 系統 MUST 支援每輪最多擷取 Top-K 長期記憶，且可由設定調整。
- **FR-010**: 系統 MUST 記錄記憶讀寫事件（成功/失敗/延遲/命中數）於可觀測資料中。
- **FR-011**: 系統 MUST 提供「忘記我」能力，至少支援依 `user_id` 清除相關長期記憶。
- **FR-012**: 系統 MUST 在記憶層異常時採用 fail-open 降級，不影響既有聊天回覆。
- **FR-013**: 系統 MUST 維持現有聊天歷史模式 `CHAT_HISTORY_MODE=recent` 的相容性。
- **FR-014**: 系統 MUST 保證記憶內容注入前可控長度，避免超過模型上下文限制。
- **FR-015**: 系統 MUST 允許後續擴充新的記憶供應器而不改動聊天主流程介面。

### 關鍵實體

- **MemoryScope**: 記憶範疇定義，包含 `user_scope`、`agent_scope`、`interaction_scope`、`global_scope`。
- **MemoryQueryContext**: 單次檢索上下文，包含 `user_id`、`agent_id`、`run_id`、`app_id`、`query`、`top_k`。
- **MemorySnippet**: 檢索回來的記憶片段，包含 `text`、`score`、`scope`、`source_id`。
- **MemoryWritePayload**: 寫入記憶的內容，包含 `messages`、`scope`、`metadata`、`write_reason`。
- **ShortTermSessionState**: Redis 內短期對話狀態，包含最近 N 則 user/assistant 訊息與 TTL。

## 成功指標（必要）

### 可量測成果

- **SC-001**: 同一使用者跨代理任務中，偏好延續命中率達 90% 以上。
- **SC-002**: 開啟記憶後，代理重複性錯誤（同類工具錯誤）在 2 週內下降 30% 以上。
- **SC-003**: 記憶檢索附加延遲 p95 小於 250ms（不含外部 LLM 推理時間）。
- **SC-004**: 記憶層故障時聊天可用性維持 99.9%（故障僅降級，不中斷）。
- **SC-005**: `user_id` 隔離測試 100% 通過，不得檢索到其他使用者記憶。

## 供應模式說明

- `off`：關閉記憶讀寫。
- `mock`：測試用模擬記憶供應器，不依賴外部服務。
- `mem0_oss`：使用 Mem0 開源版（自託管），可接既有 Qdrant。
- `mem0_platform`：使用 Mem0 官方託管平台 API（非任意第三方雲端記憶服務）。

## 路由與記憶影響說明

以下原則作為本功能的路由設計基準：

- 長期記憶會影響系統回應是否適當。
- 主代理在選擇次代理與替次代理準備上下文時，可透過 `ROUTER_ASSIGNMENT_MODE` 與 `AGENT_MEMORY_ROUTING_MODE` 兩個參數設定，決定長期記憶影響多寡。

### 參數責任

- `ROUTER_ASSIGNMENT_MODE`：主代理挑選次代理的主決策開關。
- `AGENT_MEMORY_ROUTING_MODE`：僅在主決策使用記憶訊號時生效，用於控制記憶分數的計算與權重。

### 建議評分公式（實作基線）

當 `ROUTER_ASSIGNMENT_MODE=hybrid` 時，對每個候選次代理計算：

`total_score = w_desc * desc_score + w_skill * skill_score + w_mem * memory_score`

建議預設權重：

- `w_desc = 0.40`
- `w_skill = 0.35`
- `w_mem = 0.25`

### memory_score 子策略（受 AGENT_MEMORY_ROUTING_MODE 控制）

- `memory_first`：記憶命中可作為主導訊號；命中高且超門檻時可提前定案。
- `skill_first`：技能命中優先；記憶訊號僅作加分或同分決勝（tie-break）。
- `description_only`：記憶只用於上下文，不影響路由分數。
- `hybrid`：融合 `user_scope`、`agent_scope`、`interaction_scope` 記憶分數後再加權。

### 主決策與子策略交互規則

- 若 `ROUTER_ASSIGNMENT_MODE=description_only`，忽略 `AGENT_MEMORY_ROUTING_MODE`。
- 若 `ROUTER_ASSIGNMENT_MODE=skill_first`，記憶訊號僅在技能分數接近時作補充。
- 若 `ROUTER_ASSIGNMENT_MODE=memory_first` 或 `hybrid`，`AGENT_MEMORY_ROUTING_MODE` 會直接影響 `memory_score` 的計算、門檻與最終影響比重。
