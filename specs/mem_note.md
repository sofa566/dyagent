在多使用者、多代理者（Multi-Agent）且由 Master Agent 負責分派的架構中，長期記憶不應該只是簡單地讓每個 Function Agent 各自具備一個獨立且封閉的記憶庫。

最理想且通用的設計是「多維度隔離與共享」的矩陣式記憶架構。以下為您詳細拆解記憶該如何切分、為什麼這樣設計，以及如何實作。

## 1.記憶切分的黃金架構（矩陣式記憶）

在多使用者系統中，如果讓 Function Agent 各自擁有完全獨立的記憶，會導致「記憶孤島」效應（例如：使用者在 A 任務提過的要求，B 任務的 Agent 完全不知道）。因此，您的記憶庫必須同時滿足使用者維度與功能維度的切分：


```
                [ Master Agent ] (負責分派與全域調度)
                       │
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
   [用戶特定記憶]   [全域公共記憶]   [Agent 功能記憶]
   (User Memory)   (Global Memory)  (Agent Memory)
         │             │             │
   ┌─────┴─────┐       │             ├─────────────┐
   ▼           ▼       ▼             ▼             ▼
[使用者 A]  [使用者 B]  [公共知識庫]  [程式碼 Agent] [日曆 Agent]
```

### ① 使用者專屬記憶 (User Memory) —— 絕對隔離
- 擁有者：屬於特定使用者，跨所有 Agent 共享。
- 內容：使用者的偏好（如：喜歡用 Python 寫程式、開會偏好下午）、個人背景、歷史交談風格。
- 為什麼：不論 Master 把任務分派給哪個 Function Agent，該 Agent 都必須遵守該使用者的個人偏好。

### ② Agent 專業記憶 (Agent Memory) —— 功能隔離
- 擁有者：屬於特定的 Function Agent，跨所有使用者累積（或依用戶進一步隔離）。
- 內容：該 Agent 執行特定任務的經驗。例如「程式碼優化 Agent」紀錄哪些優化策略最常被接受；「日常排程 Agent」紀錄特定 API 的報錯與修正經驗（Few-shot 範例累積）。
- 為什麼：讓 Function Agent 在其專業領域「越用越聰明」，但不需要把這些專業的執行細節污染到其他 Agent。

### ③ 全域公共記憶 (Global Memory) —— 完全共享
- 內容：系統層級的規則、多使用者皆可查閱的公共知識庫、組織共享文件。

## 2. Master 怎麼動態組裝記憶？（運作流程）
當使用者發出請求時，Master Agent 的核心工作之一就是「組裝上下文（Context Assembly）」：
1. 接收請求：使用者 A 發送請求給 Master Agent。
2. 提取用戶記憶：Master 從資料庫撈出「使用者 A 的長期記憶」。
3. 分派任務：Master 決定將任務交給「報表 Agent」。
4. 注入雙重記憶：Master 將「使用者 A 的記憶」與「報表 Agent 的專業記憶」同時注入到 Prompt 中，再發送給報表 Agent。
5. 更新記憶：任務結束後，產生的新記憶依性質分流儲存。使用者的偏好存回 User Memory；報表的執行經驗存回 Agent Memory。

### 3. 長期記憶使用 mem0 方案
使用 [Mem0](https://adg.csdn.net/694cf3e65b9f5f31781aa429.html) 方案來構建這套多使用者、多代理者（Master-Worker）系統是非常精準的選擇。Mem0 的核心設計本就原生支持 user_id 與 agent_id 的多維度隔離（Scoping），它在底層會自動將這些 ID 轉化為資料庫的標籤（TAG）或獨立的索引，確保在物理或邏輯層面實現合規的數據隔離。

## 1. Mem0 多維度隔離架構設計
在您的系統中，記憶將透過 Mem0 的三大範疇（Scopes）進行精準對齊：
### 1. 使用者專屬記憶（User Scope）：
- 標記方式：指定 user_id。
- 行為：由 Master Agent 全域控管。跨所有 Function Agent 共享此用戶的偏好（例如：「用戶習慣看繁體中文」、「不喜歡囉唆」）。
### 2. 功能代理者記憶（Agent Scope）：
- 標記方式：指定 agent_id。
- 行為：各個 Function Agent 專屬的專業技能沉澱，不綁定特定使用者。它累積的是執行該功能時的 Few-shot 範例或優化路徑（例如：程式優化 Agent 記住「Python 3.12 的某個新語法效能更好」）。
### 3. 用戶與功能交叉記憶（User-Agent Interaction Scope）：
- 標記方式：同時指定 user_id 與 agent_id。
- 行為：針對特定用戶在特定功能下的特殊要求（例如：用戶 A 要求「程式優化 Agent」在幫他改程式碼時一律不准用三元運算子）。
## 2. 實作程式碼：
原生 Python 記憶管理器請先確保您的原生 Python 環境已安裝 Mem0（pip install mem0ai）。以下程式碼封裝了多租戶嚴格隔離與動態記憶組裝的邏輯：
```python
import os
from typing import List, Dict, Any
from mem0 import Memory

class ComplianceAgentMemoryManager:
    def __init__(self):
        # 1. 初始化 Mem0。
        # 您可以使用開源自託管自訂向量庫（如 Qdrant / Chroma），或使用 Mem0 Cloud。
        # 這裡以開源預設配置為例，它會在底層進行合規的標籤隔離。
        self.memory = Memory()

    def get_context_for_worker(self, user_id: str, agent_id: str, current_query: str) -> str:
        """
        Master Agent 在分派任務前調用：
        嚴格隔離檢索，並組裝該 Function Agent 所需的所有合法上下文。
        """
        if not user_id:
            raise ValueError("【合規安全性錯誤】: 必須提供 user_id，禁止全域越權檢索！")

        # 核心防禦：利用 Mem0 的 filters 機制，在資料庫底層強制執行嚴格隔離檢索。

        # A. 檢索該用戶的「全域個人偏好」 (跨 Agent 共享)
        user_global_memories = self.memory.search(
            query=current_query,
            filters={"user_id": user_id, "agent_id": "global_preference"}
        )

        # B. 檢索該用戶在「該特定 Agent」下的特殊習慣 (User-Agent 交叉記憶)
        user_agent_specific_memories = self.memory.search(
            query=current_query,
            filters={"user_id": user_id, "agent_id": agent_id}
        )

        # C. 檢索該 Agent 的「通用專業知識」 (不含任何用戶隱私，全系統跨用戶共享)
        agent_expertise_memories = self.memory.search(
            query=current_query,
            filters={"user_id": "system_shared", "agent_id": agent_id}
        )

        # 格式化提取出的記憶文本
        def extract_text(mem_list):
            # Mem0 search 返回的結構中，記憶文字通常在 'memory' 或 'text' 欄位
            return [m["memory"] for m in mem_list if "memory" in m]

        user_prefs = extract_text(user_global_memories)
        user_agent_prefs = extract_text(user_agent_specific_memories)
        agent_expertises = extract_text(agent_expertise_memories)

        # 3. 組裝成嚴格隔離的 System Prompt 上下文
        context_prompt = f"""
=== 嚴格隱私隔離上下文 (僅供本次對話線程使用) ===
【當前使用者全域偏好】:
{chr(10).join([f'- {m}' for m in user_prefs]) if user_prefs else '無特定偏好'}

【當前使用者對本代理人的特殊要求】:
{chr(10).join([f'- {m}' for m in user_agent_prefs]) if user_agent_prefs else '無特殊要求'}

【本代理人沉澱之專業執行經驗（公共知識）】:
{chr(10).join([f'- {m}' for m in agent_expertises]) if agent_expertises else '無相關經驗沉澱'}
==================================================
"""
        return context_prompt

    def add_user_preference(self, user_id: str, text: str, agent_id: str = "global_preference"):
        """
        記錄用戶偏好（可選擇記錄為全域偏好，或在特定 Agent 下的偏好）
        """
        self.memory.add(
            text,
            user_id=user_id,
            metadata={"agent_id": agent_id}  # 透過 metadata 深度隔離
        )

    def add_agent_generalized_experience(self, agent_id: str, text: str):
        """
        儲存 Agent 的專業優化經驗。
        【合規防禦】：使用 'system_shared' 固定 ID 存放，不綁定任何真實 user_id，避免 PII 洩露。
        """
        self.memory.add(
            text,
            user_id="system_shared",
            metadata={"agent_id": agent_id}
        )

```
## 3. Master Agent 的工作流水線（Workflow）整合
在原生 Python 系統中，Master Agent 收到請求後的完整安全派發流程如下：
```python
# 模擬系統運作實體
memory_manager = ComplianceAgentMemoryManager()

def master_agent_orchestrator(user_id: str, user_request: str):
    """
    Master Agent 核心調度函數
    """
    print(f"[Master] 收到來自使用者 {user_id} 的請求: '{user_request}'")

    # 1. Master 進行意圖路由（假設分析後決定交給 code_agent）
    target_agent_id = "code_optimizer_agent"

    # 2. 安全屏障：從 Mem0 提取當前用戶與該 Agent 允許組合的記憶
    # 底層 filters 會隔離掉其他 user_id 的數據，絕不可能發生 cross-user 漏洞
    safe_context = memory_manager.get_context_for_worker(
        user_id=user_id,
        agent_id=target_agent_id,
        current_query=user_request
    )

    # 3. 呼叫 Function Agent（Function Agent 保持 Stateless 無狀態設計）
    # 將安全上下文作為 System Prompt 的一部分注入
    final_system_prompt = f"你是一個專業的程式碼優化 AI。請遵循以下上下文指導：\n{safe_context}"

    print(f"[Master] 已成功注入隔離記憶，派發任務至 -> {target_agent_id}")

    # response = call_llm(system_prompt=final_system_prompt, user_prompt=user_request)
    # return response

# --- 模擬資料寫入示範 ---
# 使用者 A 說：「我不喜歡用三元運算子」-> 記錄在程式 Agent 的範疇下
memory_manager.add_user_preference(user_id="user_A", text="用戶在寫程式時不喜歡使用三元運算子", agent_id="code_optimizer_agent")

# 使用者 B 說：「我只看繁體中文」-> 記錄在全域範疇下
memory_manager.add_user_preference(user_id="user_B", text="用戶偏好繁體中文回應", agent_id="global_preference")

# 程式 Agent 自己學會了新技巧 -> 記錄為公共技術經驗
memory_manager.add_agent_generalized_experience(agent_id="code_optimizer_agent", text="Python 3.12 中使用 F-string 的效能優於舊版格式化")

```
## 4. 此 Mem0 架構的合規與安全優勢
### 1. 底層 Filter 強制隔離：
在呼叫 self.memory.search 時，程式碼強制代入了 filters={"user_id": user_id}。Mem0 在轉譯為向量資料庫查詢時，會在資料庫索引階段直接過濾掉非該使用者的所有向量，完全杜絕了 Top-K 語意相似度可能撈出隔壁棚使用者敏感資料的風險。
### 2. 一鍵滿足「忘記我權利」（GDPR Compliance）：
如果 user_A 要求刪除所有個資，您不需要重構公共知識，只需調用 Mem0 的 self.memory.reset(user_id="user_A")，Mem0 就會乾淨地抹除該命名空間下的所有微小偏好記錄。
### 3. 無狀態 Worker 防禦：
所有的 Function Agent（如 code_agent）本身都不存儲狀態與記憶。它們只是「工具人」，每次被 Master 呼叫時才吃進當下臨時組裝好的 Prompt [4]。這防止了多線程並行處理不同使用者時，在記憶體內部發生變數污染的可能。

## 5. 長期與短期記憶的核心差別
| 記憶類型 | 在 AI Agent 中的本質 | 程式碼中的實作載體 | 消失時機 |
| :--- |  :--- | :--- | :--- |
| 短期記憶(Short-term) | 當前對話的前後文連貫性與工作狀態。例如：這輪對話剛說過的話、上一個工具執行的錯誤。 | Python 記憶體變數(List 儲存的 Chat History) 或是 LLM Context Window。 | 該次任務結束、API 請求回應後，變數即釋放。 |
| 長期記憶(Long-term) | 跨越時間、跨工作階段（Session）被沉澱下來的事實、偏好與知識。 | 外部持久化資料庫(透過 Mem0 提取、動態更新並寫入磁碟)。 | 永久儲存，除非主動刪除。 |

## 6. Mem0 的標準雙層記憶實作設計
Mem0 原生支援將這兩者結合。以下為您重寫一個「同時包含短期對話記憶 + 長期事實記憶」的原生 Python 運作模組：
```python
from typing import List, Dict, Any
from mem0 import Memory

class DualMemoryAgent:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        # 初始化 Mem0 (負責長期記憶)
        self.long_term_mem0 = Memory()

        # 【短期記憶實作】：使用 Python 的字典與列表儲存於記憶體中
        # 結構為 { user_id: [ {"role": "user", "content": "..."}, ... ] }
        self.short_term_chat_histories: Dict[str, List[Dict[str, str]]] = {}

    def get_full_context(self, user_id: str, user_query: str) -> List[Dict[str, str]]:
        """
        組裝 LLM 最終需要的完整上下文（包含長期偏好與短期對話歷史）
        """
        # 1. 獲取【長期記憶】（從 Mem0 向量資料庫中搜尋與當前問題相關的事實）
        ltm_results = self.long_term_mem0.search(
            query=user_query,
            filters={"user_id": user_id, "agent_id": self.agent_id}
        )
        long_term_facts = [m["memory"] for m in ltm_results if "memory" in m]

        # 2. 建立 LLM 的 System Prompt (注入長期記憶)
        system_prompt = f"""你是一個專業的 AI 智慧體。
【此用戶的長期偏好/事實】：
{chr(10).join([f'- {fact}' for fact in long_term_facts]) if long_term_facts else '無特定長期記憶'}
"""

        # 3. 獲取【短期記憶】（此用戶在「當前這一個對話 Session」中的最近幾輪對話）
        if user_id not in self.short_term_chat_histories:
            self.short_term_chat_histories[user_id] = []

        short_term_history = self.short_term_chat_histories[user_id][-6:] # 僅保留最近 3 輪對話以防爆 Context

        # 4. 完美組裝：LLM Message 陣列
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(short_term_history) # 塞入短期記憶
        messages.append({"role": "user", "content": user_query}) # 塞入當前問題

        return messages

    def update_memories_after_reply(self, user_id: str, user_query: str, agent_response: str):
        """
        當 LLM 回應完成後，同步更新短期與長期記憶
        """
        # 1. 更新【短期記憶】：直接 append 進記憶體變數，維持當下對話連貫
        if user_id not in self.short_term_chat_histories:
            self.short_term_chat_histories[user_id] = []

        self.short_term_chat_histories[user_id].append({"role": "user", "content": user_query})
        self.short_term_chat_histories[user_id].append({"role": "assistant", "content": agent_response})

        # 2. 更新【長期記憶】：丟給 Mem0，Mem0 會利用 LLM 自動動態評估：
        # 這句話有沒有沉澱為「長期偏好」的價值？如果有，才寫入資料庫。
        # 例如：「把剛才的程式改用 Java 寫」-> 屬於短期對話。
        # 例如：「我以後開發都改用 Java 了」-> Mem0 會自動捕捉並更新長期記憶。
        self.long_term_mem0.add(
            f"User: {user_query} \nAgent: {agent_response}",
            user_id=user_id,
            metadata={"agent_id": self.agent_id}
        )

    def clear_short_term_memory(self, user_id: str):
        """
        當用戶點擊「開啟新對話」或更換主題時呼叫
        只清空短期，長期記憶在資料庫內依然完好
        """
        if user_id in self.short_term_chat_histories:
            self.short_term_chat_histories[user_id] = []

```
## 7. 一個具體的情境演練
- 第一句：「我個人完全不會寫 Python。」
  - 長期記憶：Mem0 將其沉澱並寫入資料庫："用戶不具備 Python 程式能力"。
  - 短期記憶：Python List 記住這句話。
- 第二句：「那你可以幫我寫一個查詢天氣的網頁前端嗎？」
  - 短期記憶的作用：LLM 因為看得到上一輪的 List 內容（短期記憶），所以知道「那」指的是「因為不會 Python」，並自動選擇使用 HTML/JavaScript 來寫，而不會寫出 Python 後端。
- 隔天，開啟新對話，短期記憶已被 clear_short_term_memory 清空）：「我想做一個計時器網頁。」
  - 短期記憶：此時為空。
  - 長期記憶的作用：get_full_context 從 Mem0 撈出昨天的紀錄："用戶不具備 Python 程式能力" 並轉成 System Prompt。LLM 看到後，再次自動避開 Python，改用網頁原生工具。

透過這樣的實作，您的原生系統就能完美兼顧「當下聊天的上下文（短期）」與「跨越時空的個資偏好（長期）」。

## 8. 實做長期記憶與短期其記憶

### 8.1. 技術使用
- Postgres：存放結構化商業資料（使用者資訊、Agent 註冊表、Master 的分派紀錄等）。
- Redis：存放高併發的短期記憶（維持對話連貫、設定 1 小時過期時間）。
- Qdrant：存放被 Mem0 萃取後的長期記憶（使用者跨天/跨代理人的事實、習慣、歷史偏好）。

### 8.2 實作設計：原生 Python 直接對接 Qdrant 與 Redis
在原生 Python 系統中，我們不需要再單獨啟動 Mem0 Server 的 Docker 容器。您可以直接在 Python 代碼中初始化 mem0.Memory，並將配置同時指向您現有的 Qdrant 容器以及 LiteLLM 網關。

請先確保您的 Python 環境安裝了 Qdrant 的 SDK 支援：
```bash
pip install mem0ai qdrant-client redis

```
以下是針對目前的容器配置，為使用 Python 雙層記憶架構：
```python
import json
import redis
from mem0 import Memory

# 1. 初始化短期記憶（指向您的 dyagent-redis 容器，同台伺服器可用 localhost）
redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
SHORT_TERM_TTL = 3600  # 短期對話歷史緩存一小時

# 2. 初始化長期記憶：配置直接連向您的 dyagent-qdrant 容器與 LiteLLM 網關
config = {
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "host": "localhost",
            "port": 6333,
            "collection_name": "dyagent_long_term_memories" # Mem0 會自動在 Qdrant 內建立這個 Collection
        }
    },
    "llm": {
        "provider": "openai", # 透過 LiteLLM 的 OpenAI 相容接口來幫 Mem0 萃取記憶
        "config": {
            "api_base": "http://localhost:4000/v1", # 您的 LiteLLM 網關埠口
            "model": "litemodel-extraction",
            "api_key": "sk-litellm-dummy-key"
        }
    },
    "embedder": {
        "provider": "openai", # 透過 LiteLLM 來幫記憶生成向量 (Embedding)
        "config": {
            "api_base": "http://localhost:4000/v1",
            "model": "litemodel-embedding",
            "api_key": "sk-litellm-dummy-key"
        }
    }
}

# 建立長期記憶管理實體
long_term_memory = Memory.from_config(config)

# ========================================================
# 記憶管裡核心業務邏輯 (Master 與 Worker 排程完美適用)
# ========================================================

def get_redis_key(user_id: str, agent_id: str) -> str:
    """生成短期對話的 Redis Key，嚴格進行橫向用戶與縱向代理人的安全隔離"""
    return f"short_term:user:{user_id}:agent:{agent_id}"

def prepare_context_for_worker(user_id: str, agent_id: str, user_query: str) -> list:
    """
    Master Agent 在分派任務給特定的 Function Agent 前調用：
    同時從 Qdrant 撈取長期 Facts，從 Redis 撈取最近短期歷史，並打包給 LiteLLM。
    """
    # A. 提取【長期事實偏好】（Qdrant）
    # Mem0 會自動在 Qdrant 內部執行向量搜尋，並加上 Metadata 過濾，絕不發生跨用戶外洩
    ltm_results = long_term_memory.search(
        query=user_query,
        user_id=user_id,
        filters={"agent_id": agent_id} # 強制隔離：只檢索該功能 Worker 沉澱下來的記憶
    )
    long_term_facts = [m["memory"] for m in ltm_results if "memory" in m]

    # B. 提取【短期對話歷史】（Redis）
    redis_key = get_redis_key(user_id, agent_id)
    raw_history = redis_client.lrange(redis_key, 0, -1)
    chat_history = [json.loads(msg) for msg in raw_history]

    # C. 組裝符合 LiteLLM/OpenAI 規範的 Messages 上下文陣列
    system_prompt = f"""你是一個被 Master 分派任務的專屬智慧體（Agent ID: {agent_id}）。
請務必嚴格遵守此用戶的長期偏好、習慣與背景事實。

【此用戶的長期事實與偏好紀錄（來自 Qdrant 庫）】：
{chr(10).join([f'- {fact}' for fact in long_term_facts]) if long_term_facts else '無特定長期偏好紀錄'}
"""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(chat_history)  # 注入短期對話前後文
    messages.append({"role": "user", "content": user_query})  # 塞入當前的新問題

    return messages

def update_dual_memory(user_id: str, agent_id: str, user_query: str, agent_response: str):
    """
    Function Agent 執行回應完成後，由系統非同步/同步更新雙層記憶
    """
    redis_key = get_redis_key(user_id, agent_id)

    # 1. 更新【短期對話】 (Redis)
    user_msg = json.dumps({"role": "user", "content": user_query})
    agent_msg = json.dumps({"role": "assistant", "content": agent_response})

    redis_client.rpush(redis_key, user_msg, agent_msg)
    redis_client.ltrim(redis_key, -10, -1)  # 限制最近 5 輪對話（10條訊息），防止上下文過長
    redis_client.expire(redis_key, SHORT_TERM_TTL)  # 重設過期時間

    # 2. 更新【長期記憶】 (Qdrant)
    # 丟給 Mem0，Mem0 會調用 LiteLLM 判斷是否有長期留存價值（如改用新語言、新偏好），有則向量化存入 Qdrant
    conversation_chunk = f"User: {user_query} \nAgent: {agent_response}"
    long_term_memory.add(
        text=conversation_chunk,
        user_id=user_id,
        metadata={"agent_id": agent_id}
    )

```
