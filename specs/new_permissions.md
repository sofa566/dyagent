# 新權限架構決策稿（草案 v1）

- 決策主題：將工具（MCP / 技能 / Functions）執行權限從人員 RBAC 移除，改為「預設可執行 + 風險策略控管」；實體權限聚焦於 Agents 與資料集。
- 決策狀態：Proposed（待確認）
- 適用範圍：後端授權、前端權限管理頁、聊天工具執行流程、稽核與配額治理。

## 1. 背景與問題

- 現況 RBAC 同時管理「功能管理 CRUD」與「執行時授權」，模型複雜、維運成本高。
- 工具本身多為計算能力，未必直接代表資料敏感性；主要資料邊界其實在 Agent 與資料集。
- 權限管理頁與授權邏輯因工具執行維度過多，造成理解、設定與排錯成本上升。

## 2. 決策目標

- 將授權核心聚焦於「資料與代理可見性」。
- 降低角色配置複雜度，提升前後端授權一致性。
- 保留必要治理能力：成本、外部呼叫、寫入風險與審計可追溯性。

## 3. 核心決策

1. 保留 RBAC 的管理權限：
   - `skills.*`、`mcp.*`、`functions.*` 僅代表管理 CRUD（建立、讀取、更新、刪除）。
2. 移除工具執行 RBAC：
   - 不再以「使用者角色」決定是否可執行某個工具。
3. 實體權限僅保留兩類：
   - `entity.agent.<id>.execute`
   - `entity.dataset.<id>.execute`
4. 工具執行改為策略控管（Policy-based），不再以人員 RBAC 控制。

## 4. 權限模型（To-Be）

- 身份層（Who）：User / Group / Role（維持）
- 實體層（What）：Agent、Dataset（核心授權）
- 策略層（How）：工具風險策略（非 RBAC）

建議工具策略欄位：

- `risk_level`: `safe | restricted | dangerous`
- `requires_confirmation`: `true | false`
- `cost_class`: `free | billable`
- `allow_scopes`: 允許掛載在哪些 Agent 類型或範圍
- `rate_limit_profile`: 每使用者 / 每群組 / 每 Agent 的配額與頻率限制

## 5. 風險治理原則

- `safe`：純讀、純計算，預設可執行。
- `restricted`：會外部呼叫或可能產生成本，需配額與頻率限制。
- `dangerous`：外部寫入/刪除/交易類操作，需強制二次確認與完整審計。
- MCP 額外原則：凡涉及憑證、外部寫入、不可逆操作者，不可視為無風險工具。
- 資料最小暴露：工具可執行不代表可讀所有資料，仍受 Agent / Dataset 權限邊界限制。

## 6. 優點與缺點

### 優點

- 權限心智模型更清楚：人管資料邊界，策略管工具風險。
- 角色配置顯著簡化，跨頁一致性提高。
- 減少因工具授權造成的角色碎片化。

### 缺點

- 需補齊策略引擎、配額、審計欄位與告警。
- 若策略過寬，可能導致成本飆升或外部操作濫用。

## 7. 漸進遷移方案

### Phase 1：相容期（1-2 週）

- 保留既有舊鍵，執行路徑優先走新策略。
- 權限管理頁將工具執行舊鍵標註為「相容舊鍵」。

### Phase 2：切換期（1 週）

- 前端隱藏工具執行 RBAC 設定入口。
- 後端增加告警：偵測仍依賴舊執行鍵的流量。

### Phase 3：清理期（1 週）

- 移除工具執行 RBAC 判斷與舊鍵。
- 保留 CRUD 管理權限 + Agent/Dataset 實體權限。

## 8. 驗收標準（Definition of Done）

- 角色/群組配置複雜度下降（例如權限項目數降低 30% 以上）。
- 使用者是否可用某 Agent 僅由 `entity.agent.*` 決定。
- 使用者是否可存取某資料集僅由 `entity.dataset.*` 決定。
- 每次工具呼叫均可審計：`who`、`agent`、`tool`、`risk_level`、`cost_estimate`、`result_status`。
- 上線後 1-2 週內，成本與錯誤率無異常升高。

## 9. 回滾策略

- 保留 feature flag：`TOOL_EXEC_POLICY_ENABLED`
- 若治理風險升高，可暫時切回舊 RBAC 執行判斷。
- 審計事件格式維持向後相容，避免回滾斷鏈。

## 10. 待拍板決策

1. MCP 是否一律預設可執行？
   - 建議：否。至少依 `risk_level` 分級，不建議全開。
2. `dangerous` 工具是否允許一鍵執行？
   - 建議：否。至少需二次確認並記錄審計。

## 11. Deferred / 後續觸發條件

目前決策：先不立即實作完整工具策略層（Policy Engine），改採最小可行治理。

### 11.1 目前先做（Now）

- 權限主軸維持：
  - 管理權限：`skills.*`、`mcp.*`、`functions.*`（CRUD）
  - 實體權限：`entity.agent.*`、`entity.dataset.*`
- 工具執行預設可用（以 Agent / Dataset 實體權限邊界為準）。
- 危險操作先以工具說明標示，並保留 UI 執行前確認機制。

### 11.2 觸發完整策略層的條件（Trigger）

符合任一條件即啟動策略層建置：

1. 正式上線第一個「會產生成本」的工具（例如付費 API、計費 MCP）。
2. 出現第一個「外部寫入/刪除/交易」型工具。
3. 單月工具費用超過預算門檻（門檻值另訂）。
4. 出現跨租戶/跨專案資料外流風險事件。

### 11.3 最小策略層落地順序（MVP）

第一階段只做三件事，不先做完整 policy engine：

1. 個人額度（每日 / 每月）：
   - 指標：呼叫次數、估算成本、實際成本（若可取得）
2. 危險工具二次確認：
   - `dangerous` 工具需 explicit confirm 才可執行
3. 稽核事件：
   - 固定記錄 `who`、`agent`、`tool`、`cost`、`status`、`timestamp`

## 12. 儀表板補強需求（角色 / 群組 / 個人統計）

### 12.1 目標

- 在儀表板提供授權治理的可觀測性，支援管理者快速判斷權限分布、風險熱點與成本異常。

### 12.2 角色統計（Role Metrics）

- 角色總數、啟用/停用數
- 每角色綁定使用者數、群組數
- 每角色權限數（含實體權限占比）
- 前 N 名高權限角色（依權限數或危險權限數排序）

### 12.3 群組統計（Group Metrics）

- 群組總數、啟用/停用數
- 每群組人數、角色數、直掛權限數
- 空群組（0 人）與孤兒群組（未綁角色/權限）
- 前 N 名高影響群組（依成員數 * 權限數估算）

### 12.4 個人統計（User Metrics）

- 使用者總數、啟用/停用數
- 每人有效權限數（展開後）
- 每人可執行 Agent 數、可存取資料集數
- 權限異常指標：
  - 超高權限使用者
  - 長期未使用但高權限帳號
  - 近 7 天危險操作次數

### 12.5 工具與成本統計（待啟用策略層後生效）

- 工具呼叫總次數、成功率、錯誤率
- 每工具成本、每人成本、每群組成本
- 額度使用率（個人 / 群組）與超額告警

### 12.6 儀表板最小交付順序

1. 第 1 波：角色/群組/個人基礎統計（不含成本）
2. 第 2 波：權限異常偵測卡片（高權限、孤兒綁定、未使用高權限）
3. 第 3 波：工具成本與額度統計（策略層啟用後）

## 13. To-Be 流程圖：Agent 去耦但保留工具邊界

### 13.1 設計原則

- Agent 與資料權限去耦：
  - 資料可見性由使用者/群組權限決定（`entity.dataset.*`）。
- 工具不做「人員私有化」：
  - 工具註冊在全域目錄，預設可被系統使用。
- Agent 保留工具邊界（Allowlist）：
  - Agent 仍定義「可用哪些工具」，此邊界屬於行為配置，不是資料授權。

### 13.2 授權與執行判斷流程（To-Be）

```text
[使用者發送請求]
        |
        v
[檢查使用者是否可執行此 Agent]
  - require: entity.agent.<agent_id>.execute
        |
     (否)----> [403 拒絕]
        |
      (是)
        v
[建立可見資料集集合]
  - 來自使用者有效權限：entity.dataset.<dataset_id>.execute
  - 與 Agent 請求範圍求交集（若有）
        |
        v
[LLM 產生 tool call 意圖]
        |
        v
[工具邊界檢查]
  - 工具是否在 Agent allowlist
        |
     (否)----> [拒絕該 tool call + 回覆不可用]
        |
      (是)
        v
[風險策略檢查（Deferred，可先最小化）]
  - 是否需二次確認
  - 是否超過額度/頻率
        |
     (否)----> [拒絕該 tool call + 記錄審計]
        |
      (是)
        v
[執行工具]
        |
        v
[工具結果回傳 LLM，並套用資料集可見性邊界]
        |
        v
[輸出最終回應 + 記錄審計]
```

### 13.3 關鍵判斷順序

1. 先判斷 Agent execute 權限。
2. 再決定可見資料集集合（最小資料暴露）。
3. 最後才允許工具執行，且必須通過 Agent allowlist。
4. 成本/危險策略檢查可先做最小版，再逐步完整化。

## 14. 以現有系統需調整的點（改造清單）

### 14.1 權限與資料模型

- 保留：
  - `entity.agent.*`、`entity.dataset.*`（核心）
  - `skills.*`、`mcp.*`、`functions.*`（管理 CRUD）
- 移除（逐步退場）：
  - 任何「以人員 RBAC 控制工具執行」的判斷邏輯
- 新增（可 Deferred）：
  - 工具風險欄位（`risk_level`、`cost_class`、`requires_confirmation`）
  - 個人/群組額度欄位（可先放設定表）

### 14.2 Agent 配置層

- Agent 不再承擔資料集授權語意。
- Agent 仍保留工具 allowlist（例如 `allowed_mcp_ids`、`allowed_skill_ids`、`allowed_function_ids`）。
- 若現有 `rag_config` 內有資料集綁定：
  - 改為「預設建議來源」或「查詢範圍提示」，不是最終授權依據。

### 14.3 後端 API 與服務

- `backend/src/api/routes/chat.py`
  - 工具執行前新增 Agent allowlist 檢查。
  - 資料檢索前改用使用者有效 `entity.dataset.*` 權限建立可見資料集集合。
- `backend/src/api/routes/agents.py`
  - 保留 Agent 工具配置 CRUD，但語意改為「行為邊界」而非「私有工具授權」。
- `backend/src/api/routes/rag.py`
  - 任何 dataset 存取以使用者有效資料集權限為準。
- `backend/src/services/access_control_service.py`
  - 保留 capability 聚合；後續可加額度/策略查詢方法。

### 14.4 前端頁面

- `frontend/src/pages/access-control.html`
  - 持續聚焦 Agent / Dataset 實體權限管理。
  - 工具執行權限（若仍可見）標示為相容舊鍵並逐步移除。
- `frontend/src/pages/agent-llm-config.html`
  - 明確區分「工具邊界設定」與「資料授權（由權限頁管理）」。
- `frontend/src/pages/rag-datasets.html`
  - 說明資料集可見性由實體權限決定，Agent 只做建議範圍。

### 14.5 稽核與儀表板

- 新增/補強稽核事件欄位：
  - `user_id`、`agent_id`、`tool_id`、`allowlist_passed`、`quota_passed`、`status`、`cost_estimate`
- 將 12 章定義的角色/群組/個人統計接入儀表板。

### 14.6 兼容遷移步驟（建議）

1. 先上線 Agent allowlist 強制檢查（不動 UI）。
2. 再將資料集讀取統一切換為 `entity.dataset.*` 決策。
3. 前端文案改版：Agent 不承載資料授權語意。
4. 最後移除工具執行 RBAC 舊判斷與舊鍵顯示。
