# 任務清單（實作順序）

## M1：資料層

- [x] T001 Alembic：`agents` 新增 `system_prompt`、`function_profile_id`、`is_router`
- [x] T002 Alembic：建立 `function_profiles`
- [x] T003 Alembic：建立 `rag_datasets`
- [x] T004 Alembic：建立 `llm_turns`
- [x] T005 更新 SQLAlchemy models 與 schema

## M2：後端核心能力

- [x] T010 PromptBuilder：導入 system prompt 回退順序
- [x] T011 Function profile resolver：profile -> 舊欄位回退
- [x] T012 RAG dataset scope 驗證（global/agent_private）
- [x] T013 寫入 `llm_turns`（usage/cost/status）

## M3：Functions API（admin 建立）

- [x] T020 `GET /api/functions`
- [x] T021 `POST /api/functions`（admin）
- [x] T022 `PUT /api/functions/{id}`（admin）
- [x] T023 `DELETE /api/functions/{id}`（admin）
- [x] T024 `GET /api/functions/selectable`

## M4：Agent 綁定與設定 API

- [x] T030 `GET/PUT /api/agents/{id}/prompt`
- [x] T031 `PUT /api/agents/{id}/function-profile`（agent_admin/admin）
- [x] T032 `POST /api/agents/{id}/rag/datasets`（建立私有資料集）
- [x] T033 `PUT /api/agents/{id}/rag/bindings`

## M5：前端

- [x] T040 導覽列新增 `Functions` 按鍵
- [x] T041 新增 `frontend/src/pages/functions.html`
- [x] T042 `agent-llm-config.html` 改為只可「從 Functions 清單加入」
- [x] T043 `agent-llm-config.html` 新增私有資料集建立區
- [x] T044 權限 UI：Functions 建立操作僅 admin 可見

## M6：Router-Worker

- [x] T050 建立 Router Agent seed（`is_router=true`）
- [x] T051 新增 `POST /api/chat` 統一入口
- [x] T052 Router 分派到 Worker（先單 worker）
- [x] T053 寫入 `route.decision` / `route.forward` / `route.fallback`

## M7：測試與驗收

- [x] T060 Contract tests：Functions / RAG datasets / Agent bindings / Router chat
- [x] T061 Integration：一般使用者不選代理者也可聊天成功
- [x] T062 Security：私有資料集不可被其他代理者綁定與檢索
- [x] T063 Regression：既有 MCP/Skills/Chat 不退化
