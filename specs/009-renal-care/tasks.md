# Tasks: 腎友照護 POC

**Input**: `specs/009-renal-care/spec.md`, `specs/009-renal-care/plan.md`

## Phase 1 - 資料模型與 API 契約

- [x] T001 定義病患、監測、透析、追蹤的資料模型與欄位
- [x] T002 設計並建立 migration（含索引與關聯）
- [x] T003 定義 S01/S02 API 契約與錯誤回應格式

## Phase 2 - S01 日常監測

- [x] T004 實作 `POST /api/monitoring-records`
- [x] T005 實作輸入驗證與確認旗標檢查
- [x] T006 實作個別規則比對與追蹤判斷
- [x] T007 實作病患可讀提醒與護理端追蹤輸出

## Phase 3 - S02 透析療程

- [x] T008 實作 `POST /api/dialysis-sessions`（建立/更新）
- [x] T009 實作 `POST /api/dialysis-events`（透析中事件）
- [x] T010 實作 `POST /api/dialysis-post-check`（洗後確認）
- [x] T011 實作異常事件與人工處理紀錄欄位

## Phase 4 - 雙角色 UI（對齊參考站）

- [x] T012 新增護理師工作台頁面（六分頁）
- [x] T013 新增腎友/家屬工作台頁面（個人視角）
- [x] T014 實作上方角色切換導覽（護理師/腎友家屬）
- [x] T015 實作四人總覽與摘要指標卡
- [x] T016 實作今日透析、個案趨勢、異常追蹤頁內容

## Phase 5 - LINE 與照護流程整合

- [ ] T017 將 LINE 填報資料導入 S01
- [ ] T017-1 新增「腎友陪伴」代理與 LINE session 強制指派策略（不經主代理分派）
- [ ] T017-2 針對既有 LINE session 補齊 agent 修正機制，避免誤路由到一般聊天代理
- [ ] T018 將護理師人工接手與追蹤案件串接
- [ ] T018-1 建立每日兩次回報完整性檢核（`MORNING` / `EVENING`）
- [ ] T018-2 實作糖尿病腎友血糖必填驗證（`is_diabetic=true`）
- [ ] T018-3 實作缺報/漏填催報訊息（LINE push）與送達紀錄
- [ ] T019 驗證 bot/human 模式下流程一致性
- [ ] T019-1 驗證 LINE 腎友對話在 bot/human 切換時均維持「腎友陪伴」處理上下文

## Phase 5.1 - 腎友免註冊入口（LINE 身分）

- [x] T019A 設計 `line_user_id` 與病患主檔映射規則
- [ ] T019B 設計 magic link token 格式（短時效、可撤銷）
- [ ] T019C 設計入口 API（發送連結、驗證連結、失效回應）
- [ ] T019D 設計異常流程（連結過期、重發、人工客服介入）

## Phase 6 - 測試與驗收

- [x] T020 新增 S01/S02 API 單元與整合測試
- [ ] T021 新增角色隔離與權限測試
- [ ] T021-1 新增 LINE 腎友誤路由防呆測試（不得回退主代理）
- [ ] T022 新增 UI 功能驗收清單（六分頁 + 雙角色）
- [x] T023 執行 `pytest -q` 與 `npm run build`
- [ ] T024 對照流程圖完成 A/B 演示腳本驗收
- [ ] T025 新增催報排程整合測試（每日兩次與糖友血糖缺漏情境）

## 第 1 週可交付清單（文件拆解）

- [x] W001 完成 S01/S02 API 契約文件（request/response/error）
- [x] W002 完成雙角色頁面線框（護理師六分頁 + 腎友個人頁）
- [x] W003 完成 LINE 免註冊入口設計稿（映射、token、失效策略）
- [x] W004 完成 POC 驗收腳本草案（A 流程、B 流程、人工接手）
- [ ] W005 完成腎友 LINE 日常回報 SOP（早晚回報、糖友加填、缺報補報）

## 與 010 依賴關係

- [x] D001 已將「提醒排程通用能力」轉由 `010-reminder-schedule` 定義與治理
- [ ] D002 `009-renal-care` 保留腎友業務規則與驗收，不重複定義 scheduler 通用規格
