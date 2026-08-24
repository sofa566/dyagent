# Tasks: User / Group / Role 動態權限管理

**Input**: `specs/006-users-roles/spec.md`, `specs/006-users-roles/plan.md`

## Phase 1 - 基礎資料模型

- [x] T001 新增 access permission/role/group 與三個綁定表（backend/src/models/__init__.py）
- [x] T002 新增最小稽核表（backend/src/models/__init__.py）

## Phase 2 - 服務層

- [x] T003 建立有效權限聯集計算服務（backend/src/services/access_control_service.py）
- [x] T004 新增舊 `user.role` 回退策略（backend/src/services/access_control_service.py）

## Phase 3 - API

- [x] T005 建立角色管理 API（backend/src/api/routes/access_control.py）
- [x] T006 建立群組管理 API（backend/src/api/routes/access_control.py）
- [x] T007 建立 user-role/group-role/user-group 綁定 API（backend/src/api/routes/access_control.py）
- [x] T008 建立 `/api/me/capabilities`（backend/src/api/routes/access_control.py）
- [x] T009 將新路由掛載至 API main（backend/src/api/main.py）

## Phase 4 - 授權接線

- [x] T010 擴充 `check_permission` 支援動態權限判斷（backend/src/middleware/rbac.py）
- [x] T011 在主要管理路由改用含 db 的權限檢查（backend/src/api/routes/users.py 等）

## Phase 5 - 測試

- [x] T012 新增聯集權限與能力 API 測試（backend/tests/unit/test_access_control.py）
- [x] T013 執行既有 users/chat 權限相關回歸測試

## Phase 6 - 前端能力驅動 UI

- [x] T014 導覽列改用 capabilities 控制可見功能（frontend/src/main.js）
- [x] T015 新增權限管理頁（Role/Group/Binding）與操作流程（frontend/src/pages/access-control.html）
- [x] T016 主要管理頁導入 capability guard（frontend/src/pages/users.html、frontend/src/pages/dashboard.html）
- [x] T017 完成 UI 驗收步驟與手動測試腳本（specs/006-users-roles/spec.md）
