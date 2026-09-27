# API Contracts：衛教內容提醒（MVP）

## 1. 端點清單

1. `POST /api/health-education/contents`
2. `POST /api/health-education/contents/import-candidates`
3. `POST /api/health-education/contents/import-from-agent`
4. `GET /api/health-education/source-policy`
5. `GET /api/health-education/contents?status=draft&limit=50`
6. `PUT /api/health-education/contents/{content_id}/approve`
7. `PUT /api/health-education/contents/{content_id}/reject`
8. `POST /api/health-education/contents/{content_id}/send-now`
9. `POST /api/health-education/contents/{content_id}/schedule`
10. `GET /api/health-education/contents/{content_id}/logs?limit=100`

## 2.1 批次匯入候選（Agent）

Request:

```json
{
  "agent_name": "腎友衛教搜尋",
  "items": [
    {
      "title": "透析飲食重點：如何控制鉀離子",
      "source_name": "udnhealth-hd",
      "source_url": "https://udnhealth-hd.com/HD",
      "summary": "整理透析病友在高鉀食物上的選擇原則",
      "tags": ["飲食", "透析", "鉀離子"]
    }
  ]
}
```

Response 200:

```json
{
  "ok": true,
  "agent_name": "腎友衛教搜尋",
  "created": 1,
  "skipped_duplicate": 0,
  "skipped_blocked": 0,
  "skipped_invalid": 0,
  "items": []
}
```

## 2.2 來源規則查詢

Response 200:

```json
{
  "ok": true,
  "policy": {
    "trusted_domains": ["udnhealth-hd.com"],
    "blocked_domains": [],
    "default_policy": "review_required"
  }
}
```

## 2.3 一鍵呼叫 Agent 搜尋並匯入

Request:

```json
{
  "agent_name": "腎友衛教搜尋",
  "topic": "透析飲食與生活照護",
  "limit": 5
}
```

Response 200:

```json
{
  "ok": true,
  "agent_name": "腎友衛教搜尋",
  "agent_id": "uuid",
  "topic": "透析飲食與生活照護",
  "requested_limit": 5,
  "parsed_items": 5,
  "created": 3,
  "skipped_duplicate": 1,
  "skipped_blocked": 1,
  "skipped_invalid": 0,
  "items": []
}
```

## 3. 建立內容

Request:

```json
{
  "title": "透析飲食重點：如何控制鉀離子",
  "source_name": "udnhealth-hd",
  "source_url": "https://udnhealth-hd.com/HD",
  "summary": "整理透析病友在高鉀食物上的選擇原則",
  "tags": ["飲食", "透析", "鉀離子"]
}
```

Response 200:

```json
{
  "ok": true,
  "item": {
    "id": "uuid",
    "status": "draft"
  }
}
```

## 4. 立即發送

Request:

```json
{
  "audience_rule": "all"
}
```

Response 200:

```json
{
  "ok": true,
  "content_id": "uuid",
  "audience_rule": "all",
  "scheduled": 10,
  "sent": 8,
  "skipped": 2,
  "failed": 0
}
```

## 5. 排程發送

Request:

```json
{
  "name": "衛教-每週三晚間",
  "cron_expression": "0 20 * * 3",
  "timezone": "Asia/Taipei",
  "audience_rule": "all",
  "enabled": true
}
```

Response 200:

```json
{
  "ok": true,
  "task": {
    "id": "task-uuid",
    "task_type": "health_education_dispatch"
  }
}
```

## 6. 錯誤回應

```json
{
  "detail": "內容尚未核准，無法發送"
}
```
