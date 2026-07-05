#!/bin/bash

pids="$(pgrep -f "$ROOT/.venv/bin/uvicorn|src.api.main:app" || true)"
if [ -n "$pids" ]; then
  while IFS= read -r pid; do
    [ -z "$pid" ] && continue
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [ -n "$pgid" ]; then
      kill -TERM "-$pgid" 2>/dev/null || true
    fi
  done <<< "$pids"
  sleep 1

  pids="$(pgrep -f "$ROOT/.venv/bin/uvicorn|src.api.main:app" || true)"
  while IFS= read -r pid; do
    [ -z "$pid" ] && continue
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [ -n "$pgid" ]; then
      kill -KILL "-$pgid" 2>/dev/null || true
    fi
  done <<< "$pids"
fi

ok=0
for _ in $(seq 1 20); do
  pids="$(pgrep -f "$ROOT/.venv/bin/uvicorn|src.api.main:app" || true)"
  if [ -z "$pids" ]; then
    ok=1
    break
  fi
  sleep 1
done

if [ "$ok" -eq 1 ]; then
  echo "[done] backend was stopped"
else
  echo "[fail] The backend is still running."
fi
