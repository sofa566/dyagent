# Implementation Plan: 腎友照護 POC

**Branch**: `009-renal-care` | **Date**: 2026-09-05 | **Spec**: `specs/009-renal-care/spec.md`
**Input**: Feature specification from `/specs/009-renal-care/spec.md`

## Summary

本變更以既有 dyagent 為平台底座，實作腎友照護 POC 的雙角色工作台與兩個核心技能閉環：

1. 護理師工作台（六分頁）與腎友/家屬工作台（個人視角）。
2. S01 日常監測、S02 透析療程兩個 Skill 的 API 與資料落地。
3. 串接既有 LINE 通道形成日常填報與人工接手閉環。
4. UI 視覺語言與參考站一致。

## Technical Context

**Language/Version**: Python 3.11（後端） / JavaScript ES2022（前端）
**Primary Dependencies**: FastAPI、SQLAlchemy、PyTest、Vite
**Storage**: PostgreSQL（主） / SQLite（測試）
**Testing**: PyTest + 前端 build 驗證
**Target Platform**: Linux
**Project Type**: Web（backend/frontend）
**Constraints**: 全文繁體中文、不得跨越醫療安全邊界
**Scope**: POC 四病患、兩 Skill、雙角色工作台、LINE 整合

## Constitution Check

- I. Code Quality：場景邏輯模組化，避免汙染既有通用聊天流程。
- II. Testing Standards：S01/S02、角色隔離、LINE 流程需有測試。
- III. User Experience Consistency：雙角色入口、六分頁與參考站一致。
- IV. Performance Requirements：POC 以穩定與可演示為主。
- V. Observability & Maintainability：異常、追蹤、結案皆可追溯。
- VI. Documentation in Traditional Chinese：規格與操作文件全繁中。

GATE 結論：通過。

## Project Structure

### Documentation (this feature)

```text
specs/009-renal-care/
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
│   │   ├── renal_monitoring.py
│   │   ├── renal_dialysis.py
│   │   └── renal_dashboard.py
│   ├── services/
│   │   ├── renal_monitoring_service.py
│   │   └── renal_dialysis_service.py
│   └── models/
│       └── renal_*.py (或集中於既有 models)
└── tests/
    ├── integration/
    └── unit/

frontend/
└── src/pages/
    ├── renal-care-nurse.html
    ├── renal-care-patient.html
    └── line-console.html (重用)
```

## Phase Plan

### Phase 1：資料模型與 API 契約

1. 定義 POC 實體與欄位（病患、監測、透析、追蹤）。
2. 規劃 S01/S02 API request/response 與錯誤格式。
3. 完成 migration 設計與索引策略。

### Phase 2：S01 日常監測閉環

1. 建立 `POST /api/monitoring-records`。
2. 加入欄位驗證、個別規則比對、追蹤判斷。
3. 回傳病患可讀提醒與護理端追蹤資訊。

### Phase 3：S02 透析療程閉環

1. 建立透析 Session 建立與更新 API。
2. 建立透析中事件寫入 API。
3. 建立洗後確認 API 與結案狀態轉移。

### Phase 4：雙角色工作台 UI

1. 護理師工作台：六分頁與摘要指標。
2. 腎友/家屬工作台：個人任務、趨勢、提醒。
3. 上方角色切換導覽與權限分流。

### Phase 5：LINE 流程整合

1. LINE 填報導入 S01。
2. 護理師人工接手與異常追蹤串接。
3. 驗證 bot/human 與照護流程一致。

### Phase 5.1：腎友免註冊網頁入口（LINE 身分）

1. 設計 LINE 身分映射流程（`line_user_id -> patient_profile`）。
2. 提供短時效簽章連結（magic link）進入腎友工作台。
3. 補齊連結重發、過期與撤銷策略。
4. 設計 fallback：若映射失敗，導向人工協助流程。

### Phase 6：驗收與演示

1. 對照流程圖演示 A/B 兩條主流程。
2. 完成十項驗收清單與回歸測試。
3. 產出 POC 示範腳本與限制說明。

## Complexity Tracking

| Risk/Complexity | Why Needed | Mitigation |
|---|---|---|
| 角色與權限隔離 | 護理師與腎友資料可視範圍不同 | 角色路由分離 + API 權限檢查 |
| 醫療邊界誤用 | AI 容易被誤解為醫囑 | 文案與流程明確標註人工確認 |
| 流程資料一致性 | S01/S02/LINE 來源多 | 事件模型統一時間戳與關聯鍵 |
| 展示站一致性 | UI 驗收主觀差異高 | 以六分頁與樣式語意清單逐項對照 |
