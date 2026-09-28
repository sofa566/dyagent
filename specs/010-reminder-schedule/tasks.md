# Tasks: 系統共用提醒排程

**Input**: `specs/010-reminder-schedule/spec.md`, `specs/010-reminder-schedule/plan.md`

## Phase 1 - 模型與遷移

- [x] T001 建立提醒 policy/job/delivery log 資料模型
- [x] T002 建立 migration（含狀態 enum 與查詢索引）
- [ ] T003 檢查命名中性化（避免侷限腎友語意）

## Phase 2 - 派送服務

- [x] T004 實作提醒派送服務（dispatch）
- [x] T005 實作同日同時段去重（force=false）
- [x] T006 實作 skipped/failed/sent 分支與落地紀錄
- [ ] T007 補充通道適配介面（預留 Email/SMS）

## Phase 3 - 背景排程

- [x] T008 建立 `scheduled_tasks` / `scheduled_task_runs` 資料表與模型
- [x] T009 實作 scheduler 任務服務（CRUD、run log、手動執行）
- [x] T010 實作 Celery worker 執行層（run task callback + run log）
- [x] T010A 實作 Celery Beat + RedBeat 持久化排程同步
- [x] T010B 將 LINE 自動提醒改為預設 Crontab 任務（停用舊 dispatcher 啟動）

## Phase 4 - API 與契約

- [x] T011 提供每日完整性查詢 API
- [x] T012 提供手動派送 API（支援 force）
- [ ] T013 補齊派送統計查詢 API（依日期/視窗/狀態）

## Phase 4.1 - 後台 UI

- [ ] T013A 新增提醒管理頁（`frontend/src/pages/reminder-schedule.html`）
- [ ] T013B 新增合規列表區（日期/時段篩選 + 病患狀態表格）
- [ ] T013C 新增手動派送區（dispatch + force 開關 + 結果摘要）
- [ ] T013D 新增 policy 管理區（啟用/停用、模板編輯、時段設定）
- [ ] T013E 新增派送紀錄區（sent/skipped/failed 與錯誤摘要）
- [ ] T013F 串接導航與權限 gate（`line.center` / `nursing.line` / `nursing.trends`）
- [x] T013G 新增 Crontab 任務管理區（新增/啟停/立即執行/最近執行）

## Phase 5 - 測試與驗收

- [x] T014 新增整合測試：dispatch 成功與重複 dispatch 去重
- [x] T015 新增整合測試：LINE 腎友固定路由「腎友陪伴」
- [ ] T016 新增排程 tick 單元測試（時間命中與 lock 分支）
- [x] T017 執行 `pytest`（相關整合測試）與 `ruff check`
- [ ] T017A 新增 UI 驗收清單與手測腳本（非工程人員操作流程）
- [x] T017B 新增 scheduler task API 整合測試（CRUD + internal execute）

## 與 009 關聯

- [x] T018 在 `009-renal-care` 僅保留業務規則，排程通用能力改由 `010-reminder-schedule` 承接
