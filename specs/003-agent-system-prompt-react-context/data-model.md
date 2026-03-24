# 資料模型草案

## 1) agents（既有表）擴充

新增欄位：

- `system_prompt` TEXT NULL
- `function_profile_id` UUID NULL（FK -> `function_profiles.id`）
- `is_router` BOOLEAN NOT NULL DEFAULT FALSE

相容讀取順序：

1. `agents.system_prompt`
2. `agents.model_config.system_prompt`
3. `agents.description`
4. 系統預設 prompt

## 2) function_profiles（新表）

用途：集中管理工具呼叫協定模板（Functions）。

欄位：

- `id` UUID PK
- `name` VARCHAR(100) UNIQUE NOT NULL
- `provider` VARCHAR(50) NULL（generic/openai/anthropic/gemini...）
- `template` TEXT NOT NULL
- `description` TEXT DEFAULT ''
- `enabled` BOOLEAN DEFAULT TRUE
- `version` INTEGER DEFAULT 1
- `created_at` DATETIME
- `updated_at` DATETIME

## 3) rag_datasets（新表）

用途：統一管理公有與私有資料集。

欄位：

- `id` UUID PK
- `name` VARCHAR(150) NOT NULL
- `scope` ENUM('global','agent_private') NOT NULL
- `agent_id` UUID NULL（`scope=agent_private` 時必填）
- `owner_user_id` UUID NULL
- `sensitivity` ENUM('normal','confidential','restricted') DEFAULT 'normal'
- `vector_backend` VARCHAR(50) NULL
- `index_name` VARCHAR(150) NULL
- `enabled` BOOLEAN DEFAULT TRUE
- `created_at` DATETIME
- `updated_at` DATETIME

規則：

- `scope=global`：`agent_id` 必須為 NULL
- `scope=agent_private`：`agent_id` 必須非 NULL，且僅對該 agent 可見

## 4) llm_turns（新表）

用途：記錄每輪模型上下文、成本、狀態；避免污染 Message。

欄位：

- `id` UUID PK
- `conversation_id` UUID FK
- `agent_id` UUID FK
- `message_user_id` UUID NULL
- `message_assistant_id` UUID NULL
- `provider` VARCHAR(50) NULL
- `model` VARCHAR(120) NULL
- `tier` VARCHAR(20) NULL
- `system_prompt_snapshot` TEXT NULL
- `context_snapshot` JSON NULL
- `usage` JSON NULL
- `cost_usd` NUMERIC(12,6) NULL
- `latency_ms` INTEGER NULL
- `status` VARCHAR(20) NOT NULL（success/timeout/no_route/tool_error...）
- `error` TEXT NULL
- `created_at` DATETIME

## 5) Message / Event / Turn 分層

- `messages`：只存 user/assistant 可見內容
- `events`（既有 EventPart）：存 ReAct/tool/progress 細節
- `llm_turns`：存模型上下文與成本審計

## 6) Router 配置

系統設定需可指定 `system_router_agent_id`（可放在設定檔或資料庫設定表）。

Router 代理者可透過 `agents.is_router = true` 標記。
