# 多代理協作架構方案（修訂版）

## Context

目前聊天路由每次只選一個 Worker，無法在單一對話中協調多個能力不同的 Agent 協作。
本計畫實作完整的 **Orchestrator-level ReAct 循環**，包含任務分解、子代理執行、結果合成，
以及合成結果不符需求時的重試機制。DB 採正規化設計，不遷就相容性。

---

## 架構：Orchestrator ReAct 循環

```
使用者訊息
    ↓
[判斷路徑] 三層過濾（見下文）
    ├─ 單代理 → 原有 chat_stream()（不變）
    └─ 多代理 → Orchestrator ReAct 循環
           ↓
    Plan：LLM 分解任務 → 產生 MultiAgentSession + tasks
           ↓
    Act：依波次執行子代理（循序/並行）
    每個子代理走完整 ReAct + 工具呼叫
           ↓
    Observe：收集所有子代理 result_text
           ↓
    Synthesize：Router LLM 合成最終回覆
    （可閱讀所有子代理的完整輸出 + 原始問題）
           ↓
    Evaluate：Router LLM 自評是否滿足需求
    ├─ 滿足 → orchestrator.done，回給使用者
    └─ 不滿足 → 重新 Plan（react_step++，最多 max_steps）
```

---

## 單代理 vs 多代理路徑判斷（三層）

### 第一層：快速排除（不呼叫 LLM，零成本）

以下任一條件成立 → **直接走單代理**：
- 訊息長度 < 15 字元
- 可用 workers < 2
- 無複合意圖信號（複合連接詞：並且/同時/另外/然後/以及/也要/and then/also；
  或訊息含 @代理者名稱；或多動詞+不同受詞）

### 第二層：Embedding 能力距離（不呼叫 LLM）

計算訊息向量與每個 Worker 描述向量的餘弦相似度：
- **前兩高分差 > 0.2**（一個 Worker 明顯更適合）→ 單代理（選最高分）
- **前兩高分差 ≤ 0.1**（兩者都相關）→ 進入第三層

### 第三層：LLM 裁決（必要時才呼叫）

呼叫 LLM（tier='cloud'）輸出結構化 JSON：
```json
{"multi": true, "tasks": [
  {"task": "查本月銷售數據", "agent_id": "...", "depends_on": null},
  {"task": "發通知給主管", "agent_id": "...", "depends_on": [0]}
]}
```
- 解析失敗 / `multi: false` / 所有 tasks 同一 agent_id → 單代理降級
- tasks 含 2+ 不同 agent_id → 多代理路徑

---

## DB 正規化設計（新增兩張表）

```python
class MultiAgentSession(Base):
    __tablename__ = 'multi_agent_sessions'
    id              = GUID PK
    conversation_id = GUID FK → conversations.id
    user_message    = Text          # 原始使用者訊息
    router_agent_id = GUID FK → agents.id
    status          = Enum(planning, running, synthesizing, evaluating, done, failed)
    react_step      = Integer default 0
    max_steps       = Integer default 3
    plan_json       = JSON          # LLM 分解出的任務計畫
    synthesis       = Text          # 最終合成回覆
    eval_ok         = Boolean       # 最後一次自評是否通過
    created_at, updated_at

class MultiAgentTask(Base):
    __tablename__ = 'multi_agent_tasks'
    id              = GUID PK
    session_id      = GUID FK → multi_agent_sessions.id
    task_index      = Integer
    agent_id        = GUID FK → agents.id
    task_desc       = Text          # 子任務描述（注入後）
    depends_on      = JSON          # [task_index, ...]
    status          = Enum(pending, running, done, failed, skipped)
    result_text     = Text          # 子代理完整輸出
    error           = Text
    started_at, finished_at
```

新增 Alembic migration：`20260328_0002_multi_agent_session_task.py`

---

## 關鍵檔案

| 檔案 | 改動說明 |
|------|----------|
| `backend/src/api/routes/chat.py` | 新增 6 個函式 + 修改 `chat_entry_router` |
| `backend/src/models/__init__.py` | 新增 `MultiAgentSession`、`MultiAgentTask` |
| `backend/alembic/versions/20260328_0002_...py` | 新建 migration |
| `frontend/src/pages/chat.html` | 處理新 SSE 事件（orchestrator.*、agent.*）|

---

## 新增函式（chat.py）

### 1. `_classify_routing(message, workers) -> str`
回傳 `"single"` 或 `"multi"`，實作三層判斷邏輯。

### 2. `_decompose_tasks(message, workers, router_agent) -> dict | None`
LLM 分解，回傳 `{multi, tasks}` 或 None（失敗時降級）。

### 3. `_build_execution_waves(tasks) -> list[list[dict]]`
拓撲排序：依 `depends_on` 分波次（同波可並行）。
首版：全部循序（每波 1 個），Phase 2 加並行。

### 4. `_inject_prior_context(task_desc, prior_results) -> str`
`[背景資訊]\n...\n\n[使用者原始需求]\n{task_desc}`
每個前置結果最多 2000 字元。

### 5. `_run_subtask_stream(task_row, enriched_msg, db, current_user) -> AsyncGenerator`
- 更新 `MultiAgentTask.status` 為 running
- 呼叫現有 `chat_stream()`，轉譯 SSE：
  - `text` → `agent.text`（加 agent_id/name/task_index）
  - `react/tool_*` → 附加標識直接轉發
  - `done` → 不轉發
- 寫入 `MultiAgentTask.result_text`
- yield `agent.start`、`agent.done`/`agent.error`

### 6. `_synthesize_and_evaluate(session, all_results, original_message, db) -> tuple[str, bool]`
- 呼叫 Router LLM 合成最終回覆
- 呼叫 Router LLM 自評（prompt 含原始需求 + 合成結果）
- 回傳 `(synthesis_text, eval_ok)`

### 7. `_multi_agent_orchestrator(message, plan, workers, db, current_user, router_agent) -> AsyncGenerator`
主協調迴圈：
```python
session = MultiAgentSession(...)  # 建立 session
db.add(session); db.commit()

for step in range(session.max_steps):
    session.react_step = step + 1

    # 建立本輪 tasks（重試時重新規劃）
    waves = _build_execution_waves(tasks)
    yield orchestrator.plan 事件

    completed_results = {}
    for wave in waves:
        for task in wave:  # Phase 1 循序
            async for event in _run_subtask_stream(...):
                yield event
            completed_results[task.task_index] = task.result_text

    # 合成
    yield orchestrator.synthesizing 事件
    synthesis, eval_ok = await _synthesize_and_evaluate(session, completed_results, message, db)
    yield agent.text (synthesis 逐字串流)

    if eval_ok:
        session.status = 'done'
        db.commit()
        yield orchestrator.done
        return

    # 不滿足 → 重新規劃
    yield orchestrator.retry 事件
    plan = _decompose_tasks(message, workers, router_agent)  # 重新分解
    if not plan:
        break  # 降級結束

# 超過步驟上限
session.status = 'failed'
db.commit()
yield orchestrator.done（含 failed=True）
```

---

## SSE 事件（新增）

| type | 說明 | 關鍵欄位 |
|------|------|----------|
| `orchestrator.plan` | 任務分解計畫 | `total_tasks`, `tasks[]`, `react_step` |
| `agent.start` | 子代理開始 | `agent_id`, `agent_name`, `task_index`, `overall_progress` |
| `agent.text` | 子代理文字增量 | `agent_id`, `agent_name`, `task_index`, `delta` |
| `agent.done` | 子代理完成 | `agent_id`, `task_index`, `ok`, `result_preview` |
| `agent.error` | 子代理失敗 | `agent_id`, `task_index`, `error`, `degradation` |
| `orchestrator.synthesizing` | 開始合成 | `react_step` |
| `orchestrator.retry` | 不滿足，重試 | `react_step`, `reason` |
| `orchestrator.done` | 全部完成 | `conversation_id`, `completed`, `failed`, `react_steps_used` |

---

## 失敗降級

| 情況 | 處理 |
|------|------|
| 第一/二層快速排除 | 直接走單代理，原路徑不變 |
| LLM 分解失敗/解析錯誤 | 降級單代理 |
| 子代理失敗 | `agent.error` + skip，其他繼續 |
| 依賴任務失敗 | 後置任務 `dependency_failed` 跳過 |
| 合成後自評不過 | 重新 Plan（最多 max_steps 次） |
| 超過步驟上限 | 以最後合成結果回覆，`orchestrator.done(failed=True)` |
| 全部子任務失敗 | `_fallback_general_answer()` 保底 |

---

## 實作步驟

### Phase 1（核心）
1. 新增 `MultiAgentSession`、`MultiAgentTask` 模型 + migration
2. `_classify_routing()`（三層判斷）
3. `_decompose_tasks()`
4. `_build_execution_waves()`（循序版）
5. `_inject_prior_context()`
6. `_run_subtask_stream()`
7. `_synthesize_and_evaluate()`（含自評）
8. `_multi_agent_orchestrator()`（含 ReAct 重試）
9. 修改 `chat_entry_router` 加入分支

### Phase 2（優化）
- `_build_execution_waves()` 支援真正並行（asyncio.gather + Queue 合并 SSE）
- 前端進度面板 UI

---

## 驗證方式

1. **單代理回歸**：短訊息/無複合信號 → 走原路徑，無新 SSE 事件
2. **多代理觸發**：「查銷售數據並同時發通知給主管」→ orchestrator.plan → agent.* × N → orchestrator.synthesizing → orchestrator.done
3. **重試觸發**：設計一個子代理故意輸出不完整結果 → eval_ok=false → orchestrator.retry → 第二輪
4. **降級**：LLM 回傳無效 JSON → 走單代理，無 500
5. **DB 驗證**：`SELECT * FROM multi_agent_sessions` 有正確 status/react_step/synthesis
