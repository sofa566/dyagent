# 腎友照護 POC 現場 Demo 劇本

## 0) Demo 前準備（3 分鐘）

1. 後端已啟動且已跑 migration：`alembic upgrade head`
2. 前端已啟動：`http://<host>:5173`
3. 確認環境變數：
   - `LINE_CHANNEL_ACCESS_TOKEN` 已設定
   - `LINE_RENAL_COMPANION_AGENT_NAME=腎友陪伴`
   - `MONITORING_REMINDER_SCHEDULER_ENABLED=true`（若要演示自動排程）
4. 先登入後台管理頁，開啟：
   - `LINE 對話中心`（`/pages/line-console.html`）
   - `腎友照護護理師頁`（`/pages/renal-care-nurse.html`）

---

## 1) 劇本總覽（建議 8~12 分鐘）

1. LINE 訊息進站後，固定走「腎友陪伴」代理
2. 腎友回報早晚監測（血壓、體重）
3. 糖尿病腎友缺血糖會被補問/攔截
4. 人工接手（human 模式）
5. 觸發催報（手動 dispatch）並展示去重

---

## 2) 場景 A：LINE 腎友固定路由到「腎友陪伴」

### 操作

1. 用已綁定腎友帳號在 LINE 傳：`我要回報今天狀況`
2. 到 `LINE 對話中心` 點開該 session。

### 要講的重點

- 這條會話的 `assigned_agent_id` 已被系統固定到「腎友陪伴」，不經主代理再分派。
- 這可避免一般聊天語氣混入，讓醫療場景回覆更一致。

### 成功畫面

- session 可看到最新 inbound 訊息。
- 系統回覆內容聚焦在回報引導，不是通用閒聊。

---

## 3) 場景 B：早晚回報固定欄位

### 操作（LINE 對話）

1. 腎友傳：`早上回報`
2. 腎友傳：`血壓 128/76`
3. 腎友傳：`體重 63.4`

### 要講的重點

- 「腎友陪伴」會逐步收集欄位，不會一次丟很多問題。
- 早/晚都要求血壓（收縮/舒張）與體重。

### 成功畫面

- LINE 回覆會有確認語句，例如已收到數值、下一步要填什麼。
- 後台訊息稽核可看到完整對話軌跡。

---

## 4) 場景 C：糖尿病腎友缺血糖攔截

### 操作（API 示範，Postman/curl）

對 `is_diabetic=true` 病患送出晚間回報，但不給血糖：

```bash
curl -X POST "http://localhost:8000/api/monitoring-records" \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "patient_id": "P001",
    "record_type": "EVENING",
    "recorded_at": "2026-09-12T21:05:00+08:00",
    "submitted_by_role": "PATIENT",
    "measurements": {"systolic": 130, "diastolic": 78, "weight_kg": 63.6},
    "symptoms": {},
    "confirmed": true
  }'
```

### 預期

- 會被拒絕（缺 `blood_glucose_mg_dl`）。
- 對非糖友則可不填血糖（可口頭說明，或再示範一次成功請求）。

---

## 5) 場景 D：人工接手（human 模式）

### 操作

1. 在 `LINE 對話中心` 把該 session 切到 `human`。
2. 腎友再傳一則訊息。
3. 觀察：系統僅收訊，不會自動 AI 回覆。
4. 由操作員在後台送出人工訊息。

### 要講的重點

- 人工接手時仍保留同一會話與同一稽核軌跡。
- 醫療邊界可由護理師介入，不強迫 AI 自動回。

---

## 6) 場景 E：催報派送 + 去重

### Step 1：查每日完整性

```bash
curl "http://localhost:8000/api/monitoring-compliance/daily?date=2026-09-12" \
  -H "Authorization: Bearer <ADMIN_TOKEN>"
```

重點看欄位：`morning_done`、`evening_done`、`glucose_complete`、`compliant`。

### Step 2：手動觸發晚間催報

```bash
curl -X POST "http://localhost:8000/api/monitoring-compliance/reminders/dispatch" \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"date":"2026-09-12","window":"EVENING","force":false}'
```

### Step 3：同條件再打一次（驗證去重）

- 第二次回應預期 `scheduled=0`（或顯著下降），表示同日同時段不重複排送。

---

## 7) 收尾講法（30 秒）

1. LINE 腎友流量已固定走「腎友陪伴」，減少誤路由。
2. 回報規則已落到後端（早晚血壓+體重、糖友加血糖），不是只靠前端。
3. 已有可重用提醒排程底座（policy/job/log + dispatcher + 去重），可擴到其他場景。

---

## 8) 備援方案（若現場網路不穩）

1. LINE 收發失敗時，改用 webhook 模擬請求（Postman）示範後台訊息與狀態流。
2. 排程時間不易卡點時，直接用手動 dispatch API 演示完整性與去重。
3. 若 token 問題導致無法推送，展示 job/log 狀態 `failed/skipped` 也可說明可追溯性。
