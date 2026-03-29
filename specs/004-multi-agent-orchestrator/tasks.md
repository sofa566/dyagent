# 任務清單（實作順序）

## M1：資料層

- [x] T001 新增 SQLAlchemy 模型 `MultiAgentSession`（`backend/src/models/__init__.py`）
- [x] T002 新增 SQLAlchemy 模型 `MultiAgentTask`（`backend/src/models/__init__.py`）
- [x] T003 Alembic migration：`20260328_0002_multi_agent_session_task.py`
- [x] T004 執行 `alembic upgrade head`，確認兩張表建立成功

## M2：路由判斷

- [x] T010 實作 `_classify_routing(message, workers) -> str`（三層：快速/embedding/LLM）
- [x] T011 實作 `_decompose_tasks(message, workers, router_agent) -> dict | None`（LLM 裁決）

## M3：子任務執行

- [x] T020 實作 `_build_execution_waves(tasks) -> list[list[dict]]`（拓撲排序，Phase 1 循序）
- [x] T021 實作 `_inject_prior_context(task_desc, prior_results) -> str`
- [x] T022 實作 `_run_subtask_stream(...)` → AsyncGenerator（SSE 轉譯 + DB 寫入）

## M4：合成與自評

- [x] T030 實作 `_synthesize_and_evaluate(session, all_results, original_message, db) -> tuple[str, bool]`
- [ ] T031 驗收：合成文字非空；自評 JSON 可解析；ok=false 時 reason 非空

## M5：Orchestrator 主循環

- [x] T040 實作 `_multi_agent_orchestrator(...)` → AsyncGenerator（完整 ReAct 循環含重試）
- [x] T041 實作 `_create_task_rows(session, plan, db)`（建立 MultiAgentTask 列）

## M6：接入路由入口

- [x] T050 修改 `chat_entry_router`：加入 `_classify_routing` + `_multi_agent_orchestrator` 分支

## M7：前端

- [x] T060 `chat.html`：處理 `orchestrator.plan` 事件（顯示任務清單）
- [x] T061 `chat.html`：處理 `agent.start` / `agent.text` / `agent.done` / `agent.error`
- [x] T062 `chat.html`：處理 `orchestrator.synthesizing` / `orchestrator.retry` / `orchestrator.done`

## M8：測試與驗收

- [ ] T070 單元測試：`_classify_routing` 各層判斷（快路徑/embedding/LLM）
- [ ] T071 整合測試：複合訊息觸發 orchestrator，DB session/task 有正確資料
- [ ] T072 整合測試：eval_ok=false 觸發重試，react_step 正確累加
- [ ] T073 回歸測試：簡單訊息走原路徑，SSE 無 orchestrator 事件
- [ ] T074 失敗測試：子代理失敗 → agent.error + 繼續；全失敗 → fallback 回覆
