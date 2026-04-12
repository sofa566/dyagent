# 員工請假技能（Node.js 範例）

這是一個示範「前端兩步驟表單 + Node.js BFF + 假應用系統 API」的最小範例。

## 流程說明

1. 使用者在前端填寫請假資料（步驟 1/2）。
2. 前端顯示確認畫面（步驟 2/2）。
3. 前端呼叫本服務 `POST /api/leave-requests`。
4. 本服務再轉呼叫假應用系統 API：`POST /mock-application-system/leave-requests`。
5. 回傳申請單號與審核狀態。

## 啟動方式

```bash
cd examples/employee-leave-skill-node
npm install
npm run dev
```

瀏覽器開啟：`http://localhost:3456`

## 可替換為真實應用系統 API 的位置

- 檔案：`src/server.js`
- 函式：`fetchApplicationSystem()`
- 調整項目：
  - 改為真實 API 網址（`APPLICATION_API_BASE_URL`）
  - 加入授權 header（例如 Bearer Token）
  - 依目標系統格式調整 `mapToApplicationPayload()`

## 為什麼不建議前端直接呼叫應用系統 API

- 避免將 API key/secret 暴露在前端。
- 集中做欄位驗證、錯誤轉譯、重試與日誌。
- 第三方 API 調整時，可只改後端，不必整包前端重發。
