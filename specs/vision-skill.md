# HTML 視覺互動技能規格（Vision Skill）

## 1. 目的

定義 dyagent 內「以 HTML 畫面互動」的技能規格，讓技能可以在 chat 流程中彈出多步驟表單、收集資料，並由技能腳本完成最終處理（例如送單、建立申請、呼叫外部 API）。

本規格聚焦：

1. 前端與技能 UI 的通訊協定
2. 後端與技能執行的回傳模式
3. 多步驟互動狀態管理
4. 安全與資源載入約束
5. 開發者可重複套用的 ZIP 結構

---

## 2. 核心原則

1. **單一工具入口**：不為每個技能建立專屬 API。
2. **技能主導流程**：步驟推進由技能腳本回傳 `mode` 決定。
3. **前端僅橋接**：前端只負責 UI 載入、事件轉送與結果呈現。
4. **安全預設**：採 `iframe sandbox`，限制資源來源與存取範圍。
5. **不中斷對話**：即使流程錯誤，也保留使用者可見訊息與錯誤內容。

---

## 3. 名詞定義

1. **Skill ZIP**：技能壓縮包，包含腳本與 UI 資源。
2. **interaction_id**：一次技能互動流程的唯一識別（後端持久化）。
3. **mode**：技能腳本對當前流程回傳的狀態（`ui/final/error`）。
4. **channel_nonce**：iframe 與主頁訊息通道驗證值。

---

## 4. 技能 ZIP 結構

建議結構：

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

規範：

1. `scripts/main.js` 負責流程判斷與回傳 `mode`。
2. `ui/index.html` 為技能主畫面入口。
3. `ui/main.js` 與父頁使用 `postMessage` 溝通。
4. 不允許依賴外部 CDN（預設安全策略）。

---

## 5. 後端工具呼叫協定

API：

- `POST /api/conversations/{conversation_id}/tools/{tool_name}`

請求 payload（建議）：

```json
{
  "action": "start | submit | back | cancel | resume | heartbeat",
  "interaction_id": "uuid",
  "step": "step-1",
  "form_data": {}
}
```

`action` 說明：

1. `start`：啟動互動流程
2. `submit`：提交當前步驟資料
3. `back`：返回上一步
4. `cancel`：取消流程
5. `resume`：重建當前畫面
6. `heartbeat`：保活（可選）

---

## 6. 技能腳本回傳規格

技能腳本（`scripts/main.js`）需回傳以下模式之一：

### 6.1 `mode = "ui"`

用途：要求前端顯示或更新互動畫面。

```json
{
  "mode": "ui",
  "step": "step-1",
  "ui": {
    "entry": "ui/index.html",
    "title": "員工請假申請",
    "state": {}
  }
}
```

### 6.2 `mode = "final"`

用途：流程完成，回傳最終結果與使用者訊息。

```json
{
  "mode": "final",
  "step": "done",
  "output": {},
  "assistant_message": "已送出申請..."
}
```

### 6.3 `mode = "error"`

用途：流程失敗或使用者取消。

```json
{
  "mode": "error",
  "step": "step-1",
  "error": "錯誤訊息"
}
```

---

## 7. 前端與 iframe 通訊

### 7.1 子頁 -> 主頁

1. `skill.ready`
2. `skill.submit`
3. `skill.back`
4. `skill.cancel`
5. `skill.heartbeat`
6. `skill.resize`

### 7.2 主頁 -> 子頁

1. `host.init`
2. `host.update`
3. `host.error`
4. `host.close`

### 7.3 驗證要求

1. 驗證 `event.source === iframe.contentWindow`
2. 驗證 `channel_nonce` 一致
3. 驗證 `interaction_id` 合法

---

## 8. UI 資源載入規範

資源路由：

- `GET /api/skills/ui/{interaction_id}/{asset_path}?token=...`

規則：

1. 僅允許讀取 `ui/` 目錄資源
2. token 與 `interaction_id/tool_name/conversation_id` 必須一致
3. 支援 ZIP 外層包一層資料夾的解析（`*/ui/index.html`）
4. HTML 回傳時需確保子資源可帶 token（避免 script/css 請求被拒）

---

## 9. 多步驟狀態

後端以 `skill_interactions` 管理狀態，至少包含：

1. `interaction_id`
2. `conversation_id`
3. `tool_name`
4. `status`（active/completed/cancelled/expired/failed）
5. `current_step`
6. `state_json`
7. `expires_at`

狀態原則：

1. `mode=ui`：維持 active，更新 step/state
2. `mode=final`：標記 completed
3. `mode=error`：標記 failed（或 cancel 對應 cancelled）

---

## 10. 錯誤處理

1. 後端錯誤應回傳可理解文字給前端。
2. 前端不可清空已可見回覆（即使後續失敗）。
3. 技能回 `mode=error` 時，仍保留 chat 可讀結果。
4. 路由不可用、工具不允許、token 無效等錯誤都應可追蹤。

---

## 11. 分派與技能關係

1. 主代理先決定由哪個代理處理（tasked/public/master）。
2. 命中技能後由該代理執行工具。
3. 多步驟提交沿用同一工具 API，不需新增專屬 endpoint。
4. 若分派到錯誤代理，流程可能無法命中對應技能。

---

## 12. 驗證清單（上線前）

1. `start -> ui(step-1)` 正常
2. `submit(step-1) -> ui(step-2)` 正常
3. `submit(step-2) -> final` 且有 `assistant_message`
4. `back` 可返回並保留已填資料
5. `cancel` 可終止流程並回傳明確結果
6. iframe 重新整理後可 `resume`
7. 技能完成後訊息能持久保留於對話歷史

---

## 13. 版本與擴充建議

1. 後續可將 skill schema 與 UI 分離（JSON schema + HTML renderer）
2. 可加入權限等級（僅特定角色可執行某些技能）
3. 可加入互動追蹤事件（step 轉換、耗時、失敗點）

---

## 14. 本規格適用範圍

適用於目前 dyagent 的 HTML 互動技能實作（例如 `leave-request`, `expense-request`），並可作為未來所有畫面型技能的共同基準。