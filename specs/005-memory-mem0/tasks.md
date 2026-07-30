# Tasks: 多代理雙層記憶（Mem0）

## M1：設定與抽象層

- [ ] T001 新增記憶設定：`AGENT_MEMORY_PROVIDER`、`AGENT_MEMORY_ROUTING_MODE`、讀寫開關與 Top-K（`backend/src/core/config.py`）
- [ ] T001-A 新增主代理分派設定：`ROUTER_ASSIGNMENT_MODE`（`description_only`/`hybrid`/`skill_first`/`memory_first`）
- [ ] T002 新增 `MemoryProvider` 介面（`backend/src/services/memory_providers/base.py`）
- [ ] T003 新增 `memory_service` 門面，統一 `retrieve_context`、`write_memory`、`forget_user`（`backend/src/services/memory_service.py`）
- [ ] T004 新增 `mock_provider`（`backend/src/services/memory_providers/mock_provider.py`）

## M2：Mem0 Provider

- [ ] T010 新增 `mem0_provider`，支援 `mem0_oss`（`Memory.from_config`）
- [ ] T011 新增 `mem0_provider`，支援 `mem0_platform`（`MemoryClient`/`AsyncMemoryClient`）
- [ ] T012 實作 scope 對映：user/agent/interaction/global
- [ ] T013 實作 fail-open 與錯誤分類（timeout、auth、provider_unavailable）

## M3：聊天流程接線

- [ ] T020 在 `chat_stream` 內加入長期記憶檢索並拼接 `memory_context`（`backend/src/api/routes/chat.py`）
- [ ] T021 在回覆完成後寫回長期記憶（偏好/經驗分流）
- [ ] T022 擴充短期記憶 Redis helper（最近 N 則 + TTL）
- [ ] T023 新增 `memory.retrieve` / `memory.write` 事件寫入（`EventPart`）
- [ ] T024 修改主代理分派流程，導入 `ROUTER_ASSIGNMENT_MODE` 主決策（`backend/src/api/routes/chat.py`）
- [ ] T025 在 `ROUTER_ASSIGNMENT_MODE=hybrid` 時套用評分公式：`total_score = 0.40*desc_score + 0.35*skill_score + 0.25*memory_score`
- [ ] T026 實作 `AGENT_MEMORY_ROUTING_MODE` 子策略（`memory_first`/`skill_first`/`description_only`/`hybrid`）對 `memory_score` 的影響
- [ ] T027 實作主決策與子策略交互規則：
  - `description_only` 忽略 `AGENT_MEMORY_ROUTING_MODE`
  - `skill_first` 僅在技能分數接近時使用記憶補充分數
  - `memory_first`/`hybrid` 由子策略決定 `memory_score` 計算與門檻
- [ ] T028 確保長期記憶寫入由 Master/Orchestrator gate 執行，Worker 只產出候選結果

## M4：治理與管理 API

- [ ] T030 新增 `GET /api/memory/health`
- [ ] T031 新增 `POST /api/memory/search`（管理除錯）
- [ ] T032 新增 `POST /api/memory/users/{user_id}/forget`

## M5：測試與驗收

- [ ] T040 單元測試：scope 隔離、策略模式切換、Top-K 截斷
- [ ] T041 單元測試：provider 失敗時 fail-open 降級
- [ ] T042 整合測試：跨代理偏好延續（user_scope）
- [ ] T043 整合測試：代理專業記憶重用（agent_scope）
- [ ] T044 整合測試：forget user 後記憶不可再檢索
- [ ] T045 回歸測試：既有 chat/rag 路徑不退化
- [ ] T046 單元測試：`ROUTER_ASSIGNMENT_MODE` 四種模式行為符合預期
- [ ] T047 單元測試：`AGENT_MEMORY_ROUTING_MODE` 在 `memory_first/hybrid` 主決策下對結果有可觀測影響
