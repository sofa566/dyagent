# 資料模型設計

## 1) multi_agent_sessions（新表）

用途：記錄每次 Orchestrator 協作的完整生命週期。

```python
class MultiAgentSession(Base):
    __tablename__ = 'multi_agent_sessions'

    id              = Column(GUID(), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(GUID(), ForeignKey('conversations.id'), nullable=False)
    router_agent_id = Column(GUID(), ForeignKey('agents.id'), nullable=False)
    user_message    = Column(Text, nullable=False)     # 原始使用者訊息
    status          = Column(
                        Enum('planning','running','synthesizing','evaluating','done','failed',
                             name='mas_status'),
                        nullable=False, default='planning')
    react_step      = Column(Integer, nullable=False, default=0)
    max_steps       = Column(Integer, nullable=False, default=3)
    plan_json       = Column(JSON, default=dict)        # LLM 分解出的任務計畫（每輪覆寫）
    synthesis       = Column(Text, nullable=True)       # 最終合成回覆
    eval_ok         = Column(Boolean, nullable=True)    # 最後一次自評結果
    created_at      = Column(DateTime, default=datetime.now)
    updated_at      = Column(DateTime, default=datetime.now, onupdate=datetime.now)
```

## 2) multi_agent_tasks（新表）

用途：記錄每個子任務的分配、執行與結果。

```python
class MultiAgentTask(Base):
    __tablename__ = 'multi_agent_tasks'

    id          = Column(GUID(), primary_key=True, default=uuid.uuid4)
    session_id  = Column(GUID(), ForeignKey('multi_agent_sessions.id'), nullable=False)
    task_index  = Column(Integer, nullable=False)       # 本輪中的序號（0-based）
    agent_id    = Column(GUID(), ForeignKey('agents.id'), nullable=False)
    task_desc   = Column(Text, nullable=False)          # 注入前置背景後的任務描述
    depends_on  = Column(JSON, default=list)            # [task_index, ...] 前置依賴
    status      = Column(
                    Enum('pending','running','done','failed','skipped',
                         name='mat_status'),
                    nullable=False, default='pending')
    result_text = Column(Text, nullable=True)           # 子代理完整輸出文字
    error       = Column(Text, nullable=True)
    started_at  = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
```

## 3) 既有表的對應關係

```
multi_agent_sessions
  ├─ conversation_id → conversations（主 Conversation 掛 Router Agent）
  └─ router_agent_id → agents

multi_agent_tasks
  ├─ session_id → multi_agent_sessions
  └─ agent_id → agents（子代理）

EventPart（現有）
  └─ 保留 orchestrator.* / agent.* 事件的 SSE 可觀測性記錄
     （不用於取代 multi_agent_sessions）

llm_turns（現有）
  └─ 每個子代理的 LLM 呼叫審計仍寫入 llm_turns
     （conversation_id = 子代理自己的 conversation）
```

## 4) status 流轉

### MultiAgentSession.status

```
planning → running → synthesizing → evaluating
                                         ├─ eval_ok=true → done
                                         └─ eval_ok=false → planning（重試）
                                                                └─ 超過 max_steps → failed
```

### MultiAgentTask.status

```
pending → running → done
                 → failed
                 → skipped（依賴任務失敗，或已超過步驟上限）
```

## 5) Alembic Migration

檔案：`backend/alembic/versions/20260328_0002_multi_agent_session_task.py`

操作：

- CREATE TABLE `multi_agent_sessions`
- CREATE TABLE `multi_agent_tasks`
- CREATE TYPE `mas_status`（若 PostgreSQL Enum）
- CREATE TYPE `mat_status`
