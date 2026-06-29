# MCP / Functions 執行流程說明

本文整理目前專案中，`MCP` 與 `Functions（工具呼叫）` 在聊天流程中的實際執行路徑，並標註程式檔案與行號，方便後續除錯與擴充。

## 1. 設定來源與前置整備

### 1.1 Agent 設定寫入（Functions 與 MCP 綁定）

- `backend/src/api/routes/agents.py:225` `update_agent_integrations()` 會接收整合設定。
- `backend/src/api/routes/agents.py:312`~`331` 將以下欄位寫入 `agent.model_config`：
  - `functions_definition_template`（兼容 `toolcall_guide`）
  - `mcp_ids`
  - `skill_ids`
- `backend/src/api/routes/agents.py:158`~`184` 回傳整合資訊時，會組出 `mcp_schemas`（優先 `mcp_ids` 對應全域 MCP，再合併 inline `mcp_config`）。

### 1.2 會話初始化時載入整合能力

- `backend/src/api/routes/chat.py:1753` 建立 `ChatRouter()`。
- `backend/src/api/routes/chat.py:1755` `_apply_custom_toolcall_guide()` 套用 agent 自訂 `functions_definition_template`。
- `backend/src/api/routes/chat.py:1762` `router._prepare_integrations()` 載入該 agent 的 skills / MCP / RAG。
- `backend/src/services/chat_router.py:363` `_prepare_integrations()` 主要行為：
  - `378`~`403`：若有 `mcp_ids`，從 `MCPConnection` 載入正式 MCP 連線設定。
  - `404`~`428`：若沒用 `mcp_ids`，才用舊版 inline `mcp_config`。
  - `480`：產生 `_allowed_tools` 白名單（skills + `mcp:<name>`）。
  - `482`~`485`：建立 `_mcp_map`（`name -> 連線設定`）供執行期查找。

## 2. Functions（工具呼叫）如何生成與被解析

### 2.1 Prompt 內的函式協定模板注入

- `backend/src/api/routes/chat.py:248` `_build_capability_prompt()` 會組裝：
  - 能力前綴（skills / mcp / rag）
  - `router._render_toolcall_guide(agent_ctx)`
- `backend/src/services/chat_router.py:488` `_toolcall_guide()` 取得模板來源：
  1. 會話級自訂 `set_toolcall_guide()`
  2. function profile template
  3. 預設 Tool-Call Protocol
- `backend/src/services/chat_router.py:520` `_render_toolcall_guide()` 把模板變數實際替換為：
  - 可用工具名
  - skills/mcp 清單
  - schema 與 RAG 狀態

### 2.2 串流中偵測工具呼叫

- `backend/src/api/routes/chat.py:2233` 開始讀 LLM 串流增量。
- `backend/src/api/routes/chat.py:2242` 先用 `_parse_tool_call_block()` 解析 `[[CALL tool=...]]`。
- `backend/src/api/routes/chat.py:2245` 若未命中，再用 `_parse_openai_tool_call_block()` 解析 `{"tool_calls":[...]}`。
- 解析器實作：
  - `backend/src/api/routes/chat.py:1229` `_parse_tool_call_block()`
  - `backend/src/api/routes/chat.py:1197` `_parse_openai_tool_call_block()`
- `backend/src/api/routes/chat.py:2248` `_apply_tool_payload_defaults()` 補常見缺省參數（如 timezone）。
- `backend/src/api/routes/chat.py:2249`~`2252` 注入附件上下文與 markdown 到工具 payload。

## 3. 工具執行分流（MCP 與非 MCP）

### 3.1 統一路由入口

- `backend/src/api/routes/chat.py:2191` 與 `2470` 都會走 `router.call_tool_async()`。
- `backend/src/services/chat_router.py:608` `call_tool_async()` 為非同步總入口。
- `backend/src/services/chat_router.py:684` `_validate_async_tool_call_request()` 先做：
  - 白名單檢查（`tool_not_allowed`）
  - 寫入 `tool_input` 事件
  - doom loop 防護

### 3.2 MCP 工具路徑

- `backend/src/services/chat_router.py:625` 若工具名稱 `mcp:` 開頭，進入 MCP 分支。
- `backend/src/services/chat_router.py:634` `_resolve_mcp_tool_name()` 會做工具名解析與快取，避免連線名不等於實際工具名。
- 傳輸分流：
  - `635`~`658`：`stdio` -> `MCPClient.stream_rpc_call_stdio()`
  - `659`~`679`：remote/ws -> `MCPClient.stream_rpc_call_ws()`，失敗再 fallback 到同步 `call_tool()`

### 3.3 非 MCP（skills）路徑

- `backend/src/services/chat_router.py:682` 非 MCP 轉同步 `call_tool()`。
- `backend/src/services/chat_router.py:964` `call_tool()` 中：
  - `1013` 起是 skill 執行流程
  - 支援 zip/prompt/hybrid、command、python handler、webhook 等型別

## 4. MCPClient 真正呼叫機制

### 4.1 HTTP / JSON-RPC / WS / stdio 支援

- `backend/src/services/mcp_client.py:286` `invoke()`：HTTP endpoint 嘗試 + JSON-RPC fallback。
- `backend/src/services/mcp_client.py:324` `rpc_call()`：HTTP JSON-RPC（`/jsonrpc`, `/rpc`）。
- `backend/src/services/mcp_client.py:356` `rpc_call_ws()`：單次 WebSocket JSON-RPC。
- `backend/src/services/mcp_client.py:386` `stream_rpc_call_ws()`：WebSocket 串流 frame。
- `backend/src/services/mcp_client.py:414` `stream_rpc_call_stdio()`：stdio 串流；`tools/call` 會優先 one-shot `invoke_stdio()`。
- `backend/src/services/mcp_client.py:557` `invoke_stdio()`：啟 subprocess，做 `initialize -> notifications/initialized -> tools/call`。

### 4.2 stdio 長連線會話管理

- `backend/src/services/mcp_client.py:636` `_StdioSession` 管理持久進程、reader thread、id 對應 queue。
- `backend/src/services/mcp_client.py:707` `_mcp_init_handshake()` 做 MCP 初始化握手。
- `backend/src/services/mcp_client.py:830` `stream_request()` 依 request id 持續吐出 frame，含 idle/total timeout 控制。

## 5. chat_stream 內的 ReAct + MCP 串流事件

### 5.1 MCP frame 正規化與進度事件

- `backend/src/api/routes/chat.py:1874` `_normalize_mcp_frame()`：把標準 JSON-RPC `result/error` 轉成統一 `{ok, result/error}`，並萃出 `_result_text`。
- `backend/src/api/routes/chat.py:2304`~`2336`：stdio MCP 工具串流執行、進度事件發送、成功/失敗分支。
- `backend/src/api/routes/chat.py:2378`~`2412`：WS MCP 工具串流執行、進度與 ETA 發送、成功/失敗分支。
- `backend/src/api/routes/chat.py:2343` 與 `2419`：工具 timeout 分支，會改走 fallback 一般回覆。

### 5.2 工具結果回到最終回答

- `backend/src/api/routes/chat.py:1897` `_observe_and_answer()`：把工具結果餵回 LLM，產生使用者可讀最終文字。
- `backend/src/api/routes/chat.py:2337`~`2341`、`2413`~`2417`：工具成功後清除工具文字，輸出最終答案並 `done`。
- `backend/src/api/routes/chat.py:2460`~`2467`：找不到 MCP 連線時，送 reroute 事件並降級一般回答。

## 6. 直接工具 API（非 chat_stream）

### 6.1 手動呼叫單一工具

- `backend/src/api/routes/chat.py:1393` `invoke_tool()`：
  - 驗證 conversation/agent
  - 補附件資料
  - `1454` 呼叫 `router.call_tool_async()`

### 6.2 工具 SSE 串流端點

- `backend/src/api/routes/chat.py:1490` `stream_tool()`：
  - 先 `_prepare_integrations()`
  - `1545` 起對 `mcp:*` 優先走 stdio/ws 串流
  - 否則 `1649` 回退 `call_tool_async()` 單段結果

## 7. 流程總結（從訊息到 MCP 執行）

1. `chat_stream` 建立 `ChatRouter` 並載入 agent integrations（含 `mcp_ids` / `functions_definition_template`）。
2. 將 tool-call guide（Functions 協定）注入 prompt，讓模型只在需要時輸出工具呼叫格式。
3. 串流解析 `[[CALL ...]]` 或 `tool_calls JSON`，整理 payload（含預設值與附件）。
4. 進入 `call_tool_async()`，先過白名單與防護，再依 `mcp:` 分流。
5. MCP 分流中依 transport 走 `stdio` 或 `ws/http`；`MCPClient` 實作實際 JSON-RPC 溝通。
6. 回傳 frame 後正規化為 `{ok, result/error}`，成功則進入 Observe 階段產出最終回答，失敗則 reroute/fallback。
