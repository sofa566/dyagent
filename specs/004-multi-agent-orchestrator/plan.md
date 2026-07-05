# 實作計畫

## 總覽

採「先資料層、再核心邏輯、最後 SSE/前端」順序，確保每個 Phase 可獨立驗收。

## Phase 1：資料層

1. 新增 SQLAlchemy 模型 `MultiAgentSession`、`MultiAgentTask`
2. 新增 Alembic migration：`20260328_0002_multi_agent_session_task.py`
3. 執行 `alembic upgrade head` 驗收

完成標準：
- migration 可正向執行
- psql 查得到兩張新表

## Phase 2：路由判斷（三層）

實作 `_classify_routing(message, workers) -> str`：

**第一層（零成本）**：
- 訊息 < 15 字 → `"single"`
- workers < 2 → `"single"`
- 無複合意圖信號 → `"single"`
  - 信號：並且/同時/另外/然後/以及/也要/and then/also；或含 @代理者名稱

**第二層（Embedding，無 LLM 費用）**：
- 計算訊息向量 vs 每個 Worker 描述向量的餘弦相似度
- 前兩高分差 > 0.2 → `"single"`（最高分 Worker）
- 前兩高分差 ≤ 0.1 → 進第三層

**第三層（LLM 裁決）**：
- 呼叫 `_decompose_tasks(message, workers, router_agent) -> dict | None`
- 解析失敗 / `multi=false` / 所有 task 同 agent_id → `"single"`
- 2+ 不同 agent_id → `"multi"`

完成標準：
- 單元測試：短訊息/無信號 → single；「查並通知」訊息 → multi

## Phase 3：子任務執行

1. `_build_execution_waves(tasks)` - 拓撲排序（Phase 1 循序，每波 1 個）
2. `_inject_prior_context(task_desc, prior_results) -> str` - 前置結果注入
3. `_run_subtask_stream(task_row, enriched_msg, db, current_user) -> AsyncGenerator`：
   - 更新 `MultiAgentTask.status = 'running'`
   - 呼叫現有 `chat_stream()`
   - 轉譯 SSE：`text` → `agent.text`；其他附加 agent 標識直接轉發；`done` 不轉發
   - 寫入 `task_row.result_text`、`finished_at`
   - yield `agent.start`、`agent.done` / `agent.error`

完成標準：
- 呼叫一次 _run_subtask_stream，DB task 有正確 result_text 與 status

## Phase 4：合成與自評

實作 `_synthesize_and_evaluate(session, all_results, original_message, db) -> tuple[str, bool]`：

**合成（Synthesize）**：
```
Router LLM prompt：
  [原始需求] {original_message}
  [子代理結果]
  - {agent_name}: {result_text[:2000]}
  - ...
  請整合以上資訊，回覆使用者。
```

**自評（Evaluate）**：
```
Router LLM prompt：
  [原始需求] {original_message}
  [你的回覆] {synthesis}
  請判斷此回覆是否完整回應了使用者需求。
  輸出 JSON：{"ok": true|false, "reason": "..."}
```

完成標準：
- 合成文字非空；自評 JSON 可解析；ok=false 時 reason 非空

## Phase 5：Orchestrator 主循環

實作 `_multi_agent_orchestrator(message, plan, workers, db, current_user, router_agent)`：

```python
session = MultiAgentSession(user_message=message, ...)
db.add(session); db.commit()

for step in range(session.max_steps):
    session.react_step = step + 1
    session.status = 'planning' if step > 0 else 'running'

    tasks = _create_task_rows(session, plan, db)
    waves = _build_execution_waves(tasks)

    yield orchestrator.plan 事件

    completed_results = {}
    for wave in waves:
        for task in wave:
            async for event in _run_subtask_stream(task, ...):
                yield event
            completed_results[task.task_index] = task

    session.status = 'synthesizing'
    yield orchestrator.synthesizing 事件

    synthesis, eval_ok = await _synthesize_and_evaluate(session, completed_results, message, db)

    # 串流合成文字給前端
    for chunk in synthesis_chunks:
        yield agent.text 事件（agent_name='Router'）

    session.synthesis = synthesis
    session.eval_ok = eval_ok

    if eval_ok:
        session.status = 'done'
        db.commit()
        yield orchestrator.done
        return

    # 不滿足 → 重新分解
    yield orchestrator.retry 事件
    plan = _decompose_tasks(message, workers, router_agent)
    if not plan:
        break

session.status = 'failed'
db.commit()
yield orchestrator.done（failed=True）
```

完成標準：
- 完整 SSE 流：orchestrator.plan → agent.* × N → synthesizing → agent.text → done/retry

## Phase 6：接入 chat_entry_router

修改 `chat.py` 的 `chat_entry_router`（約 +20 行）：

```python
workers = db.query(Agent).filter(Agent.id != router_agent.id).all()

routing = _classify_routing(message=message, workers=workers)
if routing == 'multi':
    plan = _decompose_tasks(message=message, workers=workers, router_agent=router_agent)
    if plan and plan.get('multi') and len(plan.get('tasks', [])) >= 2:
        return StreamingResponse(
            _multi_agent_orchestrator(message, plan, workers, db, current_user, router_agent),
            media_type='text/event-stream',
        )

# 原有單代理流程（不變）
worker, route_reason = _pick_worker_agent(...)
```

完成標準：
- 複合訊息走 orchestrator 路徑；簡單訊息走原路徑

## Phase 7：前端 SSE 事件處理（chat.html）

新增事件處理：
- `orchestrator.plan` → 顯示任務清單（可折疊）
- `agent.start` → 顯示「[代理名稱] 開始處理...」badge
- `agent.text` → 同 `text`，前綴代理名稱標籤
- `agent.done` → 顯示 ✓ 完成 badge
- `orchestrator.synthesizing` → 顯示「Router 合成中...」
- `orchestrator.retry` → 顯示「重新規劃中（第 N 輪）...」
- `orchestrator.done` → 同 `done`，結束串流

## 風險與對策

| 風險 | 對策 |
|------|------|
| LLM 分解失敗 | 回傳 None → 降級單代理 |
| 子代理超時 | 繼承現有 MCP_TOOL_TIMEOUT_SEC，task 標記 failed |
| 自評無限重試 | max_steps 上限，超過後 status=failed 並回傳最後合成結果 |
| 單代理回歸 | _classify_routing 第一層零成本快速排除 |
