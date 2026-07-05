# Quickstart: Per-Agent Integrations

## 目的

快速驗證「每代理者 MCP/Skills/RAG 設定可獨立生效」，以及「新增 `SKILL.md` 後不改 router 程式碼即可命中」。

## 前置條件

- 已完成後端環境安裝並可執行 `pytest`
- 測試資料庫可用
- 專案根目錄為 `dyagent`

## 步驟 1：驗證動態 Skill 路由（單元 + 整合）

在專案根目錄執行：

```bash
pytest backend/tests/unit/test_skill_registry.py backend/tests/integration/test_skill_router_dynamic_discovery.py
```

預期結果：

- 全部測試通過
- `test_skill_router_dynamic_discovery` 通過，表示新增技能後不需修改 router 程式碼

## 步驟 2：驗證執行期資料來源優先權

規則：

- `name`、`description`、`prompt_template` 以 DB 欄位為準
- ZIP 僅提供執行資產（`scripts/`、`ui/`）
- `prompt_template` 不再由 ZIP 內 `SKILL.md` 自動覆蓋

人工檢查重點：

1. 在技能管理頁上傳 ZIP 後，修改技能名稱/說明/提示詞模板
2. 重新執行該技能
3. 確認回應使用的是 DB 最新內容，而不是 ZIP 內舊 `SKILL.md`

## 步驟 3：驗證 UI 技能解壓時機

概念：

- `extract_skill_zip`：執行技能時使用
- `extract_skill_ui_dir`：只有技能回傳 `mode=ui` 且前端請求 `/api/skills/ui/...` 時才使用

人工檢查重點：

1. 使用 `expense-request-html` 或 `leave-request-html` 觸發 `mode=ui`
2. 確認前端開啟 iframe，並向 `/api/skills/ui/{interaction_id}/{asset_path}` 取資源
3. 同一技能非 UI 模式不會走 UI asset 路由

## 步驟 4：驗證每代理者能力隔離

建議建立兩個代理者：

- Agent A：啟用 RAG + 指定 Skill
- Agent B：停用 RAG + 不綁定該 Skill

用同一段提問測試兩者，預期：

- Agent A 出現對應能力行為（檢索/技能回覆）
- Agent B 不出現該能力行為

## 常見問題

- 測試找不到技能：檢查 `SKILL_REGISTRY_PATHS` 是否包含技能目錄
- 技能有 ZIP 但不出現 UI：檢查腳本輸出是否為 `mode=ui`
- 技能回覆不是最新文案：檢查 DB `prompt_template` 是否已更新
