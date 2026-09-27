# Implementation Plan: 系統共用提醒排程

**Branch**: `010-reminder-schedule` | **Date**: 2026-09-12 | **Spec**: `specs/010-reminder-schedule/spec.md`
**Input**: Feature specification from `/specs/010-reminder-schedule/spec.md`

## Summary

本變更將 `009-renal-care` 已落地的催報能力整理為平台層能力，重點為：

1. 將提醒資料模型定義為可共用 policy/job/log 三層。
2. 建立 Celery + Redis + RedBeat 的 Crontab 任務執行層，支援動態任務同步與執行。
3. 保留手動 dispatch API 供營運補發與測試驗收。
4. 先以 LINE 通道落地，保留擴充多通道介面。

## Technical Context

**Language/Version**: Python 3.11
**Primary Dependencies**: FastAPI、SQLAlchemy、Redis、Celery、RedBeat、PyTest
**Storage**: PostgreSQL（主）/ SQLite（測試）/ Redis（鎖與去重）
**Testing**: PyTest（整合測試）
**Target Platform**: Linux
**Project Type**: Backend + Worker service
**Constraints**: 全文繁中、醫療提醒需可追溯、不可造成重複騷擾
**Scope**: 提醒排程共用層，不含多通道上線

## Constitution Check

- I. Code Quality：排程、派送、資料檢核分層。
- II. Testing Standards：需覆蓋自動與手動 dispatch、去重與失敗容錯。
- III. User Experience Consistency：提醒文案與狀態碼一致。
- IV. Performance Requirements：批次失敗隔離，不阻斷整體派送。
- V. Observability & Maintainability：每次提醒有 job/log。
- VI. Documentation in Traditional Chinese：規格與契約繁中。

GATE 結論：通過。

## Project Structure

### Documentation (this feature)

```text
specs/010-reminder-schedule/
├── change.md
├── contracts/
│   └── api-contracts.md
├── spec.md
├── plan.md
└── tasks.md
```

### Planned Source Structure

```text
backend/
├── src/
│   ├── api/routes/
│   │   └── monitoring_compliance.py
│   ├── services/
│   │   ├── monitoring_reminder_service.py
│   │   ├── monitoring_backfill_service.py
│   │   └── scheduler_task_service.py
│   └── worker/
│       ├── celery_app.py
│       └── tasks.py
│   └── models/
│       └── renal_care.py（提醒 + 排程任務實體）
└── alembic/versions/
    ├── *_monitoring_reminder_tables.py
    └── *_scheduled_tasks_tables.py

frontend/
└── src/pages/
    └── reminder-schedule.html（含 Crontab 任務管理）
```

## Phase Plan

### Phase 1：共用資料模型收斂

1. 檢查 policy/job/log 欄位可滿足跨場景。
2. 補索引與狀態欄位約束，確保查詢與去重效率。

### Phase 2：派送服務標準化

1. 將 dispatch 輸入/輸出固定化。
2. 建立 force/non-force 行為定義。
3. 建立對象未綁定與通道失敗分支。

### Phase 3：Celery 排程治理

1. 建立 `scheduled_tasks` / `scheduled_task_runs`。
2. 建立 Celery Worker 執行器與 run log 回寫。
3. 以 RedBeat 持久化 periodic task，並在 CRUD 時同步 schedule entry。

### Phase 4：觀測與契約

1. 補齊 API 契約與錯誤碼。
2. 補齊任務執行查詢需求（狀態、錯誤摘要、最近執行）。

### Phase 4.1：後台作業 UI

1. 建立提醒管理頁（日期/時段篩選、合規明細、手動派送）。
2. 建立 policy 設定區（啟用狀態、模板、時段）。
3. 建立派送紀錄區（sent/skipped/failed 與錯誤摘要）。
4. 建立 Crontab 任務區（任務新增/啟停/立即執行/最近執行）。
5. 與現有腎友照護頁權限整合（`line.center` / `nursing.line` / `nursing.trends`）。

### Phase 5：測試與驗收

1. 去重、失敗隔離、手動補發、排程觸發測試。
2. 驗證與 `009-renal-care` 串接不回歸。

## Complexity Tracking

| Risk/Complexity | Why Needed | Mitigation |
|---|---|---|
| 多 worker 重複觸發 | Celery 多 worker 併發 | RedBeat 單一排程鍵 + run log 去重觀測 |
| 通道故障影響整批 | LINE API 可能瞬斷 | 單筆失敗隔離與 failed 狀態 |
| 場景耦合過高 | 先從腎友場景起步 | 以 task_type + payload 抽象，保留 API 任務擴充 |
