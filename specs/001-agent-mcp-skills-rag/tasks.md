# Tasks: Per-Agent MCP/Skills/RAG Config

**Input**: Design documents from `/specs/001-agent-mcp-skills-rag/`
**Prerequisites**: plan.md（required）, spec.md（required）, research.md, data-model.md, contracts/

**Tests**: 本功能規格明確要求測試（API 與聊天行為），本清單包含測試任務。

**Organization**: 依使用者故事（US1/US2/US3）分組，確保可獨立實作與驗證。

## Format: `[ID] [P?] [Story] Description`

- **[P]**: 可平行執行（不同檔案、無直接依賴）
- **[Story]**: 任務所屬使用者故事（US1/US2/US3）
- 描述內含具體檔案路徑

## Phase 1: Setup（Shared Infrastructure）

**Purpose**: 建立動態 Skill 路由與規格落地的基礎骨架

- [x] T001 建立動態 Skill 索引策略文件（以 `SKILL.md` 為唯一真相）於 `specs/001-agent-mcp-skills-rag/research.md`
- [x] T002 在 `backend/src/services/skill_executor.py` 整理執行入口（class 化）與 `execute_scripts=False` 安全流程
- [x] T003 [P] 新增路由配置常數（關鍵詞/優先級）於 `backend/src/core/config.py`

---

## Phase 2: Foundational（Blocking Prerequisites）

**Purpose**: 完成不依賴預轉 JSON 的動態 parse + 路由基礎能力

**⚠️ CRITICAL**: 此階段完成前，不開始 UI 與聊天完整整合

- [x] T004 建立 Skill 掃描器：自動載入技能目錄下所有 `SKILL.md`（含遞迴）於 `backend/src/services/skill_registry.py`
- [x] T005 [P] 建立最小 parser：從 `SKILL.md` 擷取 `name/description/triggers/routes/constraints` 於 `backend/src/services/skill_registry.py`
- [x] T006 [P] 建立記憶體快取失效策略（mtime/hash）於 `backend/src/services/skill_registry.py`
- [x] T007 建立 rule-based router（不硬編碼單一技能）於 `backend/src/services/chat_router.py`
- [x] T008 [P] 新增結構化路由日誌欄位（intent/matched_rules/selected_skill/fallback_reason）於 `backend/src/services/chat_router.py`
- [x] T009 建立路由測試資料集（至少 20 筆）於 `backend/tests/fixtures/skill_router_cases.json`

**Checkpoint**: 具備「新增 SKILL.md 無需改 router 程式碼」能力

---

## Phase 3: User Story 1 - 代理者 LLM 設定可獨立配置（Priority: P1）🎯 MVP

**Goal**: 管理者可在單一代理者設定頁配置 MCP/Skills/RAG 並可重載驗證

**Independent Test**: 進入設定頁完成設定、測試、儲存、重載後內容一致

### Tests for User Story 1

- [x] T010 [P] [US1] API 合約測試：代理者 LLM 設定讀寫於 `backend/tests/contract/test_agent_llm_config_api.py`
- [x] T011 [P] [US1] 整合測試：MCP 測試連線與 RAG 測試檢索於 `backend/tests/integration/test_agent_llm_config_flow.py`

### Implementation for User Story 1

- [x] T012 [US1] 擴充代理者設定 API（MCP/Skills/RAG）於 `backend/src/api/routes/agents.py`
- [x] T013 [P] [US1] 擴充前端 LLM 設定頁表單與儲存流程於 `frontend/src/pages/agent-llm-config.html`
- [x] T014 [P] [US1] 新增前端 API 封裝（讀寫、測試連線、測試檢索）於 `frontend/src/services/api.js`
- [x] T015 [US1] 補上唯讀與可編輯權限狀態切換於 `frontend/src/pages/agent-llm-config.html`

**Checkpoint**: US1 可獨立完成並驗證

---

## Phase 4: User Story 2 - 聊天套用代理者能力（Priority: P2）

**Goal**: 聊天僅使用該代理者已啟用的 MCP/Skills/RAG

**Independent Test**: A 代理者（啟用）與 B 代理者（未啟用）對同提問行為可區分

### Tests for User Story 2

- [x] T016 [P] [US2] 聊天整合測試：MCP/Skills 啟停是否影響呼叫於 `backend/tests/integration/test_chat_agent_capability_gating.py`
- [x] T017 [P] [US2] 聊天整合測試：RAG 啟停與來源一致性於 `backend/tests/integration/test_chat_agent_rag_routing.py`

### Implementation for User Story 2

- [x] T018 [US2] 將動態 skill router 接入聊天流程於 `backend/src/services/chat_router.py`
- [x] T019 [US2] 補上失敗降級路徑（MCP/RAG 不可用不中斷對話）於 `backend/src/services/chat_router.py`
- [x] T020 [US2] 調整 `execute_skill()` 在 `execute_scripts=False` 或無腳本時仍可穩定回傳 prompt 於 `backend/src/services/skill_executor.py`

**Checkpoint**: US1 + US2 均可獨立運作

---

## Phase 5: User Story 3 - 權限與審計（Priority: P3）

**Goal**: user 唯讀、admin 可查審計，完整可追蹤

**Independent Test**: 無更新權限僅可檢視；admin 可見變更紀錄

### Tests for User Story 3

- [x] T021 [P] [US3] 權限測試：read/update 差異行為於 `backend/tests/integration/test_agent_llm_config_permissions.py`
- [x] T022 [P] [US3] 審計測試：設定變更完整記錄於 `backend/tests/integration/test_agent_llm_audit_log.py`

### Implementation for User Story 3

- [x] T023 [US3] 擴充審計記錄欄位與事件內容於 `backend/src/api/routes/agents.py`
- [x] T024 [US3] 在管理頁提供變更摘要檢視於 `frontend/src/pages/agent-llm-config.html`

**Checkpoint**: US1/US2/US3 均可獨立驗證

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: 完善跨故事品質與回歸

- [x] T025 [P] 新增 router 回歸測試（新技能加入不改程式碼）於 `backend/tests/integration/test_skill_router_dynamic_discovery.py`
- [x] T026 [P] 補充操作文件與快啟說明於 `specs/001-agent-mcp-skills-rag/quickstart.md`
- [x] T027 執行並修正 `pytest`、`ruff check .` 於專案根目錄

  - 已執行：`pytest`（完整）與 `ruff check .`（完整）；並修正本變更範圍內新增/影響測試。
  - 目前仍有既有歷史失敗與 lint debt（非本次 001 變更引入），已於收尾說明中保留清單。

---

## Phase 7: Chat 附件二進位上傳與文件轉換（A/B/C/D）

**Purpose**: 建立與技能解耦的附件處理管線（上傳→快取→轉換→注入），避免只靠前端文字拼接。

### Tests for Phase 7

- [x] T028 [P] [US2] API 合約測試：聊天附件二進位上傳/讀取 metadata 於 `backend/tests/contract/test_chat_attachments_api.py`
- [x] T029 [P] [US2] 單元測試：`document_convert.py` 對 docx/xlsx/pptx/pdf 轉 markdown/json 於 `backend/tests/unit/test_document_convert.py`
- [x] T030 [P] [US2] 整合測試：多附件上傳後於工具執行階段可分檔注入 markdown 於 `backend/tests/integration/test_chat_attachment_markdown_injection.py`

### Implementation for Phase 7

- [x] T031 [US2] 新增附件 metadata 資料表（例如 `chat_attachments`）與 migration，欄位至少含 `id/conversation_id/user_id/filename/ext/mime_type/file_path/size_bytes/status/error/created_at/expires_at` 於 `backend/src/models/__init__.py` 與 migration 檔
- [x] T032 [US2] 新增聊天附件 API（二進位上傳、回傳 attachment_id、查詢 metadata）於 `backend/src/api/routes/chat.py`
- [x] T033 [US2] 新增附件快取落地流程：所有格式皆可上傳，統一寫入 cache 目錄（分檔儲存，不覆蓋）於 `backend/src/services/chat_attachment_service.py`
- [x] T034 [US2] 建立唯一文件轉換工具檔 `backend/src/tools/document_convert.py`，使用 docling `DocumentConverter` 進行轉換，提供 markdown/json 輸出介面
- [x] T035 [US2] 將工具執行前置注入改為「通用附件注入機制」：依 `attachment_ids` 逐檔轉 markdown 後注入 payload，不可寫死針對特定技能（例如 humanizer）於 `backend/src/api/routes/chat.py` 與 `backend/src/services/chat_router.py`
- [x] T036 [US2] 前端聊天改為先上傳二進位附件再送出訊息（訊息僅攜帶 `attachment_ids` 與摘要，不再內嵌全文）於 `frontend/src/pages/chat.html`

**Implementation Notes（已確認）**

- metadata 先放 DB，需新增 table（不可只靠記憶體）。
- 所有格式均可上傳並寫入檔案；是否可轉 markdown 由後端轉換器決定。
- 多附件必須分開存檔、分開轉換、分開注入（不可合併成單一來源）。
- 不可在程式碼硬編碼特定技能名稱；附件轉換與注入必須是技能無關（tool-agnostic）。

---

## Dependencies & Execution Order

### Phase Dependencies

- Phase 1 → Phase 2（必要）
- Phase 2 完成後，US1/US2/US3 可按優先級推進
- Phase 7 建議在 US2（Phase 4）前段先完成 T031~T034，再做 T035~T036
- Polish 最後執行

### User Story Dependencies

- US1（P1）先完成，提供設定資料來源
- US2（P2）依賴 US1 的設定可用，但聊天驗證可獨立執行
- US3（P3）可與 US2 後段並行，但需接上最終資料結構

### Parallel Opportunities

- T003、T005、T006、T008、T010、T011、T013、T014、T016、T017、T021、T022、T025、T026、T028、T029、T030 可平行

---

## Implementation Strategy

### MVP First（先做穩定路由）

1. 完成 Phase 1 + Phase 2（動態 parse + rule router）
2. 完成 US1（設定頁）
3. 驗證「新增 SKILL.md 不改 router 程式」
4. 再推進 US2/US3
