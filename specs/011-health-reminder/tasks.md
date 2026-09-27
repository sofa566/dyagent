# Tasks: 衛教內容提醒（MVP）

**Input**: `specs/011-health-reminder/spec.md`, `specs/011-health-reminder/plan.md`

## Phase 1 - 模型與遷移

- [x] T001 新增 `HealthEducationContent` 與 `HealthEducationDeliveryLog` 模型
- [x] T002 新增對應 migration 與索引

## Phase 2 - 後端服務與 API

- [x] T003 新增衛教內容服務（create/list/approve/reject）
- [x] T004 新增 send-now（核准閘門 + LINE 發送 + log）
- [x] T005 新增 schedule（建立 scheduler 任務）
- [x] T006 新增衛教內容 API 路由

## Phase 3 - 前端作業頁

- [x] T007 新增 `health-reminder.html` 最小操作頁
- [x] T008 串接 API（新增/審核/發送/排程/查 log）
- [x] T009 導覽與權限整合

## Phase 4 - 測試與驗收

- [x] T010 新增整合測試（核准閘門/立即發送/排程建立）
- [x] T011 執行 `pytest`、`ruff check`、`npm run build`（以本需求異動檔案為驗收範圍）

### T011 拆分（執行層）

- [x] T011-1 執行衛教提醒整合測試（`pytest backend/tests/integration/test_health_education_api.py`）
- [x] T011-2 執行前端建置（`cd frontend && npm run build`）
- [x] T011-3 執行程式碼風格檢查（本需求異動檔案）
  - 指令：`ruff check backend/src/services/health_education_service.py backend/src/api/routes/health_education.py backend/tests/integration/test_health_education_api.py`
  - 結果：通過

## 目前狀態補充（2026-09-25）

- `pytest backend/tests/integration/test_health_education_api.py`：通過（21 passed）
- `pytest backend/tests/integration/test_renal_care_api.py backend/tests/integration/test_health_education_api.py`：通過（34 passed）
- `ruff check backend/src backend/tests`：通過
- `ruff check .`：通過（含 `backend/alembic`）
- `npm run build`：可於 `frontend/` 執行並通過（`start-dev.sh` 亦使用此指令）

## 規則與測試資料同步備註

- 衛教發送綁定判定已與腎友提醒一致：優先使用 `line_channel_sessions(status=active,binding_status=bound)`，其次才使用病患主檔，且主檔 fallback 需同時具備 `line_user_id + tel_no`。
- 測試若要驗證「主檔 fallback 可發送」，需在病患資料補齊 `tel_no`；否則預期為 `skipped`。
- 既有測試 `test_health_education_scheduler_rotate_mode_emits_empty_pool_alert` 已補上 `tel_no` 與 warmup `sent == 1` 斷言，避免規則變更後出現靜默退化。
