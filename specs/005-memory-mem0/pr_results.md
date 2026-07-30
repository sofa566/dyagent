# PR 描述草稿：005-memory-mem0

## 變更摘要

本次 PR 完成多代理雙層記憶（Mem0）主要功能，包含設定、Provider 抽象、聊天流程接線、治理 API、路由策略與測試。

- 設定與抽象層
  - 新增記憶與路由策略設定：`AGENT_MEMORY_PROVIDER`、`AGENT_MEMORY_ROUTING_MODE`、`ROUTER_ASSIGNMENT_MODE`、讀寫開關、Top-K。
  - 建立 `MemoryProvider` 介面與 `memory_service` 門面，統一讀寫/forget 入口。
  - 提供 `mock_provider`、`mem0_provider`（`mem0_oss` / `mem0_platform`）。

- Mem0 能力強化
  - 實作 scope 對映：`user_scope`、`agent_scope`、`interaction_scope`、`global_scope`。
  - 實作 fail-open 與錯誤分類：`timeout`、`auth`、`provider_unavailable`。
  - 將 `error_code` 納入 health/retrieve/write 可觀測資料。

- 聊天流程接線
  - `chat_stream` 注入長期記憶檢索結果（`memory_context`）。
  - 回覆完成後由 Master/Orchestrator gate 寫回長期記憶（Worker 不直寫）。
  - 新增 `memory.retrieve` / `memory.write` 事件紀錄（`EventPart`）。
  - 新增短期記憶 Redis helper（最近 N 則 + TTL），並接入歷史上下文組裝（Redis 優先，DB 回退）。

- 治理 API
  - `GET /api/memory/health`
  - `POST /api/memory/search`
  - `POST /api/memory/users/{user_id}/forget`
  - `POST /api/chat/memory/forget`

- 路由策略
  - 完成 `ROUTER_ASSIGNMENT_MODE` 四模式：`description_only` / `skill_first` / `hybrid` / `memory_first`。
    - 主開關 / 主流程選擇
    - 決定這次分派要走哪種大路徑（是否進入記憶加權流程）

  - 完成 `AGENT_MEMORY_ROUTING_MODE` 子策略影響（含可觀測差異測試）。
    - 四模式：`mem_disabled` / `mem_boost` / `mem_hybrid`/`mem_dominant`。
    - 記憶子開關 / 記憶分數算法
    - 只有在主流程已經決定「要用記憶」時，才定義記憶怎麼算、影響多大）

  - 實際解釋（你現在的規格語意）：
    1. ROUTER_ASSIGNMENT_MODE=`description_only`
       - 完全不走記憶決策
       - AGENT_MEMORY_ROUTING_MODE 被忽略
    2. ROUTER_ASSIGNMENT_MODE=`skill_first`
       - 以技能命中為主
       - 記憶通常只在接近分數/tie-break 補分
       - 這時 AGENT_MEMORY_ROUTING_MODE 影響有限（偏輔助）
    3. ROUTER_ASSIGNMENT_MODE=`hybrid` 或 `memory_first`
       - 會進入記憶參與的評分流程
       - 這時才真正看 AGENT_MEMORY_ROUTING_MODE：
         - `mem_disabled`：記憶只注入上下文，不加分
         - `mem_boost`：記憶僅補分，不主導
         - `mem_hybrid`：多 scope 記憶融合後加權
         - `mem_dominant`：記憶可主導，命中高可提前定案

## 主要風險與因應

- 風險：記憶層故障可能影響聊天可用性
  - 因應：Provider 例外採 fail-open，聊天主流程不中斷；回傳與事件附帶 `error_code` 供排障。

- 風險：scope 過濾錯誤導致跨範圍資料誤用或查不到
  - 因應：scope 對映與可見性判斷集中在 provider，並以單元/整合測試覆蓋 `user_scope`、`agent_scope`、forget 流程。

- 風險：新增記憶流程導致既有 chat/rag 回歸
  - 因應：新增 T045 回歸測試，驗證記憶檢索失敗時 chat/rag 路徑仍可正常回覆。

- 風險：短期記憶資料量成長與順序錯誤
  - 因應：Redis 寫入時 `LTRIM` 固定最近 N 則，`EXPIRE` 控制 TTL；讀取時統一轉為舊到新順序。

## 驗證結果

- 單元測試
  - `test_mem0_provider.py`：scope 對映、錯誤分類、fail-open 相關行為通過。
  - `test_memory_service.py`：Top-K、隔離與降級行為通過。
  - `test_redis_short_term_memory.py`：最近 N 則、TTL、順序行為通過。
  - `test_multi_agent_orchestrator.py`（策略子集）：`ROUTER_ASSIGNMENT_MODE` / `AGENT_MEMORY_ROUTING_MODE` 行為通過。
  - `test_chat.py`：治理 API 與 forget 端點行為通過。

- 整合測試
  - `test_memory_scope_reuse_flow.py`：`user_scope` 跨代理、`agent_scope` 跨使用者重用通過。
  - `test_memory_forget_flow.py`：forget 後不可再檢索通過。
  - `test_memory_chat_rag_regression.py`：既有 chat/rag 路徑不退化通過。

## 測試地圖

- Provider / Scope / Fail-open
  - `backend/tests/unit/test_mem0_provider.py`
  - `backend/tests/unit/test_memory_service.py`

- 治理 API
  - `backend/tests/unit/test_chat.py`

- 短期記憶（Redis）
  - `backend/tests/unit/test_redis_short_term_memory.py`

- 路由策略（T046 / T047）
  - `backend/tests/unit/test_multi_agent_orchestrator.py`

- 整合驗收（T042 ~ T045）
  - `backend/tests/integration/test_memory_scope_reuse_flow.py`
  - `backend/tests/integration/test_memory_forget_flow.py`
  - `backend/tests/integration/test_memory_chat_rag_regression.py`

## 最終驗收清單

### 最小必要驗收

```bash
pytest backend/tests/unit/test_mem0_provider.py backend/tests/unit/test_memory_service.py backend/tests/unit/test_redis_short_term_memory.py -q
pytest backend/tests/unit/test_multi_agent_orchestrator.py -k "router_assignment_mode or memory_routing_mode" -q
pytest backend/tests/integration/test_memory_scope_reuse_flow.py backend/tests/integration/test_memory_forget_flow.py backend/tests/integration/test_memory_chat_rag_regression.py -q
```

### 完整驗收（本次改動相關）

```bash
pytest backend/tests/unit/test_chat.py backend/tests/unit/test_mem0_provider.py backend/tests/unit/test_memory_service.py backend/tests/unit/test_redis_short_term_memory.py -q
pytest backend/tests/unit/test_multi_agent_orchestrator.py -k "router_assignment_mode or memory_routing_mode" -q
pytest backend/tests/integration/test_memory_scope_reuse_flow.py backend/tests/integration/test_memory_forget_flow.py backend/tests/integration/test_memory_chat_rag_regression.py -q
ruff check backend/src/services/memory_providers/base.py backend/src/services/memory_providers/mem0_provider.py backend/src/services/memory_service.py backend/src/services/redis_service.py backend/src/api/routes/chat.py backend/tests/unit/test_mem0_provider.py backend/tests/unit/test_memory_service.py backend/tests/unit/test_redis_short_term_memory.py backend/tests/unit/test_chat.py backend/tests/integration/test_memory_scope_reuse_flow.py backend/tests/integration/test_memory_forget_flow.py backend/tests/integration/test_memory_chat_rag_regression.py
```
