# 功能規格：Agent System Prompt + ReAct Context 治理 + Router-Worker 編排

**分支**：`003-agent-system-prompt-react-context`  
**建立日期**：2026-03-24  
**狀態**：Implemented

## 背景與目標

目前系統已具備動態代理者、MCP、Skills、RAG 與聊天能力，但仍有三個痛點：

1. 每個代理者缺少專屬 system prompt 的明確欄位與治理策略。
2. ReAct 每輪送給模型的上下文缺少一致結構與可審計資料模型。
3. 使用者需手動選代理者，實務上不符合一般使用者行為；需要主代理先接收並分派。

本規格目標：

- 引入代理者專屬 `system_prompt`。
- 將工具呼叫協定（Functions）全域化管理，代理者只做綁定。
- 將 RAG 分成「公有全域資料集」與「代理者私有資料集」。
- 引入 Router-Worker 架構（先不強制使用 LangGraph）。
- 保持既有功能相容，避免破壞性升級。

## 使用者故事

### US1：代理者專屬 system prompt（P1）

身為 Agent 管理者，我可以為每個代理者設定獨立 system prompt，讓代理者行為符合部門任務。

**驗收情境**

1. Given 已有代理者，When 設定 `system_prompt`，Then 後續聊天會套用該 prompt。
2. Given `system_prompt` 未設定，Then 系統會回退既有欄位與預設值，不中斷服務。

### US2：Functions 全域管理與代理者綁定（P1）

身為 admin，我可以建立全域 Functions 協定；身為 agent_admin，我可以在代理者設定頁綁定要用的 Function。

**權限規則（強制）**

- 建立/編輯/刪除 Functions：僅 `admin`
- 代理者加入/綁定 Functions：`agent_admin`（於 `agent-llm-config.html`）

### US3：RAG 公私有資料集（P1）

身為管理者/代理者管理者，我可區分公有與私有資料集，避免機敏文件被非授權代理者使用。

**規則**

- 公有資料集（global）：全域管理、可被多代理者綁定。
- 私有資料集（agent_private）：僅能在 `agent-llm-config.html` 建立，且只能給該代理者使用。

### US4：Router-Worker 主從編排（P1）

身為一般使用者，我不需要選代理者；問題先交由主代理，再分派給能解題的 worker 代理者。

**驗收情境**

1. Given 一般使用者進聊天頁，When 發問，Then 請求先進 Router Agent。
2. Given Router 判斷部門，Then 系統轉派到對應 Worker 並回覆。
3. Given Router 信心不足，Then 回退到預設策略（通用代理或澄清問題）。

**目前實作備註（P1 範圍）**

- Router 分派策略目前採「關鍵字/規則 + 預設 worker 回退」。
- 本期已達成統一入口與可審計路由事件；未引入 LangGraph 或複雜評分路由器。

## 功能需求

- FR-001 系統 MUST 支援代理者 `system_prompt` 欄位。
- FR-002 系統 MUST 提供 Functions 全域管理頁與 API。
- FR-003 系統 MUST 限制 Functions 建立權限為 `admin`。
- FR-004 系統 MUST 允許 `agent_admin` 在代理者設定頁綁定 Functions。
- FR-005 系統 MUST 支援 RAG 資料集 scope：`global` / `agent_private`。
- FR-006 系統 MUST 限制 `agent_private` 資料集僅能被對應 agent 使用。
- FR-007 系統 MUST 提供 `POST /api/chat` 作為使用者統一入口（不需手選代理者）。
- FR-008 系統 MUST 記錄 Router 分派事件（decision/forward/fallback）。
- FR-009 系統 MUST 保持 Message 與內部執行資料分層存放。

## 非目標

- 本期不強制導入 LangGraph。
- 本期不改動前端整體視覺語言。

## 成功指標

- 一般使用者 100% 可在不選代理者下完成對話。
- Functions 新增操作 100% 僅 admin 可執行。
- agent_private 資料集不可被其他代理者查詢（0 容忍）。
- 每輪可查到路由決策與上下文審計資料。
