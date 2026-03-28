#!/usr/bin/env bash
set -euo pipefail

ROOT="/hd18/23_Rasa/dyagent"
FRONTEND_DIR="$ROOT/frontend"
FRONTEND_LOG="/tmp/dyagent-frontend.log"

echo "[info] starting dependencies..."
docker compose up -d postgres redis qdrant

"$ROOT/restart-backend.sh"

if pgrep -f "$ROOT/frontend/node_modules/.bin/vite|node .*vite" >/dev/null; then
  echo "[skip] frontend already running"
else
  echo "[info] starting frontend..."
  (
    cd "$FRONTEND_DIR"
    npm run build
    nohup npm run dev > "$FRONTEND_LOG" 2>&1 &
  )
  disown || true
fi

echo "[done] dev stack started"
echo "- backend: http://127.0.0.1:8000"
echo "- frontend: http://127.0.0.1:5173"
