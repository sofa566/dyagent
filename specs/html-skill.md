# HTML 技能互動方案（B 方案，補強版）

## 1. 背景與目標

目前 dyagent 的技能執行流程以文字與工具事件為主，尚未支援技能在聊天流程中直接顯示互動畫面。
本方案採用 **B 方案：技能 ZIP 內自行提供 HTML + JS**，讓外部系統開發者可用熟悉技術快速開發。

本方案目標：

1. 使用者在 chat 輸入需求（例如「我想請假」）後，代理可觸發技能 UI。
2. UI 表單資料由技能接住並推進多步驟流程。
3. 不為每個技能新增專屬 API，維持單一通用 API。
4. 預設安全策略採最嚴格：**不允許外部 CDN**。
5. 補齊多步驟狀態持久化、LLM 閉環、錯誤恢復與安全驗證。

## 2. 非目標（本階段不做）

1. 不在主頁 DOM 直接注入第三方 HTML。
2. 不開放技能 UI 直接存取主頁 token 或 cookie。
3. 不開放技能 UI 載入任意外部 script/style/connect。
4. 不在此階段提供完整視覺化技能開發 IDE。

## 3. 核心設計

### 3.1 單一通用 API（不新增每技能 API）

沿用既有 API：

- `POST /api/conversations/{conversation_id}/tools/{tool_name}`

統一 envelope：

```json
{
  "action": "start | submit | back | cancel | resume | heartbeat",
  "interaction_id": "可空；start 後由後端建立",
  "step": "字串或數字（可選）",
  "form_data": {},
  "client_meta": {
    "ui_session_id": "前端一次開窗會話 id",
    "message_seq": 1
  }
}
```

說明：

1. `back` 用於多步驟返回上一步。
2. `resume` 用於刷新/重連後恢復當前互動。
3. `heartbeat` 用於延長互動有效期，避免填表過久過期。

### 3.2 多步驟狀態持久化（關鍵）

因 executable skill 每次呼叫都是新 subprocess，**狀態不得放在腳本記憶體**。

採「DB 為準、Redis 為快取」：

1. 新增 `skill_interactions`（PostgreSQL，權威資料）
   - `id`（interaction_id）
   - `conversation_id`
   - `tool_name`
   - `skill_id`
   - `status`（active/completed/cancelled/expired/failed）
   - `current_step`
   - `state_json`（技能流程狀態）
   - `ui_session_nonce`（postMessage 驗證 nonce）
   - `expires_at`
   - `created_at` / `updated_at`
2. Redis 快取 key：`skill:interaction:{interaction_id}`
   - 存放 `state_json + current_step + expires_at`
   - TTL 與 `expires_at` 同步
3. 流程規則
   - `start`：建立 interaction，寫 DB + Redis
   - `submit/back`：讀取 interaction 狀態，更新後再寫回
   - `resume`：回傳當前 step 與 UI state
   - `cancel`：狀態改 cancelled，清 Redis

### 3.3 技能回傳模式

技能（ZIP scripts 或 webhook 回應）需回傳以下模式之一：

- `mode: "ui"`：請前端開啟/更新畫面
- `mode: "final"`：流程完成
- `mode: "error"`：流程錯誤

建議回傳格式：

```json
{
  "ok": true,
  "result": {
    "mode": "ui",
    "interaction_id": "9d5f...",
    "step": "step-1",
    "ui": {
      "entry": "ui/index.html",
      "title": "員工請假申請",
      "state": {
        "employeeName": "王小明"
      }
    }
  }
}
```

`mode=final` 建議格式：

```json
{
  "ok": true,
  "result": {
    "mode": "final",
    "interaction_id": "9d5f...",
    "output": {
      "request_id": "L001",
      "status": "pending_review"
    },
    "assistant_message": "已送出請假申請，單號 L001，目前待主管審核。"
  }
}
```

### 3.4 LLM 閉環（mode=final 如何回到對話）

`mode=final` 的閉環規則：

1. 後端先發 `skill_ui_close` 給前端（關閉 iframe）。
2. 後端將 `assistant_message`（若有）視為工具可讀觀察結果。
3. 聊天串流沿用既有「工具結果 -> 回覆」流程：
   - 有 `assistant_message`：可直接輸出給使用者（優先）
   - 無 `assistant_message`：把 `output` 丟入既有 `_observe_and_answer` 交給 LLM 生成回覆
4. 最後發 `done`，並寫入 conversation/message/event。

結論：**不需要主頁再額外打一支新 API 才能讓 LLM 回覆**。

### 3.5 主頁角色澄清（橋接但不承擔業務決策）

主頁因安全策略（iframe 無直接 API 權限）必須做 API 橋接，這不等同介入業務邏輯。

主頁責任：

1. 接收 iframe 的 `submit/back/cancel`。
2. 原封不動呼叫通用工具 API。
3. 將後端回應轉發給 iframe。

主頁不做：

1. 不判斷欄位商業規則。
2. 不決定流程下一步。
3. 不改寫技能 state。

## 4. 通訊協定

### 4.1 SSE 事件（後端 -> chat 前端）

新增事件型別：

- `skill_ui_open`
- `skill_ui_patch`
- `skill_ui_close`
- `skill_ui_error`

`skill_ui_open`：

```json
{
  "type": "skill_ui_open",
  "tool": "leave-request",
  "interaction_id": "9d5f...",
  "title": "員工請假申請",
  "ui_url": "/api/skills/ui/serve?token=...",
  "state": {},
  "step": "step-1"
}
```

`skill_ui_patch` 用於同一 iframe 更新狀態（不重開窗）。

### 4.2 postMessage（iframe <-> 主頁）

子頁送出：

- `skill.ready`
- `skill.submit`
- `skill.back`
- `skill.cancel`
- `skill.resize`
- `skill.heartbeat`

主頁回覆：

- `host.init`
- `host.update`
- `host.error`
- `host.close`

`skill.submit` 範例：

```json
{
  "type": "skill.submit",
  "interaction_id": "9d5f...",
  "step": "step-1",
  "form_data": {
    "leaveType": "annual"
  }
}
```

### 4.3 postMessage 驗證（必做）

由於 sandbox iframe 可能是 opaque origin（`event.origin = "null"`），不可只靠 origin。

主頁驗證順序：

1. `event.source === iframe.contentWindow`
2. payload 需帶 `interaction_id`
3. payload 需帶 `channel_nonce`
4. `channel_nonce` 必須與 `skill_interactions.ui_session_nonce` 一致
5. 逾時/狀態非 active 一律拒絕

若 iframe 非 sandbox opaque（未來調整策略），再加上 origin 白名單比對。

## 5. Token 與憑證規格

### 5.1 UI 資源 token

- 型式：HMAC-SHA256 簽章 token（非使用者 JWT）
- Signing Key：`SKILL_UI_TOKEN_SECRET`（由後端環境變數管理，至少 32 bytes）
- Payload：
  - `conversation_id`
  - `tool_name`
  - `interaction_id`
  - `entry`
  - `exp`
  - `nonce`
- 預設有效期：10 分鐘

注意：`interaction_id` 與 token 不同。

- `interaction_id`：流程實體 id（持久化狀態主鍵）
- token：短效授權憑證（只保護 UI 檔案讀取）

### 5.2 Token 續期

1. UI 檔案請求 401/403 時，主頁以 `action=resume` 重新取得 `ui_url`（新 token）。
2. 不允許 iframe 自行刷新 token。

## 6. ZIP 結構與解壓策略

### 6.1 ZIP 結構

```text
my-skill.zip
├─ SKILL.md
├─ scripts/
│  └─ main.js
└─ ui/
   ├─ index.html
   ├─ main.js
   ├─ styles.css
   └─ assets/
```

### 6.2 解壓與快取

1. 技能上傳後保留 zip_bundle（DB）。
2. 首次 UI 請求時解壓到快取目錄：
   - `DATA_CACHE_PATH/skill-ui/{skill_id}/{zip_sha256}/`
3. 後續同版 ZIP 直接讀快取，不重複解壓。
4. 清理策略：
   - LRU + TTL（預設 24 小時未使用清除）
   - 發布新 ZIP（sha 改變）自動走新目錄

## 7. 錯誤恢復與生命週期

| 情境 | 行為 |
|---|---|
| Token 過期、使用者仍在填表 | 主頁呼叫 `resume` 取新 `ui_url`，iframe 無需清空資料 |
| 網路斷線重連 | chat 載入後若發現 active interaction，主頁自動 `resume` |
| 腳本崩潰（exception） | 後端回 `mode=error`，並標記 interaction `failed` |
| 使用者直接關閉 Modal | 主頁先送 `cancel`，若失敗仍本地關閉並提示可 `resume` |
| 使用者刷新頁面 | 從 conversations/events + interaction 狀態判斷是否恢復 UI |

互動逾時：

1. interaction 超過 `INTERACTION_TTL_MINUTES` 未 heartbeat，狀態轉 `expired`。
2. expired 後 submit/back 一律回錯，需重新 `start`。

## 8. 並行、歷史與裝置策略

### 8.1 並行多技能 UI

同一個 conversation 同時只允許一個 active skill UI。

1. 若第二個技能要求開 UI，採 FIFO 佇列。
2. 前端提示「目前有進行中的技能流程」。
3. 使用者可選擇先取消目前流程再開下一個。

### 8.2 歷史訊息重現

1. `mode=final` 結果會寫入 message 與 event，聊天歷史可重現。
2. `mode=ui` 中間畫面不做像素級重播；只保留流程狀態與步驟摘要（可 audit）。

### 8.3 Mobile

1. Modal 在行動裝置改全螢幕。
2. iframe 高度由 `skill.resize` 控制，主頁提供最小/最大高度限制。

## 9. webhook skill 與 executable skill 統一

本方案不只支援 executable。

1. executable skill：由 `scripts/main.js` 回傳 `mode=ui/final/error`。
2. webhook skill：外部 endpoint 也可回傳同結構模式。
3. chat/router 以相同模式處理，前端無需區分技能型別。

## 10. 本地開發模式（Dev Mode）

為避免每次都打包 ZIP，提供開發模式（僅 admin、非 production）：

1. 可將本地資料夾掛成暫時 skill source。
2. API 讀取 `ui/*` 時直接指向資料夾。
3. production 強制關閉 dev mode。

## 11. 安全策略（最安全預設）

1. `iframe sandbox="allow-scripts allow-forms"`。
2. 嚴禁外部 CDN 與跨網域資源載入。
3. UI 路由僅允許 `ui/` 子目錄並做 path traversal 防護。
4. UI token 短效、單 interaction 綁定、可撤銷。
5. postMessage 必做 `source + nonce + interaction` 三重驗證。
6. 技能 UI 不可直接觸碰主頁認證資訊。

## 12. 需修改的程式區域

### 12.1 後端

1. `backend/src/services/chat_router.py`
   - 支援技能結果模式 `ui/final/error`。
   - 加入 interaction 狀態讀寫。
2. `backend/src/api/routes/chat.py`
   - SSE 增加 `skill_ui_open/patch/close/error`。
   - `mode=final` 併入既有回覆閉環。
3. 新增 `backend/src/api/routes/skill_ui.py`
   - 提供 UI 檔案安全讀取（token 驗證）。
4. `backend/src/api/main.py`
   - 註冊 skill_ui 路由。
5. 新增 `backend/src/models/skill_interaction.py`（或整合至現有 models）
   - interaction 持久化模型。
6. `backend/src/core/config.py`
   - `SKILL_UI_TOKEN_SECRET`、`SKILL_UI_TOKEN_TTL_SEC`、`SKILL_INTERACTION_TTL_SEC`。

### 12.2 前端

1. `frontend/src/pages/chat.html`
   - 新增 skill Modal + iframe 容器。
   - SSE 新事件處理。
   - postMessage 橋接（submit/back/cancel/resume）。
   - 斷線重連與刷新恢復邏輯。

## 13. 驗證與測試

### 13.1 後端測試

1. `start -> submit -> back -> submit -> final` 流程可重入。
2. subprocess 重啟不影響 interaction 狀態。
3. `mode=final` 會正確產生使用者可讀回覆。
4. token 過期、nonce 不符、path 越界會被拒絕。
5. webhook/executable 兩種技能都可回 `mode=ui`。

### 13.2 前端驗證

1. 請假技能：開 UI -> 填寫 -> 送出 -> 完成。
2. 返回上一步（back）正常。
3. Token 過期自動續期恢復。
4. 刷新頁面可 resume。
5. 手機版 Modal/iframe 正常可用。

## 14. 里程碑

1. M1：協定定稿（action、mode、SSE、postMessage、安全驗證）
2. M2：interaction 持久化（DB+Redis）與 token 機制
3. M3：UI 檔案路由與 ZIP 快取解壓
4. M4：chat 前端 iframe 容器與橋接
5. M5：請假技能 ZIP 驗證（含 mock API）
6. M6：請款技能 ZIP 複用驗證（零新增 API）

## 15. 目前狀態

- 本文件已補齊關鍵缺漏：
  - 多步驟狀態持久化
  - `mode=final` 回到 LLM 的閉環
  - postMessage 驗證
  - token 細節
  - 錯誤恢復
  - `back` action

## 16. 實作階段切分（執行版）

### Phase 1：互動協定骨架（已開始）

1. 後端 chat 串流加入 `skill_ui_open/close/error` 事件輸出。
2. 後端支援 `mode=ui/final/error` 的統一判斷入口。
3. 前端先接住新事件，至少可觀測與顯示流程狀態。

### Phase 2：interaction 持久化

1. 建立 `skill_interactions` 資料表與 repository。
2. 實作 `start/submit/back/cancel/resume/heartbeat` 狀態遷移。
3. 串接 Redis 快取與 TTL。

### Phase 3：UI 資源與 token 安全

1. 新增 `skill_ui` 路由，提供 ZIP `ui/*` 安全讀取。
2. 建立 HMAC token 產生與驗證。
3. 補齊 path traversal 防護與過期處理。

### Phase 4：前端 iframe 容器與橋接

1. Chat Modal + iframe sandbox。
2. postMessage 驗證（source + nonce + interaction）。
3. `submit/back/cancel/resume` API 橋接。

### Phase 5：驗證與樣板技能

1. 建立請假技能 ZIP（含 mock API）。
2. 建立請款技能 ZIP 驗證複用。
3. 補齊測試案例與操作文件。

## 17. 實作進度（2026-04-13）

### 已完成

1. Phase 1：聊天串流支援 `mode=ui/final/error` 對應事件。
2. Phase 2：新增 `skill_interactions` 持久化模型與服務層。
3. Phase 3：新增 HMAC UI token 與 `skills/ui` 安全資源路由。
4. Phase 4：前端 chat 已加入 iframe modal 與 postMessage 橋接。
5. Phase 5（部分）：新增單元測試與請假 HTML 技能樣板。

### 已新增範例

1. `examples/html-skill-packages/leave-request-html-skill`
2. `examples/html-skill-packages/expense-request-html-skill`
3. `examples/html-skill-packages/README.md`

### 已完成測試

```bash
pytest backend/tests/unit/test_chat_router_skill_mode.py \
  backend/tests/unit/test_skill_ui_token_service.py \
  backend/tests/unit/test_skill_interaction_service.py \
  backend/tests/unit/test_chat.py -q
```

結果：11 passed。

### 端到端驗證步驟（手動）

1. 建立技能：`leave-request`、`expense-request`，上傳對應 ZIP。
2. 將兩個技能掛到同一聊天代理者。
3. 在 chat 輸入「我想請假」或「我要請款」。
4. 確認收到 `skill_ui_open` 並彈出 iframe modal。
5. step-1 提交後進 step-2，step-2 提交後收到 `mode=final`。
6. 聊天區顯示最終 assistant 訊息與單號。
