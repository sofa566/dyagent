# Tasks: LLM Token/Cost 四層治理

**Input**: `specs/007-llm-cost/spec.md`, `specs/007-llm-cost/plan.md`

## Phase 1 - 資料口徑與服務層

- [x] T001 定義四層統一口徑（input/output/total/cost/estimated）與時間窗規則
- [x] T002 新增成本聚合服務 `cost_usage_service`（公司/群組/個人/代理者）
- [x] T003 補齊 `llm_turns` 聚合查詢所需索引與查詢優化（如需 migration）

## Phase 2 - 後端 API

- [x] T004 新增 `GET /admin/cost/overview`（公司總覽）
- [x] T005 新增 `GET /admin/cost/breakdown`（`dimension=group|user|agent`）
- [x] T006 新增 `GET /me/cost/usage`（個人當月用量）
- [x] T007 API 權限接線（沿用 `dashboard.read`，預留 `cost.read/cost.manage`）

## Phase 3 - 前端可視化

- [x] T008 擴充 `dashboard.html` 顯示四層卡片與 Top 排行
- [x] T009 加入時間窗與維度切換互動
- [x] T010 補齊空資料、估算標記、錯誤提示 UI

## Phase 4 - 政策與告警

- [x] T011 新增政策讀寫 API（公司/群組/個人/代理者 token 與 cost 上限）
- [x] T012 新增 50%/80%/100% 告警事件與稽核紀錄
- [x] T013 實作 warn-only 模式（超限告警但不拒絕）

## Phase 5 - 硬限制

- [x] T014 在 LLM 呼叫前加四層配額檢查
- [x] T015 超限拒絕回應與原因碼標準化
- [x] T016 串接稽核資料，確保可追溯觸發層級

## Phase 6 - 測試與文件

- [x] T017 新增四層聚合服務單元測試（含跨日/月邊界）
- [x] T018 新增 API 測試（管理端與個人端）
- [x] T019 新增告警與硬限制整合測試
- [x] T020 更新 `操作手冊.md` 與治理流程文件
- [x] T021 執行 `pytest` 與 `ruff check .` 並修正問題
