# Research: Per-Agent MCP/Skills/RAG Config

## 決策：Skill 路由採「SKILL.md 單一真相 + 動態解析」

### 背景

過去做法傾向先將 `SKILL.md` 轉成 JSON 後再路由與執行，但在多來源（docx/pdf/手寫）技能下，
會出現欄位遺失、語意扁平化、更新不同步等問題。

### 核心原則

- `SKILL.md` 是能力定義的唯一真相（Source of Truth）。
- 路由階段只建立「最小 manifest」供匹配，不覆寫原始內容。
- 執行階段以 DB 可編輯欄位為準：`name`、`description`、`prompt_template`。
- ZIP 僅作為可執行資產（`scripts/`、`ui/`）與匯入初值來源。

### 方案

1. 動態掃描
   - 從 `SKILL_REGISTRY_PATHS` 指定目錄遞迴掃描 `SKILL.md`。
   - 預設值：`tools,.opencode/skills`。

2. 最小解析（manifest）
   - 解析欄位：`name`、`description`、`triggers`、`routes`、`constraints`。
   - 僅做路由所需萃取，不做重語意轉換。

3. 快取策略
   - 檔案模式：以 `mtime_ns + size` 判斷是否失效。
   - 文字模式：以 `sha256` 作為內容快取 key。

4. 規則路由
   - 路由計分來源：name/trigger/route-intent/description。
   - 權重與格式提示可由設定調整（避免硬編碼到程式）。
   - 支援動態新增技能：新增 `SKILL.md` 後不需改 router 程式碼。

### 設定化項目

以下常數透過 `backend/src/core/config.py` 管理：

- `SKILL_ROUTER_NAME_WEIGHT`
- `SKILL_ROUTER_TRIGGER_WEIGHT`
- `SKILL_ROUTER_ROUTE_INTENT_WEIGHT`
- `SKILL_ROUTER_DESCRIPTION_WEIGHT`
- `SKILL_ROUTER_FORMAT_BOOST`
- `SKILL_ROUTER_MAX_TOKEN_SCORE_LEN`
- `SKILL_ROUTER_HUMANIZER_HINTS`
- `SKILL_ROUTER_PDF_HINTS`
- `SKILL_ROUTER_DOCX_HINTS`

### 資料來源優先權（執行期）

- `name`、`description`、`prompt_template`：以 DB 欄位為準。
- `SKILL.md`：作為匯入初值與路由解析來源。
- `zip_bundle`：作為 `scripts/ui` 執行資產。

### 風險與緩解

- 風險：技能文字含大量雜訊可能導致誤配。
  - 緩解：停用詞、token 最小長度、格式 boost、fixture 回歸測試。
- 風險：DB 與 ZIP 內容不同步。
  - 緩解：明確來源優先權，執行時不以 ZIP 覆蓋 DB prompt。

### 驗證基準

- 路由 fixture 至少 20 筆，準確率 >= 90%。
- 動態新增技能整合測試：不改 router 程式碼即可命中。
