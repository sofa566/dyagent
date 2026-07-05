---
name: expense-request-html
description: 以 HTML 多步驟流程處理員工請款申請，示範與請假技能同一互動機制
version: 1.0.0
author: dyagent-example
---

# Expense Request HTML Skill

此技能示範 `scripts/main.js` 輸出 `mode=ui/final/error`，
由 `ui/index.html` 收集請款資料並透過 postMessage 回傳主頁橋接 API。

## 流程

1. `action=start` -> `mode=ui`（step-1）
2. `action=submit` step-1 -> `mode=ui`（step-2）
3. `action=submit` step-2 -> `mode=final`
4. `action=back` -> 回 step-1
5. `action=cancel` -> `mode=error`
