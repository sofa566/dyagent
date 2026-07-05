#!/usr/bin/env bash
set -u

ROOT="/hd18/23_Rasa/dyagent"

stop_group_by_pattern() {
  local pattern="$1"
  local label="$2"
  local pids

  pids="$(pgrep -f "$pattern" || true)"
  if [ -z "$pids" ]; then
    echo "[skip] $label: no matching process"
    return
  fi

  echo "[info] $label: stopping process groups"
  while IFS= read -r pid; do
    [ -z "$pid" ] && continue
    local pgid
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [ -n "$pgid" ]; then
      kill -TERM "-$pgid" 2>/dev/null || true
    fi
  done <<< "$pids"

  sleep 1

  # 若仍存在，強制終止整個 process group
  pids="$(pgrep -f "$pattern" || true)"
  while IFS= read -r pid; do
    [ -z "$pid" ] && continue
    local pgid
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [ -n "$pgid" ]; then
      kill -KILL "-$pgid" 2>/dev/null || true
    fi
  done <<< "$pids"
}

# backend (uvicorn reload parent)
stop_group_by_pattern "$ROOT/.venv/bin/uvicorn src.api.main:app|src.api.main:app" "backend"

# frontend (vite dev server)
stop_group_by_pattern "$ROOT/frontend/node_modules/.bin/vite|vite --host|vite$" "frontend"

# 清理可能殘留的 multiprocessing 子程序
pkill -f "$ROOT/.venv/bin/python -c from multiprocessing.spawn import spawn_main" 2>/dev/null || true
pkill -f "$ROOT/.venv/bin/python -c from multiprocessing.resource_tracker import main" 2>/dev/null || true

echo "[done] dyagent backend/frontend stopped"
