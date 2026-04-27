---
name: leave-request-html
description: 以 HTML 多步驟流程處理員工請假申請，示範 dyagent UI 互動技能
version: 1.0.0
author: dyagent-example
---

# Leave Request HTML Skill

此技能示範如何使用 `scripts/main.js` 回傳 `mode=ui/final/error`，
並由 `ui/index.html` 透過 postMessage 與 dyagent chat 主頁互動。

## 輸入欄位（form_data）

- `employeeName`: 員工姓名
- `employeeId`: 員工編號
- `leaveType`: 假別（annual/sick/personal）
- `startDate`: 開始日期（YYYY-MM-DD）
- `endDate`: 結束日期（YYYY-MM-DD）
- `reason`: 請假原因

## 流程

1. `action=start` -> 回傳 `mode=ui`（step-1）
2. `action=submit` step-1 -> 回傳 `mode=ui`（step-2）
3. `action=submit` step-2 -> 回傳 `mode=final`
4. `action=back` -> 回到 step-1
5. `action=cancel` -> 回傳 `mode=error`
