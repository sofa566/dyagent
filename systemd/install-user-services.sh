#!/usr/bin/env bash
set -euo pipefail

ROOT="/hd18/23_Rasa/dyagent"
USER_SYSTEMD_DIR="$HOME/.config/systemd/user"

mkdir -p "$USER_SYSTEMD_DIR"

cp "$ROOT/systemd/dyagent-backend.service" "$USER_SYSTEMD_DIR/dyagent-backend.service"
cp "$ROOT/systemd/dyagent-watchdog.service" "$USER_SYSTEMD_DIR/dyagent-watchdog.service"

systemctl --user daemon-reload
systemctl --user enable --now dyagent-backend.service
systemctl --user enable --now dyagent-watchdog.service

echo "[done] installed and started: dyagent-backend, dyagent-watchdog"
echo "[hint] check status: systemctl --user status dyagent-backend dyagent-watchdog"
