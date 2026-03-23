#!/usr/bin/env bash
set -euo pipefail

ROOT="/hd18/23_Rasa/dyagent"
HEALTH_URL="http://127.0.0.1:8000/health"
INTERVAL_SEC="${1:-10}"
MAX_FAILS="${2:-3}"
fails=0

echo "[info] watchdog started (interval=${INTERVAL_SEC}s, max_fails=${MAX_FAILS})"

while true; do
  if curl -sS -m 3 "$HEALTH_URL" >/dev/null; then
    if [ "$fails" -ne 0 ]; then
      echo "[info] health recovered"
    fi
    fails=0
  else
    fails=$((fails + 1))
    echo "[warn] health check failed (${fails}/${MAX_FAILS})"
    if [ "$fails" -ge "$MAX_FAILS" ]; then
      echo "[warn] restarting backend by watchdog"
      "$ROOT/restart-backend.sh" || true
      fails=0
    fi
  fi
  sleep "$INTERVAL_SEC"
done
