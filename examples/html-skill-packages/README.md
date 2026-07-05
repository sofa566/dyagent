# HTML 技能 ZIP 範例

此目錄提供可直接打包的 HTML 技能範例，對應 `specs/html-skill.md` 的互動協定。

## 範例清單

- `leave-request-html-skill/`：員工請假（2 步驟）
- `expense-request-html-skill/`：員工請款（2 步驟）

## 打包方式

```bash
cd examples/html-skill-packages
zip -r leave-request-html-skill.zip leave-request-html-skill
zip -r expense-request-html-skill.zip expense-request-html-skill
```

## 在 dyagent 技能管理的建議設定

- 技能名稱：`leave-request`
- `zip_bundle`：上傳 `leave-request-html-skill.zip`
- `skill_type`：`hybrid` 或 `prompt`（需啟用 ZIP scripts）
- `input_schema`：

```json
{
  "type": "object",
  "properties": {
    "employeeName": { "type": "string" },
    "employeeId": { "type": "string" },
    "leaveType": { "type": "string" },
    "startDate": { "type": "string" },
    "endDate": { "type": "string" },
    "reason": { "type": "string" }
  },
  "additionalProperties": true
}
```

## 驗證流程

1. 使用者輸入「我想請假」。
2. 代理呼叫 `leave-request`，技能回 `mode=ui`。
3. 前端開啟 iframe，使用者填寫並送出。
4. `step-1 submit` 回 `mode=ui(step-2)`。
5. `step-2 submit` 回 `mode=final`，聊天區顯示完成訊息。

## 端到端操作清單（建議）

1. 在技能管理建立 `leave-request` 與 `expense-request`，上傳對應 ZIP。
2. 將技能掛到可聊天代理者（agent integrations）。
3. 在 chat 輸入「我想請假」或「我要請款」。
4. 確認收到 `skill_ui_open` 並彈出 iframe modal。
5. Step-1 填表後提交，確認仍為 UI 並進入 step-2。
6. Step-2 送出後，確認收到 `mode=final` 與 assistant message。
7. 重新整理頁面，確認對話歷史可看到最終完成訊息。
