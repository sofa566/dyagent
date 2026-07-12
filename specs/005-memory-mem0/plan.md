# Implementation Plan: 多代理雙層記憶（Mem0 + Redis）

**Branch**: `005-memory-mem0` | **Date**: 2026-07-12 | **Spec**: specs/005-memory-mem0/spec.md
**Input**: Feature specification from `/specs/005-memory-mem0/spec.md`

**Note**: 本文件依 `/speckit.plan` 風格產出，並對齊現有 dyagent 架構（FastAPI + SQLAlchemy + Redis + Qdrant）。

## Summary

導入「短期記憶 + 長期記憶」雙層架構：
- 短期記憶沿用並強化 Redis（會話最近訊息，TTL 管控）。
- 長期記憶採 Mem0（scope 隔離：user/agent/user-agent/global）。
- Master/Worker 路由流程在不破壞現有聊天可用性的前提下，新增記憶組裝與寫回機制。
- 設計為可切換模式（`off`/`mem0_oss`/`mem0_platform`/`mock`），並保留 fail-open 降級。
- 長期記憶寫入責任統一由 Master/Orchestrator gate 執行，Worker 僅提供候選修正資訊。

## Technical Context

**Language/Version**: Python 3.11（後端）  
**Primary Dependencies**: FastAPI、SQLAlchemy、Redis、Qdrant、Mem0（新增）  
**Storage**: PostgreSQL（結構化資料）、Redis（短期記憶）、Qdrant（長期記憶向量）  
**Testing**: PyTest（unit/integration），外部依賴以 mock 為主  
**Target Platform**: Linux 伺服器  
**Project Type**: Web 服務（backend/frontend）  
**Performance Goals**: 記憶檢索附加延遲 p95 < 250ms，記憶故障不影響聊天主流程  
**Constraints**: 全文件與介面說明使用繁體中文；不得破壞既有 `/api/chat` 行為相容性  
**Scale/Scope**: 單組織多使用者、多代理者協作聊天

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- I. Code Quality：以 `memory_service` 抽象隔離第三方 SDK，避免在路由層散落整合邏輯。
- II. Testing Standards：補齊 scope 隔離、降級流程、寫回策略的單元與整合測試。
- III. User Experience Consistency：記憶故障僅降級，不影響使用者聊天成功回覆。
- IV. Performance Requirements：限制 Top-K 與注入長度，避免對回應延遲造成明顯退化。
- V. Observability & Maintainability：新增記憶讀寫事件與錯誤分類，便於維運追蹤。
- VI. Documentation in Traditional Chinese：本文件與關聯規格皆為繁體中文。

GATE 結論：通過。

## Project Structure

### Documentation (this feature)

```text
specs/005-memory-mem0/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── apis.md
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
backend/
├── src/
│   ├── api/
│   │   └── routes/chat.py                 # 聊天主流程（上下文組裝/寫回）
│   ├── core/
│   │   └── config.py                      # 記憶模式與策略設定
│   ├── services/
│   │   ├── memory_service.py              # 新增：記憶抽象門面
│   │   ├── memory_providers/
│   │   │   ├── base.py                    # 新增：provider 介面
│   │   │   ├── mem0_provider.py           # 新增：Mem0 OSS/Platform 實作
│   │   │   └── mock_provider.py           # 新增：測試/降級 provider
│   │   └── redis_service.py               # 既有，擴充短期記憶 helper
│   └── models/events.py                   # 可觀測事件擴充（沿用 EventPart）
├── requirements.txt                       # 新增 mem0ai 套件
└── tests/
    ├── unit/
    │   ├── test_memory_service.py
    │   └── test_memory_scope_isolation.py
    └── integration/
        └── test_chat_memory_integration.py
```

**Structure Decision**: 採「新增服務層抽象 + 最小入侵 chat 路由」策略，避免直接耦合 Mem0 SDK 到多處流程。

## Phase Plan

### Phase 0：研究與定案

1. 決定 provider 模式與預設值（`off` or `mem0_oss`）。
2. 定案 scope 對映：
   - user_scope: `user_id=<uid>, agent_id=global_preference`
   - agent_scope: `user_id=system_shared, agent_id=<agent_id>`
   - interaction_scope: `user_id=<uid>, agent_id=<agent_id>`
3. 定案寫回策略：偏好、工具修正經驗、一般對話分流規則。

完成標準：`research.md` 無未決策項。

### Phase 1：設定與抽象層

1. 在 `config.py` 新增記憶供應與策略開關。
2. 建立 `MemoryProvider` 介面與 `memory_service` 門面。
3. 實作 `mock_provider` 與 fail-open 回退。

完成標準：不接 Mem0 也可通過單元測試。

### Phase 2：Mem0 Provider 實作

1. 實作 Mem0 OSS 連線（`Memory.from_config`）。
2. 實作 Mem0 Platform 連線（`MemoryClient`/`AsyncMemoryClient`）。
3. 實作讀寫 API 對應：`search/add/delete_all`。

完成標準：可在測試環境以 mock 驗證介面行為；真實 provider 可啟動且不拋未處理例外。

### Phase 3：聊天流程接線

1. 在 `chat_stream` 組裝 `memory_context`，與 `history_context` 合併注入。
2. 在回覆完成後由 Master/Orchestrator gate 寫回長期記憶，並同步更新短期記憶。
3. 補上事件紀錄：`memory.retrieve`、`memory.write`（成功/失敗）。

完成標準：聊天主流程在記憶啟用與停用兩種模式均可正常回覆。

### Phase 4：合規與治理

1. 提供依 `user_id` 記憶清除能力（服務層接口 + API 端點）。
2. 加入敏感資訊最小遮罩（如 token、密碼模式）。
3. 補上操作日誌欄位與錯誤分類。

完成標準：隔離測試與清除測試通過。

### Phase 5：驗收與回歸

1. 單元測試：scope 隔離、策略切換、fail-open。
2. 整合測試：聊天注入記憶、寫回記憶、故障降級。
3. 回歸測試：既有 chat/rag/agent 路由不退化。

完成標準：新增測試全綠，既有關鍵測試不失敗。

## Complexity Tracking

| Risk/Complexity | Why Needed | Mitigation |
|---|---|---|
| 外部 SDK 可靠性 | Mem0 與外部向量/模型端點可能不穩定 | 以 provider 抽象 + fail-open + timeout 控制 |
| 記憶污染風險 | 多使用者多代理混用易造成跨租戶資料外洩 | 強制 scope 欄位與隔離測試 |
| Prompt 膨脹 | 長期記憶注入過多造成上下文超限 | Top-K + 字元/Token 截斷策略 |
