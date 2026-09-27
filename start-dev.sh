#!/usr/bin/env bash
set -euo pipefail

ROOT="/hd18/23_Rasa/dyagent"
FRONTEND_DIR="$ROOT/frontend"
FRONTEND_LOG="/tmp/dyagent-frontend.log"
BACKEND_DIR="$ROOT/backend"
CELERY_WORKER_LOG="/tmp/dyagent-celery-worker.log"
CELERY_BEAT_LOG="/tmp/dyagent-celery-beat.log"

load_backend_env() {
  set -a
  if [ -f "$ROOT/backend/.env" ]; then
    # shellcheck disable=SC1090
    . "$ROOT/backend/.env"
  fi
  if [ -f "$ROOT/backend/.env.local" ]; then
    # shellcheck disable=SC1090
    . "$ROOT/backend/.env.local"
  fi
  set +a
}

echo "[info] starting dependencies..."
docker compose up -d postgres redis qdrant

"$ROOT/restart-backend.sh"

load_backend_env

CELERY_WORKER_CONCURRENCY="${CELERY_WORKER_CONCURRENCY:-2}"
CELERY_WORKER_POOL="${CELERY_WORKER_POOL:-prefork}"

if pgrep -f "$ROOT/frontend/node_modules/.bin/vite|node .*vite" >/dev/null; then
  echo "[skip] frontend already running"
else
  echo "[info] starting frontend..."
  (
    cd "$FRONTEND_DIR"
    npm run build
    nohup npm run dev -- --host 0.0.0.0 --port 5173 > "$FRONTEND_LOG" 2>&1 &
  )
fi

if pgrep -f "celery.*src.worker.celery_app:celery_app worker" >/dev/null; then
  echo "[skip] celery worker already running"
else
  echo "[info] starting celery worker..."
  (
    cd "$BACKEND_DIR"
    nohup "$ROOT/.venv/bin/celery" -A src.worker.celery_app:celery_app worker -l info --concurrency "$CELERY_WORKER_CONCURRENCY" --pool "$CELERY_WORKER_POOL" > "$CELERY_WORKER_LOG" 2>&1 &
  )
fi

if pgrep -f "celery.*src.worker.celery_app:celery_app beat" >/dev/null; then
  echo "[skip] celery beat already running"
else
  echo "[info] starting celery beat (redbeat)..."
  (
    cd "$BACKEND_DIR"
    nohup "$ROOT/.venv/bin/celery" -A src.worker.celery_app:celery_app beat -l info > "$CELERY_BEAT_LOG" 2>&1 &
  )
fi

echo "[done] dev stack started"
echo "- backend: http://127.0.0.1:8000"
echo "- frontend: http://127.0.0.1:5173"
echo "- celery worker concurrency: $CELERY_WORKER_CONCURRENCY"
echo "- celery worker pool: $CELERY_WORKER_POOL"
echo "- celery worker log: $CELERY_WORKER_LOG"
echo "- celery beat log: $CELERY_BEAT_LOG"
