# 實作計畫（清楚版）

## 總覽

本計畫採「先相容、再切換、最後優化」原則，避免一次性破壞既有聊天流程。

## Phase 1：Schema 擴充（不破壞）

1. `agents` 新增 `system_prompt`、`function_profile_id`、`is_router`
2. 建立 `function_profiles`
3. 建立 `rag_datasets`
4. 建立 `llm_turns`

完成標準：

- migration 可正向/反向執行
- 舊資料不需即時搬遷即可啟動

## Phase 2：後端讀取路徑相容

1. Prompt 讀取回退順序上線（新欄位優先）
2. Function Profile 解析上線（無 profile 時回退舊 `toolcall_guide`）
3. RAG 資料集 scope 驗證上線（private 僅能綁定同 agent）

完成標準：

- 舊代理者可正常聊天
- 新代理者可使用新欄位

## Phase 3：後端 API

1. Functions CRUD + selectable
2. Agent prompt API + function-profile 綁定 API
3. RAG global/private datasets API + bindings API
4. `POST /api/chat` Router 入口（先單 worker 轉派）

完成標準：

- API contract tests 全數通過

## Phase 4：前端

1. 導覽列新增 `Functions`
2. 新增 `functions.html`（列表/新建/編輯/刪除）
3. `agent-llm-config.html`：
   - 移除 Function 建立能力
   - 僅保留從清單綁定
   - RAG 區塊新增私有資料集建立

完成標準：

- Functions 建立僅 admin 可見與可操作
- agent_admin 可在 agent 設定頁綁定 Functions

## Phase 5：Router-Worker 編排落地

1. 系統 seed 主代理（Router）
2. 一般使用者聊天改走 `POST /api/chat`
3. Router 決策寫入 events（decision/forward/fallback）

完成標準：

- 一般使用者無需選代理者即可正常使用

## Phase 6：可觀測性與安全

1. 每輪寫入 `llm_turns`（usage/cost/latency/status）
2. 敏感資料集 `restricted` 政策驗證
3. 監控面板新增路由命中率與轉派失敗率

完成標準：

- 可從後台追蹤每輪上下文與路由決策

## Migration 細節

### M1（新增欄位/新表）

- 僅新增，不刪舊欄位

### M2（讀寫切換）

- 先改 read-path，後改 write-path

### M3（清理）

- 待運行穩定後，才評估是否隱藏舊欄位 UI

## 風險與對策

- 風險：Router 判錯代理者  
  對策：加入 confidence 與 fallback 代理者

- 風險：私有資料集被錯綁  
  對策：server-side 強制檢查 dataset.agent_id == target agent id

- 風險：Functions 模板漂移  
  對策：全域 profile + version 欄位與審計記錄
