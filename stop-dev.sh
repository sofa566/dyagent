#!/usr/bin/env bash
set -euo pipefail

ROOT="/hd18/23_Rasa/dyagent"

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
    kill_process_group_by_pid "$pid"
  done <<< "$pids"

  sleep 1

  # 若仍存在，強制終止整個 process group
  pids="$(pgrep -f "$pattern" || true)"
  while IFS= read -r pid; do
    [ -z "$pid" ] && continue
    force_kill_process_group_by_pid "$pid"
  done <<< "$pids"
}

kill_listeners_by_port() {
  local port="$1"
  local label="$2"
  local pids
  pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -z "$pids" ]; then
    echo "[skip] $label: port $port already free"
    return
  fi

  echo "[info] $label: stopping listeners on port $port"
  while IFS= read -r pid; do
    [ -z "$pid" ] && continue
    kill_process_group_by_pid "$pid"
  done <<< "$pids"

  sleep 1

  pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  while IFS= read -r pid; do
    [ -z "$pid" ] && continue
    force_kill_process_group_by_pid "$pid"
  done <<< "$pids"
}

# backend (uvicorn + python -m uvicorn)
stop_group_by_pattern "$ROOT/.venv/bin/uvicorn|python -m uvicorn|src.api.main:app|src.main:app" "backend"

# frontend (vite dev server)
stop_group_by_pattern "$ROOT/frontend/node_modules/.bin/vite|vite --host|vite$" "frontend"

kill_listeners_by_port 8000 "backend"
kill_listeners_by_port 5173 "frontend"

# 清理可能殘留的 multiprocessing 子程序
pkill -f "$ROOT/.venv/bin/python -c from multiprocessing.spawn import spawn_main" 2>/dev/null || true
pkill -f "$ROOT/.venv/bin/python -c from multiprocessing.resource_tracker import main" 2>/dev/null || true

echo "[done] dyagent backend/frontend stopped"
