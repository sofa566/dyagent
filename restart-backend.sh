#!/usr/bin/env bash
set -euo pipefail

ROOT="/hd18/23_Rasa/dyagent"
BACKEND_DIR="$ROOT/backend"
LOG_FILE="/tmp/dyagent-backend.log"
QDRANT_CLIENT_PINNED_VERSION="1.9.1"

kill_process_group_by_pid() {
  local pid="$1"
  [ -z "$pid" ] && return
  local pgid
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  if [ -n "$pgid" ]; then
    kill -TERM "-$pgid" 2>/dev/null || true
  else
    kill -TERM "$pid" 2>/dev/null || true
  fi
}

force_kill_process_group_by_pid() {
  local pid="$1"
  [ -z "$pid" ] && return
  local pgid
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  if [ -n "$pgid" ]; then
    kill -KILL "-$pgid" 2>/dev/null || true
  else
    kill -KILL "$pid" 2>/dev/null || true
  fi
}

stop_backend_processes() {
  local pids
  pids="$(pgrep -f "$ROOT/.venv/bin/uvicorn|python -m uvicorn|src.api.main:app|src.main:app" || true)"
  if [ -n "$pids" ]; then
    while IFS= read -r pid; do
      [ -z "$pid" ] && continue
      kill_process_group_by_pid "$pid"
    done <<< "$pids"
    sleep 1
  fi

  pids="$(lsof -ti tcp:8000 -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    while IFS= read -r pid; do
      [ -z "$pid" ] && continue
      force_kill_process_group_by_pid "$pid"
    done <<< "$pids"
  fi
}

ensure_qdrant_client_compatibility() {
  local installed_version
  installed_version="$($ROOT/.venv/bin/python - <<'PY'
from importlib.metadata import PackageNotFoundError, version
try:
    print(version('qdrant-client'))
except PackageNotFoundError:
    print('')
PY
)"

  if [ "$installed_version" = "$QDRANT_CLIENT_PINNED_VERSION" ]; then
    echo "[info] qdrant-client already pinned: $installed_version"
    return
  fi

  echo "[info] installing qdrant-client==$QDRANT_CLIENT_PINNED_VERSION (current: ${installed_version:-none})"
  "$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check --no-deps "qdrant-client==$QDRANT_CLIENT_PINNED_VERSION"
}

load_env_files() {
  # 讓 api_key_ref 類型的金鑰（如 OPENAI_API_KEY_TEAM_A）可從檔案注入到程序環境
  set -a
  if [ -f "$BACKEND_DIR/.env" ]; then
    # shellcheck disable=SC1090
    . "$BACKEND_DIR/.env"
  fi
  if [ -f "$BACKEND_DIR/.env.local" ]; then
    # shellcheck disable=SC1090
    . "$BACKEND_DIR/.env.local"
  fi
  set +a
}

echo "[info] restarting backend..."

load_env_files
ensure_qdrant_client_compatibility

stop_backend_processes

echo "[info] starting backend..."
(
  cd "$BACKEND_DIR"
  nohup "$ROOT/.venv/bin/python" -m uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --no-use-colors --workers 1 > "$LOG_FILE" 2>&1 &
)

ok=0
for _ in $(seq 1 20); do
  if curl -sS -m 3 "http://127.0.0.1:8000/health" >/dev/null; then
    ok=1
    break
  fi
  sleep 1
done

if [ "$ok" -eq 1 ]; then
  echo "[done] backend restarted and healthy"
else
  echo "[warn] backend started but health check still failing"
fi
