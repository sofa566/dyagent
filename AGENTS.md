# dyagent Development Guidelines

- 本專案所有文件，註解，說明等文字都統一用繁體中文。

# 本專案 Clean Code 開發規範

> 本規範適用於此專案中所有新增或修改的程式碼。
> 有疑慮時，以可讀性優先，而非追求技巧性。

---

## 命名

- 取有意義、精準且無歧義的名稱
- **方法／函式**：使用動詞 — `post_form()`、`fetch_user_data()`
- **變數**：表達其用途 — `user_account_name`，而非 `x`
- **類別（Class）**：使用名詞短語 — `Customer`、`OrderProcessor`
- **禁止使用魔術數** — 一律改用命名常數
- **禁止使用語意不明常數字串**（特別是模式值／狀態值）— 必須使用可讀、可推導行為的命名；不可用 `1/2/3/4` 或混淆字串代替

```python
# 錯誤
timeout = 1000

# 正確
TIMEOUT_MILLISECONDS = 1000
timeout = TIMEOUT_MILLISECONDS
```

---

## 函式

- **只做一件事** — 若能用「和」描述一個函式，就代表它該被拆分
- **無副作用** — 函式只做名稱所描述的事，不多也不少
- **參數盡量少**：0 個最好，1 個次之，2 個可接受，3 個以上請包成物件
- **避免旗標參數（boolean flag）** — 應拆成兩個獨立函式
- **每個函式只有一個抽象層級**
- **刪除未使用的函式** — Git 會記得，程式碼庫不應該留著

```python
# 錯誤 — 用 boolean 做兩件事
def calculate(value, is_plus: bool): ...

# 正確 — 兩個語意明確的函式
def add(value): ...
def subtract(value): ...
```

---

## 類別（Class）

- **保持短小** — 然後再更短小
- **單一職責原則（SRP）**：一個 class 只有一個修改它的理由
- **高內聚性**：class 的方法應操作自身的變數，形成邏輯整體
- **開放封閉原則（OCP）**：透過繼承子類別來擴充行為，不修改原始 class
- 限制對外公開的方法與介面數量 — 越少接觸點，耦合度越低

---

## 錯誤處理

- 將錯誤處理邏輯從主要業務邏輯中**獨立隔離**
- 在專責的層級處理錯誤，不與核心功能混在一起

---

## 註解

- **優先讓程式碼自我說明** — 好的命名與拆分比註解更有價值
- 一般情況下，註解只解釋**為什麼**，而非**是什麼**
- 刪除被註解掉的程式碼 — 用 Git 歷史紀錄找回即可

### 本專案團隊規範

**函式超過 10 行，必須在函式開頭寫註解，包含：**
1. **目的** — 這個函式是做什麼的
2. **為什麼** — 為何需要這個函式，或有什麼特殊邏輯需要說明

**類別（Class）無論長短，開頭都必須寫註解，包含：**
1. **目的** — 這個 class 負責什麼職責
2. **為什麼** — 為何獨立成一個 class，在系統中扮演什麼角色

> 💡 若一個函式超過 10 行且難以用一句話描述其目的，這通常是需要重構的訊號。


```python
# 錯誤 — 只是重複程式碼在做的事
user_count += 1  # 將使用者數量加一

# 可接受 — 解釋了非顯而易見的原因
await asyncio.sleep(0.5)  # 第三方 API 限制每秒最多 2 次請求
```

# 正確 — 超過 10 行的函式，開頭說明目的與原因
def calculate_user_discount(user, order):
    # 目的：根據用戶等級與訂單金額計算最終折扣率
    # 為什麼：折扣邏輯涉及多個業務規則組合，集中在此處統一管理避免散落各處
    ...

# 正確 — 類別開頭說明職責與存在原因
class OrderProcessor:
    # 目的：負責訂單的建立、驗證與狀態流轉
    # 為什麼：將訂單處理邏輯從 API layer 中獨立出來，符合單一職責原則，便於測試與維護
    ...

---

## 格式

- 全專案統一縮排風格
- 對齊相關程式碼以提升視覺可讀性
- 每個檔案盡量只使用一種語言，避免多語言混用

---

## 資料與結構

- **物件**：曝露行為，隱藏內部資料
- **資料結構**：曝露資料，沒有明顯行為
- 將邊界條件封裝成具名變數

```python
# 錯誤
process(items[len(items) - 1])

# 正確
last_index = len(items) - 1
process(items[last_index])
```

---

## 不要重複自己（DRY）

- 將可共用的邏輯抽取成可重用的函式
- 每一份知識在系統中只應有一個權威來源
- 重複的程式碼會增加測試範圍，也會提高出錯的風險

---

## 抽象層級

- 越通用／抽象的邏輯放在基礎類別
- 越具體／細節的邏輯放在派生子類別
- 同一個函式內不應混合不同的抽象層級

```python
# 錯誤 — 混合了直接字串操作與函式呼叫
def render_typed_string(s):
    return '_' + s + get_separated_str(s)

# 正確 — 統一抽象層級
def get_underlined_str(s):
    return '_' + s

def render_typed_string(s):
    return get_underlined_str(s) + get_separated_str(s)
```

---

## 依賴與結構

- **避免鏈式存取**：`a.get_b().get_c().get_d()` → 改為直接曝露 `a.get_deep_d()`
- 可配置的值應放在最高抽象層級（config 檔、常數），不要埋在派生類別深處
- 若函式有執行順序依賴，透過回傳值傳遞給下一個函式，讓順序明確可見

---

## 單元測試（F.I.R.S.T 原則）

- **Fast（快速）** — 測試必須夠快，才能頻繁執行
- **Independent（獨立）** — 每個測試互不依賴，執行順序不影響結果
- **Repeatable（可重複）** — 在任何環境下都能成功執行
- **Self-Validating（自我驗證）** — 測試應有明確的 pass／fail 輸出，不靠手動看 log
- **Timely（及時）** — 在實作程式碼之前或同步編寫測試

### 測試結構

- 遵循 **Given／When／Then**（BDD 行為驅動開發）
- **每個測試只驗證一個概念** — 若函式有兩種行為，就寫兩個測試
- 測試所有可能失敗的情況與邊界條件 — 沒被測到的條件就是未知風險
- 不測試 UI 實作細節 — UI 改動不應導致所有測試失敗

---

## 逐步改進

1. 先讓它能動（通過所有測試）
2. 重構 — 每次修改後回顧程式碼，改善命名、內聚性、耦合度、尺寸
3. 再次執行測試並確保通過

**不要把混亂留到之後處理。** 上午寫的亂碼，下午就趁熱整理乾淨。

---

## 多執行緒（適用時）

- 將多執行緒程式碼與業務邏輯**分離**
- 限制共享資料的作用域，避免競態條件（race condition）
- 優先複製唯讀資料，而非共享可變狀態
- 各執行緒之間盡可能保持獨立


## Active Technologies
- Python 3.11 + FastAPI、LangChain、LiteLLM（langchain-litellm）、SQLAlchemy、PyTest、Ruff (001-chat-skills-mcp)
- 既有資料庫（Conversation/Message 實體於 backend 代碼中定義） (001-chat-skills-mcp)
- Python 3.11（後端）、JavaScript ES2022（前端，Vite） + FastAPI、SQLAlchemy、LangChain/LiteLLM、PyTest、Ruff、Vite (001-agent-mcp-skills-rag)
- PostgreSQL（主）、Redis（快取/暫存）、Qdrant（向量檢索，RAG） (001-agent-mcp-skills-rag)

- Python 3.11+ / JavaScript (ES2022) + LangChain (RAG), docling-serve (文件轉換), FastAPI (後端框架), Vite (前端構建) (001-dynamic-agents)

## Project Structure

```text
backend/
frontend/
tests/
```

## Commands

cd src [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] pytest [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] ruff check .

## Code Style

Python 3.11+ / JavaScript (ES2022): Follow standard conventions

## Recent Changes
- 001-agent-mcp-skills-rag: Added Python 3.11（後端）、JavaScript ES2022（前端，Vite） + FastAPI、SQLAlchemy、LangChain/LiteLLM、PyTest、Ruff、Vite
- 001-chat-skills-mcp: Added Python 3.11 + FastAPI、LangChain、LiteLLM（langchain-litellm）、SQLAlchemy、PyTest、Ruff

- 001-dynamic-agents: Added Python 3.11+ / JavaScript (ES2022) + LangChain (RAG), docling-serve (文件轉換), FastAPI (後端框架), Vite (前端構建)

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
