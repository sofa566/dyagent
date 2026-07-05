# 主代理分派機制說明（Master Dispatch）

本文整理目前 dyagent 的主代理分派流程（`backend/src/api/routes/chat.py`），說明「主代理如何決定由誰回覆」、為什麼會 fallback，以及與技能互動流程的關係。

## 1. 入口與流程總覽

聊天入口是：

- `POST /api/chat`

主流程（概念）如下：

1. 讀取使用者訊息與目前 router agent（`is_router=true`，若無則退到第一個 enabled agent）。
2. 先做單/多代理分類（`_classify_routing`）。
3. `multi`：走 multi-agent orchestrator。
4. `single`：走 `_pick_worker_agent(...)` 挑選目標代理。
5. 產生 `route.decision` 事件，實際轉送到 `chat_stream(...)`。

## 2. 單代理分派（重點）

主要函式：

- `_pick_worker_agent(db, router_agent, message)`

目前策略順序：

1. **顯式點名優先**
   - `_pick_explicit_named_worker(...)`
   - 若使用者明確提到代理名稱，直接命中。

2. **humanizer 特例優先（public）**
   - `_should_prefer_humanizer_public(...)`
   - 若訊息語意偏潤稿/口語化，優先找綁 `humanizer-zh-tw` 的 public 代理。

3. **技能意圖導向（動態）**
   - `_detect_intent_skill_name(...)`
   - 從候選代理綁定技能的 `name + description` 抽 token，與使用者訊息比對。
   - 命中後，優先回傳擁有該技能的代理（可能是 tasked 或 public）。

4. **tasked 群組嘗試**
   - `_pick_worker_without_default(workers=tasked_workers, ...)`
   - 規則匹配 -> embedding ->（非短訊息才）LLM judge。

5. **public 群組嘗試**
   - `_pick_worker_without_default(workers=public_workers, ...)`

6. **最後 fallback**
   - public 有人則 `public_default_fallback`
   - 否則 tasked 有人則 `tasked_default_fallback`
   - 否則 `worker_not_found`

> 註：先前曾有 `public_short_message_prefer`（短訊息直接偏 public），目前已移除，以避免「我想請假」被過早分派給客服 public 代理。

## 3. readiness gate（為何會回主代理）

即使挑到某代理，還會過一層可用性檢查：

- `_is_agent_ready_for_chat(db, agent)`

判斷重點：

- `tier=cloud`：至少要有 `provider + model`
- `tier=onprem`：至少要有 `onprem_provider + onprem_base_url + model`
- 未滿足則視為 not ready

若 not ready：

- 在單代理路徑會回退到 master，reason 類似：
  - `tasked_not_ready_master_fallback`
  - `public_not_ready_master_fallback`

這是你看到「應該 HR 回覆卻變主代理」最常見原因。

## 4. 多代理路徑（摘要）

當 `_classify_routing` 判定為 `multi`：

1. 先嘗試 decompose 任務。
2. 若 decompose 成功且任務數 >= 2，進 orchestrator。
3. 若 decompose 失敗，會降級回單代理挑選器（同樣會寫 route reason）。

## 5. 路由事件與前端顯示

主要事件：

- `route.decision`
- `route.forward`
- `route.fallback`

前端 chat 會根據 `route.decision.reason` 顯示：

- 本輪由：`{target_agent_name}` 代理者回覆（`{reason}`）

因此若要診斷分派錯誤，優先看 `route.decision.reason`。

## 6. 技能互動流程如何介入分派

技能互動是兩段式：

1. 初次聊天（`/api/chat`）由主代理分派到某 worker。
2. worker 內部若命中技能，輸出 `tool` / `skill_ui_open` 等事件。
3. 使用者在 iframe 送出後，走固定 API：
   - `POST /api/conversations/{conversation_id}/tools/{tool_name}`

注意：

- 第 2 段（tools API）不再重新做主代理分派；它依 conversation 對應 agent 執行。

## 7. 為何會發生「分派到客服代理」

常見原因按機率排序：

1. **HR 代理 not ready**（LLM 設定缺關鍵欄位）
2. HR 代理未 enabled 或 agent_class 不正確
3. HR 代理未綁對技能（或技能 disabled）
4. 技能描述 token 與使用者語句匹配度低，導致未命中 skill intent
5. 最終走到 public/tasked default fallback

## 8. 建議檢查清單（針對 HR）

請逐項確認：

1. HR 代理：`enabled=true`
2. HR 代理：`agent_class=tasked`
3. HR 代理 `model_config` 完整（cloud: provider+model；或 onprem 三件套）
4. HR 代理 integrations 已綁 `leave-request`
5. `leave-request` 技能本身 `enabled=true`
6. `leave-request.description` 明確包含「請假、特休、排班、人資」等詞（提高動態 token 命中率）

## 9. 現行已知設計原則

1. 不在分派核心硬編特定技能名稱與詞（已改為動態 token）
2. 分派結果可觀測（route events）
3. 代理 not ready 時寧可回主代理，不讓使用者直接拿到 no_route 錯誤
4. 技能表單流程與分派流程分離（初次分派與後續 tools call 分離）

## 10. 後續可優化方向

1. 在 `route.decision` payload 補上「被淘汰代理與原因」清單（可直接看出 HR 為何被踢掉）
2. 在代理列表頁加 `ready/not-ready` 即時燈號
3. 在技能描述抽詞加可配置 stopwords 與權重
4. 當使用者語句命中技能意圖時，若命中代理 not ready，回覆可解釋原因（避免體感像亂分派）
