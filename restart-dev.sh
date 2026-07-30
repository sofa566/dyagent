#!/usr/bin/env bash
set -euo pipefail

ROOT="/hd18/23_Rasa/dyagent"

bash "$ROOT/stop-dev.sh"
bash "$ROOT/start-dev.sh"

echo "[info] listeners after restart:"
lsof -i :8000 -n -P || true
lsof -i :5173 -n -P || true
