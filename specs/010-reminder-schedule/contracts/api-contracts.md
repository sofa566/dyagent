# API Contracts：系統共用提醒排程

## 1. 目的

定義系統層提醒排程 API 契約，供腎友照護與後續業務模組共用。

---

## 2. 端點定義

> 註：2.1、2.2 為既有端點；2.4~2.9 為支援後台 UI 與 Celery 排程的新增端點。

### 2.1 查詢每日完整性（現有）

- Method：`GET`
- Path：`/api/monitoring-compliance/daily?date=YYYY-MM-DD`
- 權限：具 `nursing.trends` / `nursing.line` / `line.center` / `update_agent` 其一

Response 200：

```json
{
  "ok": true,
  "date": "2026-09-12",
  "summary": {
    "total_patients": 4,
    "compliant_patients": 2,
    "non_compliant_patients": 2
  },
  "patients": [
    {
      "patient_id": "P001",
      "patient_name": "王美華",
      "is_diabetic": true,
      "line_user_bound": true,
      "morning_done": true,
      "evening_done": false,
      "glucose_complete": false,
      "compliant": false
    }
  ]
}
```

---

### 2.2 手動觸發提醒派送（現有）

- Method：`POST`
- Path：`/api/monitoring-compliance/reminders/dispatch`
- 權限：同 2.1

Request Body：

```json
{
  "date": "2026-09-12",
  "window": "EVENING",
  "force": false
}
```

欄位規則：

1. `date`：可選，預設當天。
2. `window`：`MORNING` 或 `EVENING`。
3. `force`：可選，`true` 時忽略既有去重判斷。

Response 200：

```json
{
  "ok": true,
  "date": "2026-09-12",
  "window": "EVENING",
  "scheduled": 2,
  "sent": 1,
  "skipped": 1,
  "failed": 0
}
```

---

### 2.3 排程器設定（設定檔）

由環境變數控制：

1. `MONITORING_REMINDER_SCHEDULER_ENABLED`：是否啟用背景排程。
2. `MONITORING_REMINDER_SCHEDULER_INTERVAL_SECONDS`：輪詢秒數。
3. `MONITORING_REMINDER_MORNING_DISPATCH_TIME`：早晨提醒時間（HH:MM）。
4. `MONITORING_REMINDER_EVENING_DISPATCH_TIME`：晚間提醒時間（HH:MM）。

---

### 2.4 查詢提醒 Policy（新增）

- Method：`GET`
- Path：`/api/monitoring-compliance/reminders/policies`
- 權限：同 2.1

Response 200：

```json
{
  "ok": true,
  "items": [
    {
      "id": "policy-uuid",
      "window": "MORNING",
      "channel": "line",
      "enabled": true,
      "requires_glucose_for_diabetic": true,
      "message_template": "{patient_name} 您好，請完成{window}回報：血壓、體重{glucose_hint}。"
    }
  ]
}
```

### 2.5 更新提醒 Policy（新增）

- Method：`PUT`
- Path：`/api/monitoring-compliance/reminders/policies/{policy_id}`
- 權限：同 2.1

Request Body：

```json
{
  "enabled": true,
  "message_template": "{patient_name} 您好，請完成{window}回報：血壓、體重{glucose_hint}。",
  "requires_glucose_for_diabetic": true
}
```

Response 200：

```json
{
  "ok": true,
  "ok": true,
  "policy": {
    "id": "policy-uuid",
    "window": "MORNING",
    "enabled": true,
    "message_template": "{patient_name} 您好，請完成{window}回報：血壓、體重{glucose_hint}。"
  }
}
```

### 2.6 查詢派送紀錄（新增）

- Method：`GET`
- Path：`/api/monitoring-compliance/reminders/logs?date=YYYY-MM-DD&window=EVENING&status=failed`
- 權限：同 2.1

Response 200：

```json
{
  "ok": true,
  "summary": {
    "total_jobs": 4,
    "pending": 0,
    "sent": 3,
    "skipped": 1,
    "failed": 0
  },
  "jobs": [
    {
      "job_id": "job-uuid",
      "patient_id": "P001",
      "patient_name": "王美華",
      "window": "EVENING",
      "status": "sent",
      "scheduled_at": "2026-09-12T00:00:00+08:00",
      "processed_at": "2026-09-12T21:00:03+08:00",
      "error_message": "",
      "payload": {
        "message": "王美華您好，請完成晚間回報..."
      }
    }
  ]
}
```

### 2.7 排程任務管理（新增）

1. `GET /api/scheduler/tasks?enabled_only=false`
2. `POST /api/scheduler/tasks`
3. `PUT /api/scheduler/tasks/{task_id}`
4. `DELETE /api/scheduler/tasks/{task_id}`
5. `POST /api/scheduler/tasks/{task_id}/run`
6. `GET /api/scheduler/tasks/{task_id}/runs?limit=20`

建立任務 Request 範例：

```json
{
  "name": "LINE-早晨提醒",
  "description": "每天提醒腎友輸入早晨血壓與體重",
  "cron_expression": "0 6 * * *",
  "timezone": "Asia/Taipei",
  "template_id": "template-uuid",
  "payload": {
    "window": "MORNING",
    "force": false
  },
  "enabled": true
}
```

### 2.8 排程模板管理（新增）

1. `GET /api/scheduler/templates?enabled_only=false`
2. `POST /api/scheduler/templates`
3. `PUT /api/scheduler/templates/{template_id}`
4. `DELETE /api/scheduler/templates/{template_id}`

建立模板 Request 範例：

```json
{
  "template_key": "ops.python.daily-report",
  "name": "每日 Python 報表",
  "description": "每日執行報表彙整腳本",
  "executor_type": "python_script",
  "payload_schema": {
    "required": ["file_path"]
  },
  "default_payload": {
    "timeout_seconds": 120
  },
  "enabled": true
}
```

`executor_type` 支援：

1. `http_call`
2. `bash_script`
3. `nodejs_script`
4. `python_script`
5. `renal_reminder_dispatch`
6. `monitoring_backfill`

### 2.9 既有提醒排程設定（相容）

保留環境變數作為預設系統任務 seed 來源：

1. `MONITORING_REMINDER_MORNING_DISPATCH_TIME`
2. `MONITORING_REMINDER_EVENING_DISPATCH_TIME`
3. `MONITORING_BACKFILL_FIRST_RUN_TIME`
4. `MONITORING_BACKFILL_SECOND_RUN_TIME`

腳本執行安全限制（新增）：

5. `SCHEDULER_SCRIPT_ALLOWED_ROOTS`：腳本與工作目錄白名單（逗號分隔）。
6. `SCHEDULER_BASH_ALLOWED_PREFIXES`：bash 指令前綴白名單（逗號分隔）。

---

## 3. 錯誤回應（統一）

```json
{
  "detail": "window 僅支援 MORNING 或 EVENING"
}
```

常見錯誤：

1. `400`：輸入格式錯誤（日期、window）。
2. `403`：權限不足。
3. `500`：外部通道或資料庫未預期錯誤（單筆錯誤應記錄為 failed，不應使整批中斷）。

---

## 4. UI 互動契約（新增）

### 4.1 提醒管理頁區塊

1. **合規概覽區**：顯示 `total_patients/compliant/non_compliant`。
2. **病患明細區**：顯示 `morning_done/evening_done/glucose_complete/compliant`。
3. **手動派送區**：可選日期與時段，按下即呼叫 2.2。
4. **Policy 區**：顯示/編輯 policy，呼叫 2.4/2.5。
5. **投遞紀錄區**：顯示最近派送結果，呼叫 2.6。
6. **Crontab 任務區**：管理通用任務，呼叫 2.7。

### 4.2 前端行為要求

1. 派送按鈕提交中需 disabled，防止連點。
2. 派送完成後自動刷新合規與紀錄區。
3. API 失敗時顯示可理解錯誤訊息，不可靜默失敗。
