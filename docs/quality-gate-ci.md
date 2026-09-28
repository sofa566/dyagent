# 品質閘道與 CI 操作文件

## 目的

本文件定義 dyagent 專案的品質檢查流程，讓本機檢查與 GitHub Actions 檢查一致，降低「本機可過、CI 失敗」的情況。

## 目前治理流程狀態

1. 品質閘道腳本：已完成（`tools/quality-gate.sh`）。
2. CI workflow：已完成，檔案為 `.github/workflows/ci.yml`。
3. 操作文件：本文件。

## CI 觸發條件

CI 會在以下情境觸發：

- Push 到 `main` 或 `master`
- Pull Request 目標分支為 `main` 或 `master`

## CI 檢查內容

### 1) Backend Quality

- 啟動服務容器：`redis:7`、`qdrant/qdrant:v1.9.0`
- 安裝後端依賴：`pip install -r backend/requirements.txt`
- 執行 Lint：`ruff check .`
- 執行關鍵整合測試：
  - `pytest backend/tests/integration/test_renal_care_api.py backend/tests/integration/test_health_education_api.py`

### 2) Frontend Build

- 安裝前端依賴：`npm ci`（於 `frontend/`）
- 執行建置：`npm run build`（於 `frontend/`）

## 本機對齊執行方式

在送 PR 前，建議在專案根目錄直接執行：

```bash
./tools/quality-gate.sh
```

若要拆開逐步檢查，可依序執行：

```bash
ruff check .
pytest backend/tests/integration/test_renal_care_api.py backend/tests/integration/test_health_education_api.py
cd frontend && npm ci && npm run build
```

## 常見失敗與排查

### Ruff 失敗

- 先看錯誤檔案是否在 `backend/alembic/`（migration 檔常見 import/type annotation 規則）
- 可先嘗試：

```bash
ruff check backend/alembic --fix --unsafe-fixes
ruff check .
```

### 後端整合測試失敗

- 優先檢查測試資料是否符合目前綁定規則：
  - 優先 `line_channel_sessions(status=active,binding_status=bound)`
  - 主檔 fallback 需同時有 `line_user_id + tel_no`
- 若測試目標是驗證主檔 fallback 發送成功，請補齊 `tel_no`。

### 前端建置失敗

- 先確認鎖檔是否一致：`frontend/package-lock.json`
- 建議重裝依賴後重試：

```bash
cd frontend
rm -rf node_modules
npm ci
npm run build
```

## 品質閘道腳本內容

`tools/quality-gate.sh` 目前封裝以下流程：

1. `ruff check .`
2. `pytest backend/tests/integration/test_renal_care_api.py backend/tests/integration/test_health_education_api.py`
3. `frontend` 的 `npm run build`（若無 `node_modules` 會先執行 `npm ci`）
