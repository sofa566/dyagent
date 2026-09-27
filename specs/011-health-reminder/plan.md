# Implementation Plan: 衛教內容提醒（MVP）

**Branch**: `011-health-reminder` | **Date**: 2026-09-16 | **Spec**: `specs/011-health-reminder/spec.md`
**Input**: Feature specification from `/specs/011-health-reminder/spec.md`

## Summary

本變更以最小增量建立衛教提醒治理層，重點為：

1. 新增衛教內容主檔與投遞紀錄。
2. 新增核准前禁止發送的硬限制。
3. 提供立即發送與排程發送 API。
4. 前端新增最小作業頁（列表、審核、發送、排程）。

## Technical Context

**Language/Version**: Python 3.11、JavaScript ES2022  
**Primary Dependencies**: FastAPI、SQLAlchemy、Celery、Redis、Vite  
**Storage**: PostgreSQL（主）/ SQLite（測試）/ Redis（排程）  
**Testing**: PyTest（整合測試）  
**Target Platform**: Linux  
**Project Type**: Backend + Frontend

## Constitution Check

- Code Quality：內容治理、發送、排程分層。
- Testing Standards：覆蓋核准閘門、立即發送、排程建立。
- UX Consistency：沿用既有後台權限與提示模式。
- Documentation：文件與欄位命名以繁中說明。

GATE 結論：通過。

## Planned Source Structure

```text
backend/
├── src/
│   ├── api/routes/
│   │   └── health_education.py
│   ├── services/
│   │   └── health_education_service.py
│   └── models/
│       └── renal_care.py（新增衛教內容/投遞紀錄）
└── alembic/versions/
    └── *_health_education_tables.py

frontend/
└── src/pages/
    └── health-reminder.html
```

## Phase Plan

### Phase 1：資料層

1. 新增 `health_education_contents`。
2. 新增 `health_education_delivery_logs`。
3. 補索引（status、source_url、content_id、created_at）。

### Phase 2：服務層與 API

1. 建立內容 CRUD（MVP: create/list）。
2. 實作 approve/reject。
3. 實作 send-now（LINE 推送 + delivery log）。
4. 實作 schedule（建立 `scheduled_tasks` 任務）。

### Phase 3：前端最小頁

1. 候選內容列表。
2. 核准/退回按鈕。
3. 立即發送與建立排程。
4. 單篇內容投遞紀錄查詢。

### Phase 4：測試與驗收

1. 未核准內容不可發送。
2. 核准內容可立即發送（mock push）。
3. schedule API 可建立任務。

## 驗收現況（2026-09-25）

1. `pytest backend/tests/integration/test_health_education_api.py`：通過（21 passed）。
2. `pytest backend/tests/integration/test_renal_care_api.py backend/tests/integration/test_health_education_api.py`：通過（34 passed）。
3. `ruff check backend/src backend/tests`：通過。
4. `ruff check .`：通過（含 `backend/alembic`）。

## 綁定判定規則同步

1. 衛教發送與腎友提醒使用同一判定：優先採用 `line_channel_sessions(status=active,binding_status=bound)`。
2. 無 active+bound session 時，才退回病患主檔，且需同時具備 `line_user_id + tel_no`。
3. 測試資料若要驗證主檔 fallback 發送成功，必須補齊 `tel_no`，否則預期為 `skipped`。
