# Implementation Plan: User / Group / Role 動態權限管理

**Branch**: `006-users-roles-implement` | **Date**: 2026-07-31 | **Spec**: `specs/006-users-roles/spec.md`
**Input**: Feature specification from `/specs/006-users-roles/spec.md`

**Note**: 本文件依 `/speckit.plan` 風格產出，並對齊現有 dyagent 架構（FastAPI + SQLAlchemy + Vite）。

## Summary

本次以「相容舊 `user.role` 欄位」為前提，落地 B 模式動態權限：
- 新增 `User ↔ Role`、`Group ↔ Role`、`User ↔ Group` 多對多資料模型。
- 提供角色、群組、綁定管理 API。
- 提供登入者能力 API（roles / groups / permissions）。
- 權限判斷優先使用新模型；無資料時回退舊 `user.role`，確保既有流程不壞。

## Technical Context

**Language/Version**: Python 3.11（後端） / JavaScript ES2022（前端）  
**Primary Dependencies**: FastAPI、SQLAlchemy、PyTest  
**Storage**: PostgreSQL（正式）/ SQLite（測試）  
**Testing**: PyTest（unit 為主）  
**Target Platform**: Linux  
**Project Type**: Web 服務（backend/frontend）  
**Constraints**: 繁體中文；需維持既有 API 相容性  
**Scope**: 權限模型、能力 API、後端授權基礎改造、前端能力導向 UI 顯示

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- I. Code Quality：以獨立 `access_control_service` 彙整權限計算，避免散落邏輯。
- II. Testing Standards：新增聯集權限、綁定管理、能力查詢測試。
- III. User Experience Consistency：先保後端授權與能力 API，再銜接前端顯示控制。
- IV. Performance Requirements：權限查詢以單輪聚合查詢實作，避免 N+1。
- V. Observability & Maintainability：預留稽核欄位與事件掛點。
- VI. Documentation in Traditional Chinese：本文件與註解維持繁中。

GATE 結論：通過。

## Project Structure

### Documentation (this feature)

```text
specs/006-users-roles/
├── spec.md
├── plan.md
└── tasks.md
```

### Source Code (repository root)

```text
backend/
├── src/
│   ├── api/routes/
│   │   └── access_control.py         # 新增：角色/群組/綁定/能力 API
│   ├── middleware/
│   │   └── rbac.py                   # 擴充：可使用動態權限判斷
│   ├── models/
│   │   └── __init__.py               # 新增 access_* 實體與綁定表
│   └── services/
│       └── access_control_service.py # 新增：有效權限聯集計算
└── tests/unit/
    └── test_access_control.py        # 新增：權限聯集與能力 API 測試
```

## Phase Plan

### Phase 0：資料模型定案

1. 定義 `access_permissions`、`access_roles`、`access_groups`。
2. 定義三個多對多綁定表（user-role、group-role、user-group）。
3. 定義稽核紀錄表（最小欄位）。

### Phase 1：服務層與授權計算

1. 新增 `access_control_service`。
2. 實作有效權限聯集計算（直掛角色 + 群組角色）。
3. 保留舊 `user.role` 回退策略（FR-011）。

### Phase 2：API 端點

1. 角色 CRUD（建立/編輯/啟停/查詢）。
2. 群組 CRUD（建立/編輯/啟停/查詢）。
3. 綁定管理 API（user-role / group-role / user-group）。
4. 能力 API（`/api/me/capabilities`）。

### Phase 3：測試與驗收

1. 單元測試：聯集權限計算。
2. API 測試：能力回傳、綁定後權限更新。
3. 回歸測試：既有 `check_permission` 使用點不破壞。

### Phase 4：前端能力驅動 UI

1. 將導覽列顯示邏輯由 `user.role` 改為 `/api/me/capabilities`。
2. 新增「權限管理」頁面，提供 Role/Group 與三種綁定操作。
3. 在主要管理頁面（如 users/dashboard）加入 capability guard。
4. 補齊 UI 驗收腳本，支援用瀏覽器完成 B 模式驗證。

## Complexity Tracking

| Risk/Complexity | Why Needed | Mitigation |
|---|---|---|
| 舊新模型共存 | 既有流程仍依賴 `user.role` | 先做回退策略，逐步遷移 |
| 權限鍵一致性 | 權限鍵拼字錯誤會造成誤授權 | 統一由 permission table 管理 |
| 前後端不同步 | 前端可見功能與後端授權可能不一致 | 以 `/me/capabilities` 為唯一來源 |
