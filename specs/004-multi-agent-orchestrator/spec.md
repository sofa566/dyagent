# 功能規格：多代理者 Orchestrator（ReAct 協作模式）

**分支**：`004-multi-agent-orchestrator`
**建立日期**：2026-03-28
**狀態**：In Progress

## 背景與目標

目前 `POST /api/chat` 每次只能路由到**一個** Worker Agent，無法在單一對話中協調多個能力不同的代理者協作完成任務。例如「查本月銷售數據，並同時發通知給相關主管」需要「數據查詢 Agent」與「通知 Agent」共同完成。

本規格目標：

- 實作 **Orchestrator-level ReAct 循環**：分解任務 → 分派子代理 → 收集結果 → 合成 → 自評 → 必要時重試。
- 向下相容：單代理場景原路徑不變。
- DB 採正規化設計，新增 `multi_agent_sessions` 與 `multi_agent_tasks` 兩張表。

## 使用者故事

### US1：單一訊息觸發多代理協作（P1）

身為使用者，我可以用一句話描述包含多個子需求的任務，系統自動判斷需要哪些代理者並協調完成，最後回覆我一份合成的完整答案。

**驗收情境**

1. Given 訊息含複合意圖（如「查銷售並發通知」），When 送出，Then SSE 依序出現 `orchestrator.plan` → `agent.start` → `agent.text` → `agent.done` × N → `orchestrator.synthesizing` → `orchestrator.done`。
2. Given 合成結果不符需求，When 自評 ok=false，Then 系統重新規劃並執行（最多 `max_steps` 輪），不需使用者重送。
3. Given 訊息為簡單單一意圖，When 送出，Then 走原有單代理路徑，無 orchestrator 事件。

### US2：子代理執行結果可追蹤（P1）

身為管理者，我可以查詢每次多代理協作的 Session 紀錄，看到任務分解計畫、各子代理輸出、合成結果與 ReAct 步驟數。

**驗收情境**

1. Given 一次多代理聊天完成，When 查 `multi_agent_sessions`，Then 有正確 status/react_step/synthesis。
2. Given 查 `multi_agent_tasks`，Then 每個子任務有 task_desc/result_text/status/started_at/finished_at。

### US3：子代理失敗不中斷整體流程（P2）

身為使用者，若其中一個子代理失敗，系統仍能以其他子代理的結果合成部分答案，並在回覆中說明哪部分無法完成。

## 功能需求

- FR-001 系統 MUST 以三層策略判斷路由（快速排除 → Embedding → LLM 裁決）。
- FR-002 系統 MUST 對多代理任務執行 Orchestrator-level ReAct（分解/執行/合成/自評/重試）。
- FR-003 系統 MUST 將最終合成結果以 Router LLM 生成（不直接拼接子代理輸出）。
- FR-004 系統 MUST 將協作 Session 寫入 `multi_agent_sessions`，子任務寫入 `multi_agent_tasks`。
- FR-005 系統 MUST 透過 SSE 即時推送 `orchestrator.*` 與 `agent.*` 事件。
- FR-006 系統 MUST 在子代理失敗時繼續其他子任務（degradation: skip）。
- FR-007 系統 MUST 在合成後由 Router LLM 自評是否滿足需求，不滿足時重試。
- FR-008 系統 MUST 保持單代理路徑行為不變（向下相容）。

## 非目標

- 本期不實作 @mention 對話鏈模式（模式 B）。
- 本期不實作共享任務板模式（模式 C）。
- 本期不加入 Agent 父子層級欄位（`parent_id`、`children`）。
- 本期子代理並行執行為 Phase 2，Phase 1 僅循序。

## 成功指標

- 複合意圖訊息 100% 觸發 orchestrator 路徑（無誤判進單代理）。
- 單代理訊息 100% 走原路徑，不受影響。
- 多代理 Session 在 DB 可查，每輪 react_step 正確累計。
- 合成後自評不過時能自動重試，不需使用者重送。
