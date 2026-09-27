#!/usr/bin/env bash
set -euo pipefail

# 目的：停止本機 Celery worker / beat。
# 為什麼：避免殘留背景程序持續佔用 Redis lock 與系統資源。

WORKER_PATTERN='celery.*worker'
BEAT_PATTERN='celery.*beat'

print_running() {
  pgrep -af "${WORKER_PATTERN}|${BEAT_PATTERN}" || true
}

stop_gracefully() {
  pkill -TERM -f "${WORKER_PATTERN}" || true
  pkill -TERM -f "${BEAT_PATTERN}" || true
}

stop_forcefully() {
  pkill -KILL -f "${WORKER_PATTERN}" || true
  pkill -KILL -f "${BEAT_PATTERN}" || true
}

echo '[stop-celery] 停止前程序：'
print_running

echo '[stop-celery] 嘗試優雅停止 (SIGTERM)...'
stop_gracefully
sleep 3

if pgrep -f "${WORKER_PATTERN}|${BEAT_PATTERN}" >/dev/null; then
  echo '[stop-celery] 偵測到殘留程序，改用強制停止 (SIGKILL)...'
  stop_forcefully
  sleep 1
fi

echo '[stop-celery] 停止後程序：'
print_running

if pgrep -f "${WORKER_PATTERN}|${BEAT_PATTERN}" >/dev/null; then
  echo '[stop-celery] 仍有程序未停止，請手動檢查。'
  exit 1
fi

echo '[stop-celery] Celery 已全部停止。'
