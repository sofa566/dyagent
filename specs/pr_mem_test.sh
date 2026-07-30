#!/usr/bin/env bash
set -euo pipefail

# 目的：一鍵完成 Mem0 記憶寫入與 A/B/C/D 搜尋驗證。
# 為什麼：避免人工逐步貼指令，降低測試流程遺漏與判讀誤差。

ROOT_DIR="/hd18/23_Rasa/dyagent"
BACKEND_DIR="$ROOT_DIR/backend"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
BASE_URL="${BASE_URL:-http://localhost:8000}"
LOGIN_EMAIL="${LOGIN_EMAIL:-admin@example.com}"
LOGIN_PASSWORD="${LOGIN_PASSWORD:-adminpassword}"
AGENT_ID="${AGENT_ID:-}"
WRONG_AGENT_ID="00000000-0000-0000-0000-000000000000"
RUN_ID="memtest-run-$(date +%s)"
MARKER="MEMTEST-SH-$(date +%s)"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "[error] 找不到虛擬環境 Python：$VENV_PYTHON"
  exit 1
fi

echo "[info] BASE_URL=$BASE_URL"
echo "[info] MARKER=$MARKER"

if ! curl -sS -m 3 "$BASE_URL/health" >/dev/null; then
  echo "[error] 後端未就緒，請先啟動服務"
  exit 1
fi

TOKEN="$(curl -sS -X POST "$BASE_URL/api/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$LOGIN_EMAIL\",\"password\":\"$LOGIN_PASSWORD\"}" \
  | "$VENV_PYTHON" -c 'import json,sys; data=json.load(sys.stdin); print(str(data.get("token") or ""))')"

if [ -z "$TOKEN" ]; then
  echo "[error] 登入失敗，未取得 token"
  exit 1
fi

export TOKEN

USER_ID="$($VENV_PYTHON - <<'PY'
import os, json, base64
token = os.environ["TOKEN"]
payload = token.split(".")[1]
payload += "=" * (-len(payload) % 4)
print(json.loads(base64.urlsafe_b64decode(payload)).get("sub", ""))
PY
)"

if [ -z "$USER_ID" ]; then
  echo "[error] 無法由 TOKEN 解出 USER_ID"
  exit 1
fi

if [ -z "$AGENT_ID" ]; then
  AGENT_ID="$(curl -sS -X GET "$BASE_URL/api/agents" \
    -H "Authorization: Bearer $TOKEN" \
    | "$VENV_PYTHON" -c 'import json,sys; data=json.load(sys.stdin); agents=data.get("agents") or []; print(next((str(a.get("id") or "") for a in agents if a.get("enabled", True) and (not a.get("is_router"))), ""))')"
fi

if [ -z "$AGENT_ID" ]; then
  echo "[error] 解析 AGENT_ID 失敗，請以 AGENT_ID=... 指定"
  exit 1
fi

echo "[info] USER_ID=$USER_ID"
echo "[info] AGENT_ID=$AGENT_ID"
echo "[info] RUN_ID=$RUN_ID"

echo "[step] 寫入三種 scope 記憶..."
(
cd "$BACKEND_DIR"
PYTHONPATH="$BACKEND_DIR:${PYTHONPATH:-}" \
USER_ID="$USER_ID" AGENT_ID="$AGENT_ID" RUN_ID="$RUN_ID" MARKER="$MARKER" \
"$VENV_PYTHON" - <<'PY'
import os
from src.services.memory_service import memory_service

user_id = os.environ["USER_ID"]
agent_id = os.environ["AGENT_ID"]
run_id = os.environ["RUN_ID"]
marker = os.environ["MARKER"]

print(f"provider={memory_service.provider_name}")
if memory_service.provider_name in {"off", "mock"}:
    raise SystemExit("memory provider is off/mock, 無法寫入 mem0，請檢查 backend/.env")

cases = [
    {
        "name": "interaction_scope",
        "scope_type": "interaction_scope",
        "messages": [
            {"role": "user", "content": f"請記住：{marker} interaction_scope 我喜歡無糖綠茶"},
            {"role": "assistant", "content": "已記住 interaction_scope。"},
        ],
    },
    {
        "name": "user_scope",
        "scope_type": "user_scope",
        "messages": [
            {"role": "user", "content": f"請記住：{marker} user_scope 我偏好烏龍茶"},
            {"role": "assistant", "content": "已記住 user_scope。"},
        ],
    },
    {
        "name": "agent_scope",
        "scope_type": "agent_scope",
        "messages": [
            {"role": "user", "content": f"請記住：{marker} agent_scope 我熟悉向量資料庫"},
            {"role": "assistant", "content": "已記住 agent_scope。"},
        ],
    },
]

failed = []
for item in cases:
    result = memory_service.write(
        messages=item["messages"],
        user_id=user_id,
        agent_id=agent_id,
        run_id=run_id,
        metadata={
            "scope_type": item["scope_type"],
            "write_reason": "pr_mem_test_script",
            "test_case": item["name"],
            "marker": marker,
        },
    )
    print(f"write {item['name']}: ok={result.ok} error={result.error or ''}")
    if not result.ok:
        failed.append(item["name"])

if failed:
    raise SystemExit(f"write failed: {','.join(failed)}")
PY
)

sleep 1

TMP_DIR="$(mktemp -d /tmp/pr-mem-test-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

run_search() {
  local label="$1"
  local payload="$2"
  local outfile="$TMP_DIR/$label.json"

  curl -sS -X POST "$BASE_URL/api/memory/search" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$payload" > "$outfile"

  echo ""
  echo "===== $label ====="
  cat "$outfile"
  echo ""
  "$VENV_PYTHON" - <<'PY' "$outfile"
import json, sys
path = sys.argv[1]
with open(path, 'r', encoding='utf-8') as fp:
    data = json.load(fp)
snippets = data.get('snippets') or []
scopes = [str(item.get('scope_type') or '') for item in snippets]
print(f"count={len(snippets)} scopes={scopes}")
PY
}

PAYLOAD_A="{\"user_id\":\"$USER_ID\",\"query\":\"烏龍茶\",\"top_k\":10}"
PAYLOAD_B="{\"agent_id\":\"$AGENT_ID\",\"query\":\"向量資料庫\",\"top_k\":10}"
PAYLOAD_C="{\"user_id\":\"$USER_ID\",\"agent_id\":\"$AGENT_ID\",\"run_id\":\"$RUN_ID\",\"query\":\"無糖綠茶\",\"top_k\":10}"
PAYLOAD_D="{\"user_id\":\"$USER_ID\",\"agent_id\":\"$WRONG_AGENT_ID\",\"query\":\"烏龍茶\",\"top_k\":10}"

echo "[step] 執行 A/B/C/D 搜尋測試..."
run_search "A_user_only" "$PAYLOAD_A"
run_search "B_agent_only" "$PAYLOAD_B"
run_search "C_user_and_agent" "$PAYLOAD_C"
run_search "D_user_and_wrong_agent" "$PAYLOAD_D"

echo ""
echo "[step] 自動判定結果..."
"$VENV_PYTHON" - <<'PY' "$TMP_DIR"
import json, os, sys

tmp_dir = sys.argv[1]

def read(name):
    with open(os.path.join(tmp_dir, f"{name}.json"), "r", encoding="utf-8") as fp:
        return json.load(fp)

def scope_set(data):
    return {str(item.get("scope_type") or "") for item in (data.get("snippets") or [])}

a = read("A_user_only")
b = read("B_agent_only")
c = read("C_user_and_agent")
d = read("D_user_and_wrong_agent")

a_scopes = scope_set(a)
b_scopes = scope_set(b)
c_scopes = scope_set(c)
d_scopes = scope_set(d)

checks = [
    ("A 含 user_scope", "user_scope" in a_scopes),
    ("B 含 agent_scope", "agent_scope" in b_scopes),
    ("C 含 interaction_scope", "interaction_scope" in c_scopes),
    ("D user_id + 錯誤 agent_id 為空", len(d_scopes) == 0),
]

all_passed = True
for title, passed in checks:
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {title}")
    if not passed:
        all_passed = False

print("\n[scope] A=", sorted(a_scopes))
print("[scope] B=", sorted(b_scopes))
print("[scope] C=", sorted(c_scopes))
print("[scope] D=", sorted(d_scopes))

if not all_passed:
    raise SystemExit(1)
PY

echo ""
echo "[done] 測試完成：A/B/C/D 全部通過"
echo "[note] 若要清除測試資料，可執行："
echo "curl -sS -X POST \"$BASE_URL/api/memory/users/$USER_ID/forget\" -H \"Authorization: Bearer $TOKEN\""
