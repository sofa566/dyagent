# API Contracts：腎友免註冊入口（LINE 身分）

## 1. 目的

本文件定義腎友／家屬「不註冊、由 LINE 進入網頁」所需的最小 API 契約，供 POC 實作與驗收使用。

設計原則：

1. 不建立腎友平台帳密。
2. 只信任 LINE 已識別的 `line_user_id`。
3. 採短時效 token，避免連結長時間可重放。
4. 全程可撤銷、可重發、可追溯。

---

## 2. 流程摘要

1. 腎友在 LINE 與官方帳號互動。
2. 後端已擁有該使用者 `line_user_id`。
3. 系統產生一次性短效 magic link，透過 LINE 推送給腎友。
4. 腎友點擊連結進入腎友工作台。
5. 後端驗證 token，建立腎友工作台 session。

---

## 3. 端點定義

### 3.1 建立免註冊登入連結（護理師/系統）

- Method：`POST`
- Path：`/api/patient-auth/line/link/request`
- 權限：`護理師/管理角色` 或系統排程服務帳號

Request Body：

```json
{
  "patient_id": "P001",
  "line_user_id": "Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "ttl_seconds": 600,
  "reason": "daily_checkin"
}
```

欄位規則：

1. `patient_id`：必填，需存在且為可用病患。
2. `line_user_id`：必填，需與該病患綁定。
3. `ttl_seconds`：可選，範圍 `60~1800`，預設 `600`。
4. `reason`：可選，例 `daily_checkin`、`manual_support`。

Response 200：

```json
{
  "ok": true,
  "link_id": "5de4b0b4-7ec8-46f4-9d13-9e9c6e1d4f2f",
  "expires_at": "2026-09-05T12:30:00+08:00",
  "magic_link": "https://<host>/pages/renal-care-patient.html?token=<signed_token>"
}
```

錯誤：

- `404`：病患不存在
- `409`：病患與 `line_user_id` 映射不一致
- `422`：欄位格式不合法

---

### 3.2 驗證連結並登入腎友工作台（公開）

- Method：`POST`
- Path：`/api/patient-auth/line/login`
- 權限：公開（token 驗證）

Request Body：

```json
{
  "token": "<signed_token>",
  "device_fingerprint": "optional-client-hash"
}
```

Response 200：

```json
{
  "ok": true,
  "session_token": "<jwt_or_session_token>",
  "expires_at": "2026-09-05T20:00:00+08:00",
  "patient": {
    "id": "P001",
    "display_name": "王美華"
  }
}
```

行為規則：

1. token 需驗簽、未過期、未撤銷。
2. token 預設一次性使用，成功後立即失效。
3. 驗證成功建立腎友工作台 session（僅限 patient scope）。

錯誤：

- `401`：token 無效或驗簽失敗
- `410`：token 已過期或已使用
- `423`：token 已被撤銷

---

### 3.3 重發連結（護理師/系統）

- Method：`POST`
- Path：`/api/patient-auth/line/link/resend`
- 權限：`護理師/管理角色` 或系統排程服務帳號

Request Body：

```json
{
  "patient_id": "P001",
  "reason": "link_expired"
}
```

Response 200：

```json
{
  "ok": true,
  "new_link_id": "fc1d64d2-2ec7-45a4-9de3-d0c04de57c8d",
  "expires_at": "2026-09-05T12:45:00+08:00"
}
```

行為規則：

1. 重發時舊連結自動撤銷。
2. 系統可選擇立即以 LINE 推送新連結。

---

### 3.4 撤銷連結（護理師/管理）

- Method：`POST`
- Path：`/api/patient-auth/line/link/revoke`
- 權限：`護理師/管理角色`

Request Body：

```json
{
  "link_id": "5de4b0b4-7ec8-46f4-9d13-9e9c6e1d4f2f",
  "reason": "security_reset"
}
```

Response 200：

```json
{
  "ok": true,
  "revoked": true
}
```

---

## 4. Token 建議格式

可採 JWT 或簽章 payload，至少包含：

```json
{
  "iss": "dyagent",
  "sub": "patient:P001",
  "line_user_id": "Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "scope": "patient_portal_login",
  "link_id": "uuid",
  "iat": 1757044800,
  "exp": 1757045400
}
```

必備規則：

1. 有效期短（建議 10 分鐘）。
2. 需可查詢撤銷狀態（`link_id`）。
3. 不在 token 明文放個資。

---

## 5. 稽核事件

每次操作需寫入審計事件：

1. `patient_login_link_requested`
2. `patient_login_link_sent`
3. `patient_login_link_used`
4. `patient_login_link_expired`
5. `patient_login_link_revoked`
6. `patient_login_failed`

欄位至少含：`patient_id`、`line_user_id`、`operator_id`（若有）、`ip`、`user_agent`、`timestamp`。

---

## 6. 失敗回應格式（統一）

```json
{
  "detail": {
    "error": "token_expired",
    "code": "PATIENT_LOGIN_TOKEN_EXPIRED",
    "message": "登入連結已過期，請回到 LINE 重新取得連結。"
  }
}
```

建議錯誤碼：

1. `PATIENT_LOGIN_TOKEN_INVALID`
2. `PATIENT_LOGIN_TOKEN_EXPIRED`
3. `PATIENT_LOGIN_TOKEN_REVOKED`
4. `PATIENT_LINE_MAPPING_NOT_FOUND`
5. `PATIENT_LINE_MAPPING_MISMATCH`

---

## 7. POC 驗收條件

1. 腎友不註冊即可從 LINE 進入個人工作台。
2. 連結過期後不可登入，且可成功重發。
3. 撤銷後原連結無法再用。
4. 任何登入或失敗都有稽核事件可查。

---

## 8. S01 API 契約（日常監測）

### 8.0 LINE 腎友訊息路由規則

1. 來源為 LINE 且已綁定腎友之會話，固定由 `腎友陪伴` 代理處理。
2. 該流程不經一般聊天主代理二次分派。
3. 若 session `assigned_agent_id` 非 `腎友陪伴`，系統需於處理前自動修正。
4. `mode=human` 時停止自動 AI 回覆，但會話仍維持 `腎友陪伴` 上下文與稽核資料。

### 8.1 建立監測紀錄

- Method：`POST`
- Path：`/api/monitoring-records`
- 權限：
  - 護理師：可代填
  - 腎友/家屬：僅可填本人

Request Body：

```json
{
  "patient_id": "P001",
  "record_type": "MORNING",
  "recorded_at": "2026-09-02T06:12:00+08:00",
  "submitted_by_role": "PATIENT",
  "measurements": {
    "weight_kg": 66.4,
    "systolic": 176,
    "diastolic": 96,
    "pulse": 88,
    "blood_glucose_mg_dl": 268
  },
  "symptoms": {
    "fall": false,
    "cramp": false,
    "fever": false,
    "breathing_discomfort": false,
    "wound_changed": true,
    "other_note": "右腳傷口較紅"
  },
  "confirmed": true
}
```

欄位規則：

1. `record_type`：`MORNING` / `EVENING` / `SYMPTOM_REPORT`
2. `confirmed`：必須為 `true` 才可正式寫入
3. 當 `record_type` 為 `MORNING` 或 `EVENING` 時，`measurements.systolic`、`measurements.diastolic`、`measurements.weight_kg` 為必填
4. 糖尿病病患（`is_diabetic=true`）於 `MORNING` 或 `EVENING` 回報時，`measurements.blood_glucose_mg_dl` 為必填
5. 同一病患每日需至少有一筆 `MORNING` 與一筆 `EVENING`，若缺漏由催報機制補發提醒

Response 200：

```json
{
  "ok": true,
  "monitoring_record_id": "f3e58ca4-5c06-4f80-97de-a93d8ef0f781",
  "record_status": "SAVED",
  "comparison_result": {
    "dry_weight_kg": 63.0,
    "current_weight_kg": 66.4,
    "weight_delta_kg": 3.4,
    "weight_delta_percent": 5.4
  },
  "matched_rules": [
    {
      "rule_id": "RW-P001-001",
      "rule_version": "v1",
      "severity": "warning",
      "message": "體重增加超過個別門檻，請追蹤。"
    }
  ],
  "follow_up": {
    "required": true,
    "case_id": "c2d0f97a-620f-4f7e-84cd-0fa5163a4bd2",
    "assigned_nurse_id": "N001",
    "status": "OPEN"
  },
  "patient_message": "已收到您的填報，主護理師將持續追蹤。"
}
```

錯誤：

- `422`：欄位缺漏、格式錯誤、`confirmed=false`
- `403`：越權填報他人資料
- `404`：病患不存在

建議錯誤碼（擴充）：

1. `MONITORING_REQUIRED_BP_MISSING`
2. `MONITORING_REQUIRED_WEIGHT_MISSING`
3. `MONITORING_REQUIRED_GLUCOSE_MISSING`

### 8.3 查詢當日回報完整性（護理師/排程）

- Method：`GET`
- Path：`/api/monitoring-compliance/daily?date=2026-09-09`
- 權限：護理師、系統排程服務帳號

Response 200（摘要）：

```json
{
  "ok": true,
  "date": "2026-09-09",
  "patients": [
    {
      "patient_id": "P001",
      "is_diabetic": true,
      "morning_done": true,
      "evening_done": false,
      "glucose_complete": false,
      "compliant": false
    }
  ]
}
```

### 8.4 觸發催報提醒（系統排程）

- Method：`POST`
- Path：`/api/monitoring-compliance/reminders/dispatch`
- 權限：系統排程服務帳號

Request Body：

```json
{
  "date": "2026-09-09",
  "window": "EVENING"
}
```

Response 200：

```json
{
  "ok": true,
  "scheduled": 12,
  "sent": 10,
  "skipped": 2
}
```

### 8.2 查詢病患監測紀錄（護理師/本人）

- Method：`GET`
- Path：`/api/monitoring-records?patient_id=P001&days=7`

Response 200（摘要）：

```json
{
  "ok": true,
  "patient_id": "P001",
  "days": 7,
  "records": [
    {
      "id": "f3e58ca4-5c06-4f80-97de-a93d8ef0f781",
      "record_type": "MORNING",
      "recorded_at": "2026-09-02T06:12:00+08:00",
      "weight_kg": 66.4,
      "bp": "176/96",
      "follow_up_required": true
    }
  ]
}
```

---

## 9. S02 API 契約（透析療程）

### 9.1 建立透析 Session

- Method：`POST`
- Path：`/api/dialysis-sessions`
- 權限：護理師

Request Body：

```json
{
  "patient_id": "P001",
  "dialysis_date": "2026-09-05",
  "shift": "MORNING",
  "bed_no": "A01",
  "machine_no": "HDM-A01",
  "created_by": "N001"
}
```

Response 200：

```json
{
  "ok": true,
  "session_id": "ce0dc029-6d0d-412a-a9f3-e66ea5a347fa",
  "status": "CREATED"
}
```

### 9.2 更新洗前確認

- Method：`POST`
- Path：`/api/dialysis-sessions/{session_id}/pre-check`
- 權限：護理師

Request Body：

```json
{
  "pre_weight_kg": 66.4,
  "dry_weight_kg": 63.0,
  "target_uf_l": 3.0,
  "note": "透析日前指定藥物暫緩",
  "confirmed_by": "N001"
}
```

Response 200：

```json
{
  "ok": true,
  "session_id": "ce0dc029-6d0d-412a-a9f3-e66ea5a347fa",
  "status": "PRE_CHECK_CONFIRMED"
}
```

### 9.3 寫入透析中事件

- Method：`POST`
- Path：`/api/dialysis-events`
- 權限：護理師或模擬機構 API

Request Body：

```json
{
  "session_id": "ce0dc029-6d0d-412a-a9f3-e66ea5a347fa",
  "event_type": "BP_LOW",
  "event_at": "2026-09-05T10:10:00+08:00",
  "payload": {
    "systolic": 84,
    "diastolic": 52,
    "pulse": 90,
    "uf_accumulated_l": 2.2
  },
  "handled_by": "N001",
  "action": "暫停脫水並觀察"
}
```

Response 200：

```json
{
  "ok": true,
  "event_id": "d9e61530-fca6-486b-b269-cb8544af5a2d",
  "follow_up_required": true
}
```

### 9.4 洗後確認與完成

- Method：`POST`
- Path：`/api/dialysis-post-check`
- 權限：護理師

Request Body：

```json
{
  "session_id": "ce0dc029-6d0d-412a-a9f3-e66ea5a347fa",
  "post_weight_kg": 64.2,
  "actual_uf_l": 2.2,
  "completion_status": "COMPLETED",
  "has_tourniquet": true,
  "confirmed_by": "N001"
}
```

Response 200：

```json
{
  "ok": true,
  "session_id": "ce0dc029-6d0d-412a-a9f3-e66ea5a347fa",
  "status": "COMPLETED",
  "post_check_reminder": {
    "required": true,
    "after_minutes": 60,
    "message": "請確認止血帶已解除。"
  }
}
```

錯誤：

- `404`：`session_id` 不存在
- `409`：狀態轉移不合法（例如未 pre-check 先 post-check）
- `422`：欄位驗證失敗

---

## 10. 角色隔離規則（S01/S02）

1. 護理師可操作多病患資料。
2. 腎友/家屬僅可讀寫本人授權資料。
3. 腎友/家屬不可呼叫 S02 的護理端流程 API。

---

## 11. 版本註記

本契約為 POC v1 草案，後續若導入院內正式流程，將補充：

1. HIS/EMR 欄位映射
2. 正式設備接口驗證
3. 醫療法遵稽核欄位
