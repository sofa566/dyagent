# PR 記憶測試手冊（Mem0）

本文件提供一套可重複執行的指令，先寫入三種 scope 記憶，再驗證搜尋情境 A/B/C/D。

## 0) 前置條件

- 後端已啟動：`http://127.0.0.1:8000`
- 記憶 provider 為 `mem0_oss`
- 以 `admin` 帳號登入（`/api/memory/search` 為管理端點）

## 1) 取得 TOKEN / USER_ID

```bash
TOKEN=$(curl -sS -X POST "http://localhost:8000/api/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"adminpassword"}' \
  | python -c 'import sys,json; print(json.load(sys.stdin).get("token",""))')

echo "TOKEN_LEN=${#TOKEN}"

export TOKEN
USER_ID=$(python - <<'PY'
import os, json, base64
token = os.environ["TOKEN"]
payload = token.split(".")[1]
payload += "=" * (-len(payload) % 4)
print(json.loads(base64.urlsafe_b64decode(payload)).get("sub", ""))
PY
)

echo "USER_ID=$USER_ID"
```

## 2) 指定測試用 AGENT_ID / RUN_ID

> `AGENT_ID` 請填一個你環境裡已存在的代理者 ID。

```bash
AGENT_ID="a6632323-697b-4d8e-a7bc-f8ede14f0196"
RUN_ID="memtest-run-001"

echo "AGENT_ID=$AGENT_ID"
echo "RUN_ID=$RUN_ID"
```

## 3) 寫入三種形式記憶（interaction_scope / user_scope / agent_scope）

> 這段會直接呼叫 backend 內部 `memory_service.write()`，可精準控制 `scope_type`。

```bash
USER_ID="$USER_ID" AGENT_ID="$AGENT_ID" RUN_ID="$RUN_ID" \
./.venv/bin/python - <<'PY'
import os
from src.services.memory_service import memory_service

user_id = os.environ["USER_ID"]
agent_id = os.environ["AGENT_ID"]
run_id = os.environ["RUN_ID"]

cases = [
    {
        "name": "interaction_scope",
        "scope_type": "interaction_scope",
        "messages": [
            {"role": "user", "content": "請記住：互動記憶飲料是無糖綠茶 INTERACTION-001"},
            {"role": "assistant", "content": "已記住互動記憶。"},
        ],
    },
    {
        "name": "user_scope",
        "scope_type": "user_scope",
        "messages": [
            {"role": "user", "content": "請記住：使用者偏好是烏龍茶 USER-001"},
            {"role": "assistant", "content": "已記住使用者偏好。"},
        ],
    },
    {
        "name": "agent_scope",
        "scope_type": "agent_scope",
        "messages": [
            {"role": "user", "content": "請記住：代理專業詞彙是向量資料庫 AGENT-001"},
            {"role": "assistant", "content": "已記住代理專業資訊。"},
        ],
    },
]

for item in cases:
    result = memory_service.write(
        messages=item["messages"],
        user_id=user_id,
        agent_id=agent_id,
        run_id=run_id,
        metadata={
            "scope_type": item["scope_type"],
            "write_reason": "pr_mem_test",
            "test_case": item["name"],
        },
    )
    print(item["name"], "ok=", result.ok, "error=", result.error or "")
PY
```

## 4) 搜尋驗證指令

### A. 只帶 user_id（應命中 interaction_scope + user_scope）

```bash
curl -sS -X POST "http://localhost:8000/api/memory/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$USER_ID\",\"query\":\"無糖綠茶 烏龍茶\",\"top_k\":10}"
```

### B. 只帶 agent_id（應命中 agent_scope；interaction_scope 是否命中視規則而定）

```bash
curl -sS -X POST "http://localhost:8000/api/memory/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"agent_id\":\"$AGENT_ID\",\"query\":\"向量資料庫 AGENT-001\",\"top_k\":10}"
```

### C. 同時帶 user_id + agent_id（應命中 interaction_scope + user_scope + agent_scope）

```bash
curl -sS -X POST "http://localhost:8000/api/memory/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$USER_ID\",\"agent_id\":\"$AGENT_ID\",\"query\":\"INTERACTION-001 USER-001 AGENT-001\",\"top_k\":10}"
```

### D. user_id + 錯誤 agent_id（應命中 user_scope；不應命中 interaction_scope/agent_scope）

```bash
curl -sS -X POST "http://localhost:8000/api/memory/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$USER_ID\",\"agent_id\":\"00000000-0000-0000-0000-000000000000\",\"query\":\"INTERACTION-001 USER-001 AGENT-001\",\"top_k\":10}"
```

## 5) 驗證刪除（Forget）

```bash
curl -sS -X POST "http://localhost:8000/api/memory/users/$USER_ID/forget" \
  -H "Authorization: Bearer $TOKEN"

curl -sS -X POST "http://localhost:8000/api/memory/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$USER_ID\",\"query\":\"無糖綠茶 烏龍茶 向量資料庫\",\"top_k\":10}"
```

預期：Forget 後 `snippets` 應為空陣列。
