#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$ROOT_DIR/frontend"

if [ ! -d "$FRONTEND_DIR" ]; then
  echo "[錯誤] 找不到 frontend 目錄：$FRONTEND_DIR" >&2
  exit 1
fi

cd "$FRONTEND_DIR"
echo "[資訊] 啟動 frontend（Vite）..."
exec npm run dev
