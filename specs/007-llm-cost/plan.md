# Implementation Plan: LLM Token/Cost 四層治理

**Branch**: `007-llm-cost` | **Date**: 2026-08-30 | **Spec**: `specs/007-llm-cost/spec.md`
**Input**: Feature specification from `/specs/007-llm-cost/spec.md`

## Summary

本變更以 `llm_turns` 為唯一資料來源，實作公司/群組/個人/代理者四層 token 與成本治理：

1. 建立四層聚合查詢 API 與個人查詢 API。
2. 儀表板新增四層成本可視化與 Top 排行。
3. 加入四層預算政策與告警（先 warn-only）。
4. 預留 hard-limit 開關，成熟後再啟用拒絕機制。

## Technical Context

**Language/Version**: Python 3.11（後端） / JavaScript ES2022（前端）
**Primary Dependencies**: FastAPI、SQLAlchemy、PyTest、Vite
**Storage**: PostgreSQL（主） / SQLite（測試）
**Testing**: PyTest、Ruff
**Target Platform**: Linux
**Project Type**: Web 服務（backend/frontend）
**Constraints**: 繁體中文、與既有 dashboard 權限邏輯相容
**Scope**: 聚合查詢、成本政策、告警事件、儀表板呈現

## Constitution Check

- I. Code Quality：聚合邏輯集中於成本服務層，避免散落在路由。
- II. Testing Standards：四層聚合、邊界時窗、告警與拒絕行為皆需測試。
- III. User Experience Consistency：先做可見性，再做硬限制。
- IV. Performance Requirements：聚合查詢需避免 N+1，優先 SQL 聚合。
- V. Observability & Maintainability：告警與拒絕寫入稽核，可追溯。
- VI. Documentation in Traditional Chinese：文件、欄位說明、操作手冊皆繁中。

GATE 結論：通過。

## Project Structure

### Documentation (this feature)

```text
specs/007-llm-cost/
├── change.md
├── spec.md
├── plan.md
└── tasks.md
```

### Source Code (repository root)

```text
backend/
├── src/
│   ├── api/routes/
│   │   ├── admin_dashboard.py       # 擴充四層成本總覽/明細
│   │   └── cost_usage.py            # 新增：成本查詢與政策 API（預計）
│   ├── services/
│   │   └── cost_usage_service.py    # 新增：四層聚合與門檻判斷
│   └── models/
│       └── __init__.py              # 視需要新增政策/告警實體
└── tests/unit/
    ├── test_cost_usage_service.py   # 新增：聚合與告警測試
    └── test_cost_usage_api.py       # 新增：API 測試

frontend/
└── src/pages/
    └── dashboard.html               # 擴充四層 token/cost 視覺區塊
```

## Phase Plan

### Phase 0：資料口徑凍結

1. 定義 token 與 cost 欄位口徑（input/output/total/cost/estimated）。
2. 定義切窗規則（日/月、時區）。
3. 定義多群組歸屬規則（先以預設策略實作，後續可配置）。

### Phase 1：四層聚合查詢

1. 建立公司總覽聚合查詢。
2. 建立群組/個人/代理者 breakdown 聚合查詢。
3. 建立個人端 `/me` 成本查詢。

### Phase 2：儀表板可視化

1. 新增四層統計卡片與趨勢區塊。
2. 新增 Top 群組 / Top 使用者 / Top 代理者表格。
3. 支援時間窗切換與空資料提示。

### Phase 3：政策與告警

1. 新增政策讀寫 API（四層 token/cost 上限）。
2. 新增 50%/80%/100% 告警判斷與事件寫入。
3. 先落地 warn-only，不阻擋流量。

### Phase 4：硬限制（可開關）

1. 在 LLM 呼叫前檢查四層用量是否超限。
2. 任一層超限則拒絕，回傳標準原因碼。
3. 完成稽核紀錄與錯誤提示對齊。

### Phase 5：驗證與文件

1. 補齊單元/整合測試。
2. 執行 `pytest` 與 `ruff check`。
3. 更新 `操作手冊.md`（口徑、告警、排障、回滾）。

## Complexity Tracking

| Risk/Complexity | Why Needed | Mitigation |
|---|---|---|
| 四層聚合查詢成本 | 維度變多易造成查詢負擔 | 以 SQL 聚合與索引優化，限制查詢窗口 |
| 供應商 usage 不一致 | 部分模型不回完整 token | 統一 normalize 流程並標記 estimated |
| 多群組歸屬對帳爭議 | 群組合計可能不等於公司總量 | 固定規則、文件明確揭露、API 回傳口徑欄位 |
| 早期就硬阻擋風險 | 可能誤擋正常請求 | 先 warn-only，觀察後再開 hard-limit |
