#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${PROJECT_ROOT}/backend"
FRONTEND_DIR="${PROJECT_ROOT}/frontend"

printf '\n[quality-gate] 1/3 ruff check .\n'
ruff check .

printf '\n[quality-gate] 2/3 pytest backend integration smoke\n'
pytest backend/tests/integration/test_renal_care_api.py backend/tests/integration/test_health_education_api.py

printf '\n[quality-gate] 3/3 frontend build\n'
if [ -d "${FRONTEND_DIR}/node_modules" ]; then
  npm run build --prefix "${FRONTEND_DIR}"
else
  npm ci --prefix "${FRONTEND_DIR}"
  npm run build --prefix "${FRONTEND_DIR}"
fi

printf '\n[quality-gate] done\n'
