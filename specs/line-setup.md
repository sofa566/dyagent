# LINE 串接設定指南

本文件整理 dyagent 串接 LINE Messaging API 時，系統端必備設定、取得路徑與 LINE OA 後台建議開關。

## 一、系統端環境變數

以下變數設定於後端服務（例如 `.env`、systemd、Docker env）：

1. `LINE_CHANNEL_SECRET`
   - 用途：驗證 `X-Line-Signature`，確認 webhook 請求確實來自 LINE。
   - 建議：正式環境必填。

2. `LINE_CHANNEL_ACCESS_TOKEN`
   - 用途：呼叫 LINE Reply / Push API 發送訊息。
   - 建議：正式環境必填，使用 long-lived token。

3. `LINE_WEBHOOK_VERIFY_SIGNATURE`
   - 用途：是否啟用 webhook 簽章驗證。
   - 建議：正式環境設為 `true`；本機除錯可暫時 `false`。

4. `LINE_DEFAULT_AGENT_ID`
   - 用途：新 LINE 使用者首次進來時，預設指派的 Agent。
   - 建議：填入已啟用且可回覆的 Agent UUID。

## 二、在 LINE 平台哪裡取得

### A. LINE Developers Console（主要）

路徑：LINE Developers Console -> Provider -> Messaging API Channel

1. `LINE_CHANNEL_SECRET`
   - 位置：`Basic settings` -> `Channel secret`

2. `LINE_CHANNEL_ACCESS_TOKEN`
   - 位置：`Messaging API` 分頁 -> `Channel access token`
   - 建議：使用可長期使用的 token（long-lived）

3. Webhook URL
   - 位置：`Messaging API` 分頁 -> `Webhook URL`
   - 設定值：`https://<你的網域>/api/line/webhook`
   - 設定後執行 `Verify` 測試。

### B. LINE Official Account Manager（OA Manager）

OA Manager 主要是營運設定，不提供 Channel secret/token；secret/token 仍以 Developers Console 為準。

## 三、LINE OA 後台建議開關

路徑可能依 OA Manager 版本略有不同，通常在「回應設定 / 聊天設定 / Messaging API」相關頁。

1. 自動回應訊息（Auto-reply message）
   - 建議：關閉
   - 原因：避免和 dyagent 回覆重複，造成一問兩答。

2. 歡迎訊息（Greeting message）
   - 建議：依需求
   - 若你希望「只由 dyagent 回覆」，可關閉。

3. Webhook（Use webhook）
   - 建議：開啟
   - 原因：需讓 LINE 把使用者訊息送到 dyagent。

4. Chat / AI 自動回覆相關功能
   - 建議：關閉或避免與 Messaging API 同時啟用同類自動回覆。
   - 原因：避免和 dyagent 的 bot 模式衝突。

## 四、最小可用檢查清單

1. 後端 `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN` 已設定。
2. `LINE_WEBHOOK_VERIFY_SIGNATURE=true`（正式環境）。
3. Developers Console 的 `Webhook URL` 指向 `/api/line/webhook` 且 Verify 成功。
4. OA Manager 的自動回應已關閉（避免重複訊息）。
5. `LINE_DEFAULT_AGENT_ID` 指向可用 Agent。

## 五、如何加入其他 LINE 使用者

本系統不需要為每位 LINE 使用者建立帳號或發放 token。只要對方加入你的官方帳號好友，即可開始對話。

### A. 提供加入方式給外部使用者

可擇一或並行使用：

1. 好友邀請連結
   - 位置：LINE Official Account Manager（OA Manager）內的好友招募/加好友頁。
   - 作法：複製官方帳號加好友連結，傳給外部使用者。

2. QR Code
   - 位置：同一頁通常可取得加好友 QR Code。
   - 作法：提供圖片給外部使用者掃描加入。

3. LINE ID 搜尋
   - 位置：OA 設定中的帳號 ID。
   - 作法：提供 ID 讓使用者在 LINE App 搜尋加入。

### B. 使用者加入後，系統自動流程

1. 使用者對官方帳號送出第一則訊息。
2. LINE 將事件送到 `POST /api/line/webhook`。
3. 系統以 `line_user_id` 建立或復用 `LineChannelSession`。
4. 在 `LINE 對話中心` 可看到新會話並可切換 `bot/human`。

### C. 讓「其他人可加入且可對話」的必要條件

1. LINE Developers 的 `Use webhook` 已開啟。
2. `Webhook URL` 為 `https://<你的網域>/api/line/webhook` 並 Verify 成功。
3. OA Manager 未啟用會干擾的自動回應（避免一問兩答）。
4. 後端 `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN` 已正確設定。

### D. 常見誤解

1. 誤解：每位使用者都要輸入 `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`。
   - 事實：不用。這是官方帳號層級設定，只在伺服器端設定一次。
2. 誤解：使用者加入好友後就一定會有回覆。
   - 事實：若 webhook 未啟用、URL 錯誤或 token 無效，仍可能無法回覆。

### E. 給外部測試者的邀請文案（可直接複製）

#### 1) 文字邀請版

```text
你好，我們正在測試 LINE 客服機器人，請協助以下步驟：

1. 點擊此連結加入官方帳號好友：
   <請貼上好友連結>
2. 加入後請直接傳送一句測試訊息，例如：
   今天台北天氣如何？
3. 若 10 秒內未收到回覆，請截圖回傳給我（含你送出的訊息時間）。

謝謝協助！
```

#### 2) QR Code 邀請版

```text
你好，請協助測試 LINE 客服機器人：

1. 掃描附上的 QR Code 加入官方帳號。
2. 加入後傳送一句測試訊息：我要人工客服。
3. 若未收到回覆，請提供截圖與發送時間。

感謝你的協助！
```

#### 3) 測試回報格式（建議）

```text
【LINE 測試回報】
- 測試時間：
- 測試帳號暱稱：
- 發送訊息內容：
- 是否收到回覆（是/否）：
- 收到回覆內容：
- 截圖：
```

## 六、常見問題

1. 問：每位 LINE 使用者都要輸入 secret/token 嗎？
   - 答：不用。這是你的官方帳號（Channel）層級設定，伺服器設一次即可，全體使用者共用。

2. 問：為何看到「感謝您的訊息，本帳號無法個別回覆」？
   - 答：通常是 OA 後台自動回應功能仍開啟，請在 OA Manager 關閉。

3. 問：Verify 失敗怎麼辦？
   - 答：先檢查是否指向後端 `8000` 而非前端 `5173`，且 URL 需包含 `/api/line/webhook`。

## 七、LINE 自動模式與 chat.html 路徑差異

### 結論

- 兩者不是完全相同路徑。
- 共用的核心是 `ChatRouter.single_turn`（Agent 單輪回覆能力）。
- 入口層、權限模型、會話模型、輸出協定與營運控制（人工接手）皆不同。

### 差異對照表

| 面向 | chat.html（`/api/chat`） | LINE 自動模式（`/api/line/webhook`） | 為何有差別 |
|---|---|---|---|
| 入口性質 | 站內前端請求 | 第三方 webhook 回呼 | 協定與安全模型不同 |
| 身分驗證 | 系統使用者 JWT + 權限 | `X-Line-Signature` 驗簽 + 後台操作權限 | LINE 使用者不是系統帳號 |
| 會話鍵 | 以 `Conversation` 為主 | 以 `line_user_id -> LineChannelSession -> Conversation` 映射 | 需維持 LINE 使用者持續會話 |
| 路由策略 | 可走 router/worker、多代理協作 | 目前以 session 指派 Agent 走單輪 | webhook 需優先穩定與低延遲 |
| RAG 輸入 | 可帶 `selected_dataset_ids` | webhook 目前無同等輸入欄位 | LINE payload 與 UI payload 不同 |
| 輸出方式 | SSE 串流回前端 | Reply API / Push API | LINE 平台不走 SSE |
| 訊息稽核 | `Message` 與事件紀錄 | `LineMessage` + `Message` 雙寫 | 客服/通道需額外稽核欄位 |
| 人工接手 | 無通道模式欄位 | `mode=bot/human` | LINE 客服營運需求 |

### 總結

- 這些差異是刻意分層：
  1. **安全**：外部 webhook 必須先驗簽，不能等同站內登入。
  2. **穩定**：LINE 回呼要短流程，避免把高複雜路由直接耦合進 webhook。
  3. **營運**：LINE 需要人工接手、push/reply、通道稽核等站內聊天沒有的控制面。
  4. **可演進**：先以通道最小閉環上線，再逐步評估哪些能力需要對齊 `/api/chat`。
