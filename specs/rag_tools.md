# RAG 與工具（MCP / Skills）協同設計

## 背景

目前聊天流程已可在主 prompt 注入 RAG 檢索內容，但「意圖技能捷徑」是另一條分支，容易在 RAG 命中時誤觸不相關技能（例如把 `MCP server` 當成天氣查詢城市）。

同時，實際需求不只「RAG 或工具二選一」，還包含「先搜尋再用工具產出」場景（例如先找 `RASA`，再用 docx 技能產報表）。

## 目標

1. 支援 RAG 與工具的組合流程，而非互斥。
2. 工具範圍必須包含：
   - 系統內建工具（tool call）
   - MCP 工具（`mcp:<name>`）
   - Skills（含 prompt/hybrid/executable）
3. 避免意圖捷徑在 RAG 命中情況下誤判。
4. 保留可觀測性（log / 事件）與可回歸測試性。

## 核心原則

1. **單一協調決策**：由同一個協調器輸出執行模式，不再讓 RAG 與技能分支各自判斷。
2. **模式化執行**：至少支援四種模式。
3. **證據可攜**：若是 `rag_then_tool`，RAG 命中片段要可被工具直接消費。
4. **可審計**：每次決策都要有 reason 與關鍵上下文（dataset ids、rag hits、tool 名稱）。

## 決策模式

協調器輸出 JSON：

```json
{
  "mode": "rag_only | tool_only | rag_then_tool | tool_then_rag",
  "tool_name": "",
  "tool_args": {},
  "reason": ""
}
```

- `rag_only`：僅用 RAG 上下文回答。
- `tool_only`：不依賴 RAG，直接呼叫工具。
- `rag_then_tool`：先 RAG 萃取，再把結果灌入工具 payload（報表/轉檔/摘要等）。
- `tool_then_rag`：先工具取外部資料，再補 RAG 比對或引用。

## 工具範圍定義

### 1) 系統內建工具

- 以既有 tool call 協定呼叫。
- 必須通過 schema required 欄位驗證。

### 2) MCP 工具

- 工具名稱格式：`mcp:<connection_name>`。
- 支援 stdio / ws 兩種 transport。
- 需保留 timeout、progress、錯誤回退機制。

### 3) Skills

- 包含 `prompt`、`hybrid`、`executable` 類型。
- 若為互動式技能，走 `action=start` 流程。
- 若為非互動技能，直接以 schema payload 執行。

## 建議執行流程

1. 正規化輸入（message、selected_dataset_ids、attachments）。
2. 先做 RAG 檢索，得到 `rag_runtime` 與 `rag_context`。
3. 呼叫協調器（LLM）做單次決策，輸出模式 JSON。
4. 依 `mode` 進入對應執行器：
   - `rag_only` -> 直接回答
   - `tool_only` -> 工具執行
   - `rag_then_tool` -> 先建 evidence payload 再執行工具
   - `tool_then_rag` -> 先工具再回到回答階段
5. 產生最終回覆，附必要引用與事件。

## `rag_then_tool` 標準 payload（建議）

為避免每個技能各自解析 RAG 格式，建議統一欄位：

```json
{
  "query": "RASA",
  "summary": "RAG 摘要",
  "evidence": [
    {
      "dataset_id": "...",
      "doc_id": "...",
      "chunk_id": "...",
      "text": "...",
      "score": 0.0,
      "citation": "dataset/doc/chunk"
    }
  ],
  "citations": ["..."]
}
```

> docx / 報表類技能可直接消費 `summary + evidence + citations` 產出文件。

## 防呆與守門規則

1. 若 `selected_dataset_ids` 非空且 `rag_hits > 0`，不得直接套用舊版「意圖捷徑」覆蓋決策。
2. 若使用者語句包含「搜尋/引用/根據資料集」語意，優先允許 `rag_only` 或 `rag_then_tool`。
3. 若工具與問題語意低相關（例如天氣技能 vs `MCP server`），應拒絕 `tool_only`。
4. 工具缺參數時可做參數抽取，但不得繞過模式決策。

## 觀測與事件

建議新增或保留以下 log / 事件：

- `chat.rag.selection_end`（含 hits、selected_dataset_ids）
- `chat.coordinator.decision`（mode、tool_name、reason）
- `chat.intent_skill.shortcut_skipped_due_to_rag`（相容期）
- `chat.tool.payload_built_from_rag`（`rag_then_tool` 專用）
- `chat.tool.execution_result`

## 回歸測試案例

1. **RAG only**：有 dataset + 有 hits + 無工具需求 -> 不呼叫工具。
2. **RAG then tool**：`搜尋 RASA 並產出 docx` -> 呼叫 docx，且 payload 含 evidence。
3. **Tool only**：純工具需求（無資料集語意）-> 正常呼叫工具。
4. **誤判防護**：`MCP server` + 有 RAG hits -> 不得呼叫天氣技能。
5. **MCP 路徑**：`rag_then_tool` 指向 `mcp:*` 工具時，仍保有 timeout/progress/fallback。

## 最小落地策略（建議分兩階段）

### 階段 A：低風險修正

1. 在現行捷徑前加入 RAG guard，避免明顯誤判。
2. 補回歸測試，先止血。

### 階段 B：完整協調器

1. 將「意圖技能捷徑」升級為「模式決策器」。
2. 導入 `rag_then_tool` / `tool_then_rag` 分支。
3. 統一工具 payload 契約與觀測事件。

---

此設計可同時滿足：

- 使用者指定資料集時的可控性
- RAG 與工具的可組合性
- MCP / Skills / 內建工具的一致治理

## 本次已完成修正（2026-07-29）

### 程式修正

1. 在 `backend/src/api/routes/chat.py` 的 `_infer_intent_skill_tool()` 加入 guard：
   - 當 `selected_dataset_ids` 非空且 `rag_runtime.hits > 0` 時，略過 intent shortcut。
   - 保留既有「系統提示明確指定技能」的優先路徑，避免破壞明確指派。
2. 新增觀測 log：
   - `chat.intent_skill.shortcut_skipped_due_to_rag`
   - 欄位包含 `conversation_id`、`agent_id`、`selected_dataset_ids`、`rag_reason`、`rag_hits`。

### 測試修正

1. 新增回歸測試 `backend/tests/unit/test_spec003_integration.py`：
   - `test_chat_stream_skips_intent_shortcut_when_selected_dataset_has_rag_hits`
2. 驗證重點：
   - 即使 `_detect_intent_skill_name` 命中技能，當 RAG 命中且有資料集上下文時，仍不觸發 `tool_start`。
   - 回覆走一般 LLM 串流路徑。

### 驗證命令

```bash
pytest tests/unit/test_spec003_integration.py -q -k "intent_shortcut_when_selected_dataset_has_rag_hits or chat_stream_records_tool_error_status"
```

結果：2 passed。

## 本次增量修正（2026-07-29，第二批）

### 程式修正

1. 新增 `rag_then_tool` 判斷函式：
   - `backend/src/api/routes/chat.py`：`_is_rag_then_tool_request(message)`
   - 用意：當訊息同時包含檢索語意（搜尋/查詢）與產出語意（docx/報表/匯出）時，允許工具分支。
2. 調整 intent shortcut guard：
   - 仍維持「有資料集且 RAG 命中時預設跳過捷徑」
   - 但若判定為 `rag_then_tool` 候選，則不跳過，並記錄 `chat.intent_skill.rag_then_tool_candidate`。
3. 新增 RAG 證據注入器：
   - `backend/src/api/routes/chat.py`：`_inject_rag_evidence_into_payload(...)`
   - 將 `rag_runtime.selected_rows` 轉為標準 `_rag` 物件（`query/summary/evidence/citations`）注入工具 payload。
4. 擴充 `rag_runtime`：
   - `_build_chat_rag_context` 回傳包含 `selected_rows`（無命中時回空陣列）。
5. 兩條工具路徑都會注入 RAG 證據（在符合 `rag_then_tool` 條件時）：
   - intent shortcut 呼叫路徑
   - LLM 偵測 `[[CALL tool=...]]` 的一般工具路徑

### 新增觀測事件

- `chat.intent_skill.rag_then_tool_candidate`
- `chat.tool.payload_built_from_rag`

### 測試修正

1. 保留前一批測試：
   - `test_chat_stream_skips_intent_shortcut_when_selected_dataset_has_rag_hits`
2. 新增複合流程測試：
   - `test_chat_stream_rag_then_tool_injects_evidence_payload`
   - 驗證在「先搜尋再產出 docx 報表」情境下，工具 payload 含 `_rag.evidence`。

### 驗證命令

```bash
pytest tests/unit/test_spec003_integration.py -q -k "intent_shortcut_when_selected_dataset_has_rag_hits or rag_then_tool_injects_evidence_payload or chat_stream_records_tool_error_status"
```

結果：3 passed。

## 問題追查紀錄（2026-07-30）

### 從 log 定位到的實際問題

以 `/tmp/dyagent-backend.log` 的同一輪請求為例：

1. 已確認資料集有正確帶入且命中：
   - `chat.stream.selected_datasets` 有 `mis_pb` id
   - `chat.rag.collection_search` 命中 `hit_count=5`
   - `chat.rag.selection_end reason=ok selected_rows=5`
2. 但主 prompt 同時含有過往錯誤對話歷史（先前回覆「無法存取資料集」），導致模型被歷史內容拉偏。
3. 代理能力前綴中的 `agent_ctx.rag.enabled=False`（代理預設設定）與本輪實際 `rag_runtime.hits>0` 並存，造成語意訊號衝突。

### 本次修正（第三批）

1. 在主 prompt 組裝加入 `RAG Runtime Guard`：
   - 檔案：`backend/src/api/routes/chat.py`
   - 函式：`_build_capability_prompt(..., rag_runtime=...)`
   - 規則：當 `rag_context` 存在且 `rag_runtime.hits > 0` 時，明確要求模型不得宣稱「無法存取資料集／無法搜尋資料集」，必須先依據 RAG 命中內容回答。
2. `chat_stream` 呼叫 `_build_capability_prompt` 時傳入 `rag_runtime`，讓 guard 與實際命中狀態一致。

### 新增測試

新增 `backend/tests/unit/test_spec003_integration.py`：

- `test_chat_stream_injects_rag_runtime_guard_when_hits_exist`
  - 驗證有 RAG hits 時，送往 LLM 的 prompt 必含 `[RAG Runtime Guard]` 與禁止誤稱無法存取的指令。

### 驗證命令

```bash
pytest tests/unit/test_spec003_integration.py -q -k "intent_shortcut_when_selected_dataset_has_rag_hits or rag_then_tool_injects_evidence_payload or injects_rag_runtime_guard_when_hits_exist or chat_stream_records_tool_error_status"
```

結果：4 passed。

## 本次修正（第四批：可讀性與可追溯性）

### 問題

1. 回覆內容雖然有命中資料，但常被模型輸出成單行長文，閱讀性差。
2. 使用者看不到可點擊來源，無法直接開啟原始檔案驗證。

### 後端修正

1. `chat_stream` 在「純 RAG 回覆（未走工具）」結束前，會自動補上 `參考來源` 區塊：
   - 來源來自 `rag_runtime.selected_rows`
   - 若存在 `dataset_id + file_key`，會產生可點擊路徑：
     - `/api/rag/datasets/{dataset_id}/documents/{file_key}/open`
2. `_build_chat_rag_context` 的命中列新增 `file_key`，供引用連結組裝使用。
3. 工具結果引用區塊 `_build_rag_citation_block` 支援 markdown 連結格式：
   - `- [檔名（頁碼）](連結)`

### RAG 文件開啟 API

新增端點：

- `GET /api/rag/datasets/{dataset_id}/documents/{file_key}/open`

行為：

- 驗證資料集與檔案存在
- global normal 資料集允許具 `chat` 或 `read_agent` 權限的使用者存取（admin 亦可）
- 回傳 `FileResponse` 並以 `inline` 呈現，支援另開瀏覽器查看

### 前端修正

1. `chat.html` 新增助理文字美觀化：
   - 自動在 `1. 2. 3.` 編號段落前後補換行
   - 改善單行長文閱讀性
2. 引用區塊支援可點擊連結：
   - 支援 markdown 連結格式 `[label](url)`
   - 支援 `/api/rag/datasets/...` 相對路徑
   - 一律 `target="_blank"` 另開視窗

### 新增測試

1. `backend/tests/unit/test_spec003_integration.py`
   - `test_chat_stream_plain_rag_reply_appends_clickable_citations`
   - 驗證純 RAG 回覆會附上可點擊來源連結
2. `backend/tests/unit/test_rag.py`
   - `test_open_dataset_document_allows_chat_user_for_global_normal`
   - 驗證聊天使用者可開啟 global normal 資料集文件

### 驗證命令

```bash
pytest tests/unit/test_spec003_integration.py -q -k "injects_rag_runtime_guard_when_hits_exist or plain_rag_reply_appends_clickable_citations or rag_then_tool_injects_evidence_payload or intent_shortcut_when_selected_dataset_has_rag_hits"
pytest tests/unit/test_rag.py -q -k "open_dataset_document_allows_chat_user_for_global_normal or list_dataset_documents_contains_file_key"
```

結果：6 passed。

## 本次修正（第五批：引用連結授權）

### 問題

引用來源已顯示為 `/api/rag/.../open` 連結，但使用者直接點擊時，瀏覽器不會自動帶 `Authorization` header，後端回 `FORBIDDEN / Authentication required`。

### 修正

1. 前端 `chat.html` 新增 `openAuthorizedCitationLink(url)`：
   - 以 `localStorage.auth_token` 組 `Authorization: Bearer ...`
   - `fetch` 下載檔案 blob
   - `window.open(blobUrl, '_blank')` 另開頁面查看
2. 在聊天訊息 click handler 攔截 `a.citation-link`：
   - 若連結是 `/api/rag/datasets/...`，改走授權下載流程
   - 其他外部 URL 仍維持原本直接開啟

### 結果

- 使用者點擊引用來源可正常開啟檔案，不再出現 `Authentication required`。

### 補丁（第五批-1）

針對實際回報仍無法開啟的案例（連結為 `http://localhost:5173/api/...`）追加修正：

1. 前端不再只判斷 `href.startsWith('/api/rag/datasets/')`，改為解析 URL 後判斷 pathname。
2. 新增 `resolveCitationRequestUrl()`，會把 `5173` 下的 `/api/...` 轉成與 `main.js` 一致的 API_BASE（開發環境預設 `:8000/api`）。
3. 因此即使 citation 是完整網址 `http://localhost:5173/api/...`，也會走授權 fetch 流程並帶 Bearer token。

### 補丁（第五批-2）

針對仍可能被瀏覽器直接導頁的情況再補強：

1. 內部資料集引用不再渲染成可直接導頁的 `<a href="/api/...">`，改為 `button[data-citation-open-url]`。
2. 點擊按鈕一律走 `openAuthorizedCitationLink()`，先授權 `fetch` 再以 blob 另開視窗。
3. 外部網址仍維持 `<a target="_blank">` 行為。

> 這可避免瀏覽器在某些互動（例如開新分頁、導頁競態）下跳過前端攔截，直接命中未授權 API URL。

### 補丁（第五批-3）

針對「使用者直接把引用 URL 貼到瀏覽器網址列」的情境追加後端容錯：

1. `GET /rag/datasets/{dataset_id}/documents/{file_key}/open` 改為可選驗證：
   - 先讀取 Authorization Bearer
   - 若無 header，允許 `access_token` query 參數
2. 對 `global + enabled + sensitivity=normal` 的資料集，允許匿名開啟文件。
3. `agent_private` 仍維持必須登入與權限檢查。

測試補充：

- `test_open_dataset_document_allows_anonymous_for_global_normal`

### 補丁（第五批-4）

修正 `INTERNAL_ERROR` 根因：

1. 後端 `open_dataset_document` 先前手動設定 `Content-Disposition: inline; filename="中文檔名"`。
2. ASGI header 需可被 latin-1 編碼，中文檔名會觸發 `UnicodeEncodeError`，導致 500。
3. 改為直接回傳 `FileResponse(path, media_type)`，不手動塞中文 `Content-Disposition`。

結果：

- 含中文檔名的來源文件可正常開啟，不再出現 `INTERNAL_ERROR`。

## 本次修正（第六批：DOCX 線上預覽）

### 需求

PDF 可直接開啟，但 DOCX 在多數瀏覽器只會下載，無法即時預覽。

### 修正

1. 新增端點：
   - `GET /api/rag/datasets/{dataset_id}/documents/{file_key}/open-preview`
2. 行為：
   - `.pdf`：維持原本檔案開啟（FileResponse）
   - `.docx` 等文件：透過 `convert_document_to_markdown` 轉為可讀內容，回傳 HTML 預覽頁
3. 權限：
   - 與 `/open` 共用同一套驗證邏輯（含 global normal 匿名可讀）
4. 聊天引用連結調整：
   - 若來源檔名為 `.docx`，聊天引用自動指向 `/open-preview`
   - 其他格式仍使用 `/open`
5. 前端引用 URL 判斷：
   - `isDatasetCitationApiUrl` 同時接受 `/open` 與 `/open-preview`

### 測試

1. `test_chat_stream_plain_rag_reply_appends_clickable_citations`
   - 驗證 docx 引用路徑改為 `/open-preview`
2. `test_open_dataset_document_preview_renders_docx_as_html`
   - 驗證 DOCX 可回傳 HTML 預覽內容

驗證結果：

- `test_spec003_integration`（1 selected）passed
- `test_rag`（3 selected）passed

## 本次調整（第六批-1：引用路徑回復）

依實際使用回饋，`open-preview` 會遺失部分 docx 圖片與版面資訊，故聊天引用路徑改回統一使用：

- `/api/rag/datasets/{dataset_id}/documents/{file_key}/open`

說明：

- 保留 `open-preview` 端點做備援，但聊天預設不再導向該端點。
- 這樣可優先維持原始文件內容完整度（交由本機 Office 或瀏覽器下載開啟）。
