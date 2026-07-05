# Tasks: 動態代理者創建系統

**Input**: Design documents from `/specs/001-dynamic-agents/`
**Prerequisites**: plan.md, spec.md, data-model.md, contracts/

**Tests**: This project follows TDD approach as per Constitution principles.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- Backend: `backend/src/`, `backend/tests/`
- Frontend: `frontend/src/`, `frontend/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [x] T001 Create backend project structure in backend/
- [x] T002 Create frontend project structure in frontend/
- [x] T003 Initialize Python 3.11+ project with FastAPI dependencies in backend/
- [x] T004 Initialize Vite project in frontend/
- [x] T005 [P] Configure pytest for backend testing
- [x] T006 [P] Configure Vitest for frontend testing
- [x] T007 Configure ruff and black for Python code formatting
- [x] T008 Configure ESLint and Prettier for JavaScript

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T009 [P] Setup PostgreSQL database schema and SQLAlchemy models in backend/src/models/
- [x] T010 [P] Setup Redis connection for session storage in backend/src/services/redis_service.py
- [x] T011 [P] Setup Qdrant client for vector storage in backend/src/services/qdrant_service.py
- [x] Implement JWT authentication in backend/src/middleware/auth.py
- [x] Implement role-based access control (RBAC) in backend/src/middleware/rbac.py
- [x] Setup FastAPI routing structure in backend/src/api/
- [x] Create base API error handlers in backend/src/api/errors.py
- [x] Setup structured logging in backend/src/core/logging.py
- [x] Create database migrations in backend/alembic/
- [x] Configure environment variables in backend/.env.example
- [x] Create frontend base layout and routing in frontend/src/main.js

**Checkpoint**: Foundation ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 1 - 創建代理者 (Priority: P1) 🎯 MVP

**Goal**: 使用者能夠建立具有名稱、功能描述的代理者，並配置模型、技能、工具和 RAG

**Independent Test**: 建立新代理者並驗證其名稱、描述、配置正確儲存

### Tests for User Story 1

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [x] T020 [P] [US1] Contract test for POST /api/agents in backend/tests/contract/test_create_agent.py
- [x] T021 [P] [US1] Unit test for AgentService.create_agent in backend/tests/unit/test_agent_service.py

### Implementation for User Story 1

- [x] T022 [P] [US1] Create Agent model in backend/src/models/agent.py
- [x] T023 [P] [US1] Create Workspace model in backend/src/models/workspace.py
- [x] T024 [US1] Implement AgentService in backend/src/services/agent_service.py
- [x] T025 [US1] Implement POST /api/agents endpoint in backend/src/api/routes/agents.py
- [x] T026 [US1] Implement GET /api/agents endpoint in backend/src/api/routes/agents.py
- [x] T027 [US1] Add validation schemas in backend/src/schemas/agent.py
- [x] T028 [US1] Create Agent creation UI in frontend/src/pages/create-agent.html
- [x] T029 [US1] Create Agent list UI in frontend/src/pages/agent-list.html

**Checkpoint**: User Story 1 should be fully functional and testable independently

---

## Phase 4: User Story 2 - 與代理者聊天 (Priority: P1)

**Goal**: 使用者能夠與代理者進行對話，代理者具有記憶能力

**Independent Test**: 選擇代理者並發送訊息，驗證收到回應且保持對話上下文

### Tests for User Story 2

- [x] T030 [P] [US2] Contract test for GET /api/agents/{id}/chat/stream in backend/tests/contract/test_chat.py
- [x] T031 [P] [US2] Integration test for conversation flow in backend/tests/integration/test_conversation.py

### Implementation for User Story 2

- [x] T032 [P] [US2] Create Conversation model in backend/src/models/conversation.py
- [x] T033 [P] [US2] Create Message model in backend/src/models/message.py
- [x] T034 [US2] Implement ChatService in backend/src/services/chat_service.py
- [x] T035 [US2] Implement LangChain agent wrapper in backend/src/agents/base.py
- [x] T036 [US2] Implement GET /api/agents/{id}/chat/stream endpoint in backend/src/api/routes/chat.py
- [x] T037 [US2] Implement GET /api/agents/{id}/conversations endpoint in backend/src/api/routes/chat.py
- [x] T038 [US2] Create chat UI page in frontend/src/pages/chat.html
- [x] T039 [US2] Implement WebSocket connection for real-time chat in frontend/src/services/websocket.js

**Checkpoint**: User Stories 1 AND 2 should both work independently

---

## Phase 5: User Story 3 - 管理代理者 (Priority: P2)

**Goal**: 使用者能夠修改和刪除代理者

**Independent Test**: 修改代理者名稱並驗證變更，刪除代理者並確認不再存在

### Implementation for User Story 3

- [x] T040 [P] [US3] Implement PUT /api/agents/{id} endpoint in backend/src/api/routes/agents.py
- [x] T041 [P] [US3] Implement DELETE /api/agents/{id} endpoint in backend/src/api/routes/agents.py
- [x] T042 [US3] Create Agent edit UI in frontend/src/pages/edit-agent.html
- [x] T043 [US3] Add delete confirmation modal in frontend/src/components/modal.js

---

## Phase 6: User Story 4 - 配置代理者模型與MCP (Priority: P2)

**Goal**: 使用者能夠配置地端/雲端模型和 MCP server

**Independent Test**: 配置不同類型模型和 MCP server，驗證能夠正常運作

### Implementation for User Story 4

- [x] T044 [P] [US4] Create ModelConfig service in backend/src/services/model_config_service.py
- [x] T045 [P] [US4] Create MCP client in backend/src/clients/mcp_client.py
- [x] T046 [US4] Implement POST /api/mcp/connect endpoint in backend/src/api/routes/mcp.py
- [x] T047 [US4] Implement GET /api/mcp/servers endpoint in backend/src/api/routes/mcp.py
- [x] T048 [US4] Implement GET /api/mcp/tools endpoint in backend/src/api/routes/mcp.py
- [x] T049 [US4] Create model configuration UI in frontend/src/pages/model-config.html
- [x] T050 [US4] Create MCP server connection UI in frontend/src/pages/mcp-config.html

---

## Phase 7: User Story 5 - 工作區隔離 (Priority: P3)

**Goal**: 每個代理者在獨立工作區運作，確保資料隔離

**Independent Test**: 建立多個代理者並分別對話，驗證對話歷史各自獨立

### Implementation for User Story 5

- [x] T051 [P] [US5] Implement workspace isolation in AgentService
- [x] T052 [P] [US5] Add workspace_id foreign key constraints in backend/src/models/
- [x] T053 [US5] Implement RAG workspace isolation in backend/src/services/rag_service.py

---

## Phase 8: User Story 6 - 使用者權限管理 (Priority: P1)

**Goal**: 區分管理者、Agent 管理者，一般使用者權限

**Independent Test**: 建立不同角色使用者並驗證其權限

### Tests for User Story 6

- [x] T054 [P] [US6] Contract test for auth endpoints in backend/tests/contract/test_auth.py
- [x] T055 [P] [US6] Unit test for RBAC in backend/tests/unit/test_rbac.py

### Implementation for User Story 6

- [x] T056 [P] [US6] Create User model in backend/src/models/user.py
- [x] T057 [P] [US6] Create Role model in backend/src/models/role.py
- [x] T058 [US6] Implement UserService in backend/src/services/user_service.py
- [x] T059 [US6] Implement POST /api/auth/register endpoint in backend/src/api/routes/auth.py
- [x] T060 [US6] Implement POST /api/auth/login endpoint in backend/src/api/routes/auth.py
- [x] T061 [US6] Implement GET /api/users endpoint in backend/src/api/routes/users.py
- [x] T062 [US6] Implement PUT /api/users/{id}/role endpoint in backend/src/api/routes/users.py
- [x] T063 [US6] Create login UI in frontend/src/pages/login.html
- [x] T064 [US6] Create user management UI in frontend/src/pages/admin/users.html

---

## Phase 9: User Story 7 - 系統日誌 (Priority: P2)

**Goal**: 記錄所有操作日誌，管理者可查看和過濾

**Independent Test**: 觸發操作並查看日誌記錄

### Tests for User Story 7

- [x] T065 [P] [US7] Unit test for logging middleware in backend/tests/unit/test_logging.py

### Implementation for User Story 7

- [x] T066 [P] [US7] Create Log model in backend/src/models/log.py
- [x] T067 [P] [US7] Implement logging middleware in backend/src/middleware/logging.py
- [x] T068 [US7] Implement LogService in backend/src/services/log_service.py
- [x] T069 [US7] Implement GET /api/logs endpoint in backend/src/api/routes/logs.py
- [x] T070 [US7] Create logs viewer UI in frontend/src/pages/admin/logs.html

---

## Phase 10: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories

- [x] T071 [P] Integrate docling-serve for document upload in backend/src/services/document_service.py
- [x] T072 [P] Implement RAG document upload UI in frontend/src/pages/rag-upload.html
- [x] T073 [P] Add skills configuration in backend/src/models/skill.py
- [x] T074 [P] Add tools configuration in backend/src/models/tool.py
- [x] T075 [P] Create skills/tools management UI in frontend/src/pages/skills-tools.html
- [x] T076 Create system dashboard in frontend/src/pages/dashboard.html
- [ ] T077 Performance optimization - add caching layer
- [ ] T078 Security hardening - add rate limiting
- [x] T079 Update API documentation in backend/src/api/docs.py
- [ ] T080 Run quickstart.md validation

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **User Stories (Phase 3-9)**: All depend on Foundational phase completion
  - User stories can then proceed in parallel (if staffed)
  - Or sequentially in priority order (P1 → P2 → P3)
- **Polish (Phase 10)**: Depends on all user stories being complete

### User Story Dependencies

- **US1 創建代理者 (P1)**: Can start after Foundational - Core feature
- **US2 與代理者聊天 (P1)**: Can start after Foundational - Core feature
- **US3 管理代理者 (P2)**: Depends on US1 - Need agents to manage
- **US4 配置MCP (P2)**: Depends on US1 - Need agents to configure
- **US5 工作區隔離 (P3)**: Depends on US1, US2 - Data isolation
- **US6 權限管理 (P1)**: Foundational - Required for security
- **US7 系統日誌 (P2)**: Foundational - Required for debugging

### Within Each User Story

- Tests MUST be written and FAIL before implementation
- Models before services
- Services before endpoints
- Core implementation before integration
- Story complete before moving to next priority

### Parallel Opportunities

- All Setup tasks marked [P] can run in parallel
- All Foundational tasks marked [P] can run in parallel
- Once Foundational phase completes:
  - US1 and US2 can run in parallel (both P1 core features)
  - US6 can start (needed for auth before other stories)
  - US7 can start (logging is cross-cutting)
- US3, US4, US5 depend on US1 completion

---

## Parallel Example: User Story 1 + 2

```bash
# Launch core features in parallel:
Task: "T020 Contract test for POST /api/agents"
Task: "T030 Contract test for GET /api/agents/{id}/chat/stream"
Task: "T056 Create User model"

# Launch models for User Story 1 and 2 in parallel:
Task: "T022 Create Agent model"
Task: "T023 Create Workspace model"
Task: "T032 Create Conversation model"
Task: "T033 Create Message model"
```

---

## Implementation Strategy

### MVP First (US1 + US2 + US6)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational
3. Complete US1: 創建代理者
4. Complete US2: 與代理者聊天
5. Complete US6: 使用者權限管理
6. **STOP and VALIDATE**: Core functionality works
7. Deploy/demo if ready

### Incremental Delivery

1. Setup + Foundational → Foundation ready
2. US1 + US2 → Basic chat functionality works
3. US6 → Authentication ready
4. US3, US4 → Agent management ready
5. US7 → Logging ready
6. US5 → Workspace isolation
7. Polish → Full feature set

### Parallel Team Strategy

With multiple developers:

1. Team completes Setup + Foundational together
2. Once Foundational is done:
   - Developer A: US1 (創建代理者)
   - Developer B: US2 (聊天) + US6 (權限)
   - Developer C: US3, US4 (管理與配置)
3. Stories complete and integrate independently

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Each user story should be independently completable and testable
- Verify tests fail before implementing
- Commit after each task or logical group
- Stop at any checkpoint to validate story independently
- Constitution requires TDD - all new features should have tests first
