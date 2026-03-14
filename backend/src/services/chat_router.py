"""
聊天路由（骨架）

職責：
- 組織單輪回合（single-turn）流程：彙整歷史 → 呼叫 LLMClient → 回傳助理回覆
- 後續將擴充事件/parts 寫盤、工具呼叫、結構化輸出等能力
"""

from __future__ import annotations

from typing import Optional, List, Any, Tuple
import uuid
from collections import deque

from src.services.llm_client import LLMClient
from src.core.logging import get_logger
from sqlalchemy.orm import Session
from src.models import Log, Agent
from src.models.events import EventPart
from src.core.config import settings
from src.services.permission_service import PermissionService
from src.services.mcp_client import MCPClient


class ChatRouter:
    """單輪聊天管線骨架。"""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self._llm = llm or LLMClient()
        self._log = get_logger("ChatRouter")
        # 最近一次回合的度量（供外部查詢）
        self._last_metrics: dict | None = None
        # 結構化輸出模式（T017 預留）
        self._structured_mode: dict | None = None  # {"schema": {...}}
        self._last_structured: dict | None = None
        # Doom loop 檢測：保存最近工具呼叫紀錄 (tool, normalized_input)
        self._recent_tool_calls: deque[Tuple[str, str]] = deque(maxlen=32)
        self._perm = PermissionService()
        self._mcp = MCPClient()

    def single_turn(self, *, session_id: str, agent_id: str, user_message: str, tier: Optional[str] = None, db: Optional[Session] = None, llm_overrides: Optional[dict] = None) -> str:
        """執行單輪回合並回傳助理文字。

        備註：
        - 歷史彙整、事件寫盤、結構化輸出與工具呼叫預留於後續任務實作
        - 目前僅委派 LLMClient.complete 取得文字輸出
        """
        # 預留：載入/彙整歷史訊息（依 Conversation/Message 模型）
        history: List[str] = []  # TODO: 從資料庫查詢歷史訊息

        # 綁定可選的 DB 寫盤目標
        self._db: Optional[Session] = db

        # 介面互動記錄（資訊等級）：標記使用者選擇之代理者與會話（以 session_id 表示）
        try:
            self._log.info("chat.ui.代理者已選", agent_id=agent_id, session_id=session_id, tier=tier or "")
        except Exception:
            pass

        # 事件寫盤
        self.write_event_start(session_id=session_id, agent_id=agent_id)
        self.write_event_step_start(session_id=session_id)

        # 初始化 LLM 客戶端（相容舊版介面無 overrides 參數）
        try:
            self._llm.init_for_session(session_id=session_id, preferred_tier=tier, overrides=llm_overrides)
        except TypeError:
            self._llm.init_for_session(session_id=session_id, preferred_tier=tier)

        agent_ctx = self._prepare_integrations(db=db, agent_id=agent_id)

        # 若啟用 RAG，嘗試構建最小檢索上下文（目前無嵌入/檢索，先提供來源標籤提示）
        rag_prefix = ""
        refs_block = ""
        try:
            rag = agent_ctx.get("rag", {})
            if rag and rag.get("enabled"):
                sources = rag.get("sources", []) or []
                topk = rag.get("topK", 5) or 5
                rag_prefix = f"\n[RAG] sources={','.join(sources)} topK={topk}"
                refs_block = "\n\nReferences:\n- （目前未連接檢索服務，無可用引用）"
        except Exception:
            pass

        # 為模型加入能力提示前綴，讓回覆能考量可用能力（即使目前為骨架）
        mcp_names = ",".join([str(c.get("name") or "").strip() for c in agent_ctx.get("mcp", []) if isinstance(c, dict) and c.get("enabled")])
        skills_names = ",".join(agent_ctx.get("skills", []))
        prefix = f"[Agent Capabilities] skills={skills_names} mcp={mcp_names}{rag_prefix}\n"
        composed_user_message = f"{prefix}{user_message}"

        # 若啟用結構化輸出模式：強制 StructuredOutput 工具並於成功後立即結束（T017 預留）
        if self._structured_mode is not None:
            # 強制注入固定工具 ID，並視為 toolChoice=required（規格要求）
            self._log.info("structured.enabled", tool_id="StructuredOutput", tool_choice="required")
            # 呼叫結構化輸出「工具」並將結果寫入本回合的 structured
            schema = self._structured_mode.get("schema") if isinstance(self._structured_mode, dict) else None
            # 優先嘗試由 LLMClient 取得結構化輸出；失敗則回退骨架
            try:
                result = self._llm.structured_output(prompt=user_message, schema=schema or {}, tier=tier)
            except Exception:
                result = self._call_structured_output(schema=schema or {}, user_message=user_message)
            self._last_structured = result
            # 記錄最小度量並結束回合
            self._last_metrics = {"tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}, "cost": 0.0}
            self.write_event_step_finish(session_id=session_id)
            self.write_event_finish(session_id=session_id)
            return ""  # 結構化輸出成功即結束回合（不再產生一般文字）

        # 串流取得輸出（簡化為兩段），並寫入 reasoning/text 事件
        chunks: List[str] = []
        try:
            for delta in self._llm.stream_complete(prompt=composed_user_message, tier=tier):
                # 視需要可區分 reasoning/text；此處以 text 事件示意
                chunks.append(delta)
                self.write_event_text(session_id=session_id, delta=delta)
        except Exception as e:  # T018：錯誤處理映射（auth / network / overflow）
            reason = self._classify_error(e)
            self.write_event_tool_error(session_id=session_id, tool="llm", error=reason)
            if reason == "context_overflow":
                # T043：建立壓縮任務
                self._create_compaction_task(session_id=session_id, overflow=True)
            # 結束事件
            self.write_event_step_finish(session_id=session_id)
            self.write_event_finish(session_id=session_id)
            # 以對使用者友善的錯誤訊息回覆（之後可改為 assistant.error 欄位）
            return self._friendly_error_message(reason)

        output_text = "".join(chunks)
        if refs_block:
            output_text = f"{output_text}{refs_block}"

        # 嘗試解析工具呼叫（Function-Call 簡易協議）：
        # 格式：[[CALL tool=mcp:NAME]]\n{...JSON...}
        try:
            handled = self._maybe_handle_tool_call(session_id=session_id, text=output_text)
            if handled is not None:
                output_text = handled
        except Exception as e:
            self._log.warning("toolcall.parse_failed", error=str(e))

        # 簡易估算 tokens 與成本（後續以供應商資料覆蓋）
        approx_tokens = max(1, len(output_text) // 4)
        self._last_metrics = {
            "tokens": {"input": 0, "output": approx_tokens, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "cost": 0.0,
        }

        # 若啟用「無可用路由即硬失敗」，且本輪為降級/骨架輸出，則回傳錯誤（不寫助理訊息）
        try:
            info = self._llm.last_route_info()
            is_fallback = not info or (str(info.get("provider")) == "fallback")
        except Exception:
            is_fallback = False
        if getattr(settings, "LLM_HARD_FAIL_ON_NO_ROUTE", False) and is_fallback:
            # 記錄工具錯誤並結束事件
            self.write_event_tool_error(session_id=session_id, tool="llm", error="no_route")
            self.write_event_step_finish(session_id=session_id)
            self.write_event_finish(session_id=session_id)
            # 以 RuntimeError 讓上層路由轉成 503（不回傳文字、不寫入助理訊息）
            raise RuntimeError("no_route")

        # 正常結束事件
        self.write_event_step_finish(session_id=session_id)
        self.write_event_finish(session_id=session_id)

        return output_text

    def _prepare_integrations(self, *, db: Optional[Session], agent_id: str) -> dict[str, Any]:
        agent_ctx: dict[str, Any] = {"skills": [], "mcp": [], "rag": {"enabled": False, "sources": [], "topK": 5}}
        try:
            if db is not None:
                ag = db.query(Agent).filter(Agent.id == agent_id).first()
                if ag is not None:
                    mcp_raw = ag.mcp_config or []
                    if isinstance(mcp_raw, dict):
                        mcp_list = list(mcp_raw.values())
                    elif isinstance(mcp_raw, list):
                        mcp_list = mcp_raw
                    else:
                        mcp_list = []
                    agent_ctx["mcp"] = [c for c in mcp_list if isinstance(c, dict) and c.get("enabled")]
                    agent_ctx["skills"] = [s for s in (ag.skills or []) if isinstance(s, str) and s.strip()]
                    rc = ag.rag_config or {}
                    agent_ctx["rag"] = {
                        "enabled": bool(rc.get("enabled", False)),
                        "sources": list(rc.get("sources", []) or []),
                        "topK": int(rc.get("topK", 5) or 5),
                    }
        except Exception as _e:
            self._log.warning("agent.integrations.load_failed", error=str(_e))
        # 允許的工具名（若未來加入工具呼叫時作為白名單）
        self._allowed_tools = set(agent_ctx.get("skills", [])) | {f"mcp:{(c.get('name') or '').strip()}" for c in agent_ctx.get("mcp", []) if isinstance(c, dict)}
        # 建立 MCP 名稱對連線的映射
        self._mcp_map = {}
        for c in agent_ctx.get("mcp", []) or []:
            if isinstance(c, dict) and c.get("enabled") and c.get("name"):
                self._mcp_map[str(c.get("name")).strip()] = c
        return agent_ctx

    async def call_tool_async(self, *, session_id: str, tool: str, payload: dict, db: Optional[Session] = None, agent_id: Optional[str] = None) -> dict:
        # 若提供 agent_id 與 db，先載入整合設定以建立白名單
        if agent_id and db is not None:
            self._prepare_integrations(db=db, agent_id=agent_id)
        # 名稱/白名單與 doom-loop 同步邏輯
        name = (tool or "").strip()
        if not name:
            self.write_event_tool_error(session_id=session_id, tool="<empty>", error="invalid_tool")
            return {"ok": False, "error": "invalid_tool"}
        if name not in getattr(self, "_allowed_tools", set()):
            self.write_event_tool_error(session_id=session_id, tool=name, error="tool_not_allowed")
            return {"ok": False, "error": "tool_not_allowed"}
        self.write_event_tool_input(session_id=session_id, tool=name, payload=payload or {})
        if self._check_doom_loop(session_id=session_id, tool=name, payload=payload or {}):
            return {"ok": False, "error": "doom_loop_denied"}

        # 僅在 MCP 工具時優先走 WS JSON-RPC 串流
        if name.startswith("mcp:"):
            conn_name = name.split(":", 1)[1]
            conn = getattr(self, "_mcp_map", {}).get(conn_name)
            if not conn:
                self.write_event_tool_error(session_id=session_id, tool=name, error="mcp_connection_not_found")
                return {"ok": False, "error": "mcp_connection_not_found"}
            base_url = str(conn.get("base_url") or "").strip()
            auth = conn.get("auth") if isinstance(conn, dict) else None
            if not base_url:
                self.write_event_tool_error(session_id=session_id, tool=name, error="mcp_invalid_base_url")
                return {"ok": False, "error": "mcp_invalid_base_url"}
            # 嘗試 WS 串流呼叫
            try:
                async for frame in self._mcp.stream_rpc_call_ws(base_url=base_url, method="tools.invoke", params={"tool": conn_name, "arguments": payload or {}}, auth=auth if isinstance(auth, dict) else None):
                    # 可在此寫入逐段事件，先保留最小行為：若拿到 result 即成功
                    if isinstance(frame, dict) and ("result" in frame or frame.get("ok") is True):
                        self.write_event_tool_result(session_id=session_id, tool=name, result={"ok": True})
                        return {"ok": True, "result": frame.get("result", frame)}
                    if isinstance(frame, dict) and ("error" in frame or frame.get("ok") is False):
                        # 結束於錯誤
                        self.write_event_tool_error(session_id=session_id, tool=name, error=str(frame.get("error")))
                        return {"ok": False, "error": str(frame.get("error"))}
            except Exception as e:
                # WS 不可用或失敗時，落回同步 HTTP 邏輯
                self._log.warning("mcp.ws.stream_failed_fallback_http", error=str(e))
                return self.call_tool(session_id=session_id, tool=name, payload=payload)

        # 其他情況沿用同步路徑
        return self.call_tool(session_id=session_id, tool=name, payload=payload)

    def _maybe_handle_tool_call(self, *, session_id: str, text: str) -> str | None:
        marker = "[[CALL tool="
        idx = text.find(marker)
        if idx < 0:
            return None
        try:
            tail = text[idx + len(marker):]
            name_end = tail.find("]]")
            if name_end < 0:
                return None
            tool_name = tail[:name_end].strip()
            # 取 JSON 主體
            after = tail[name_end + 2 :].lstrip()  # 跳過 "]]"
            # 嘗試讀取第一個大括號 JSON
            j_start = after.find("{")
            j_end = after.rfind("}")
            payload = {}
            if j_start >= 0 and j_end >= 0 and j_end > j_start:
                import json as _json
                payload = _json.loads(after[j_start : j_end + 1])
            # 呼叫工具
            result = self.call_tool(session_id=session_id, tool=tool_name, payload=payload)
            # 以簡易格式附加工具結果
            if result.get("ok"):
                return f"{text}\n\n[Tool Result] {tool_name}: success"
            return f"{text}\n\n[Tool Result] {tool_name}: failed ({result.get('error')})"
        except Exception:
            return None

    # 公用：以白名單強制的工具呼叫（未來供工具規劃/LLM function call 整合）
    def call_tool(self, *, session_id: str, tool: str, payload: dict) -> dict:
        # 正規化名稱
        name = (tool or "").strip()
        if not name:
            self.write_event_tool_error(session_id=session_id, tool="<empty>", error="invalid_tool")
            return {"ok": False, "error": "invalid_tool"}

        # 白名單檢查
        if name not in self._allowed_tools:
            self.write_event_tool_error(session_id=session_id, tool=name, error="tool_not_allowed")
            return {"ok": False, "error": "tool_not_allowed"}

        # Doom loop 檢測與事件記錄
        self.write_event_tool_input(session_id=session_id, tool=name, payload=payload or {})
        if self._check_doom_loop(session_id=session_id, tool=name, payload=payload or {}):
            return {"ok": False, "error": "doom_loop_denied"}

        # MCP 工具：命名慣例 mcp:<conn-name>
        if name.startswith("mcp:"):
            conn_name = name.split(":", 1)[1]
            conn = self._mcp_map.get(conn_name)
            if not conn:
                self.write_event_tool_error(session_id=session_id, tool=name, error="mcp_connection_not_found")
                return {"ok": False, "error": "mcp_connection_not_found"}
            base_url = str(conn.get("base_url") or "").strip()
            if not base_url:
                self.write_event_tool_error(session_id=session_id, tool=name, error="mcp_invalid_base_url")
                return {"ok": False, "error": "mcp_invalid_base_url"}
            # 佔位呼叫
            try:
                res = self._mcp.invoke(base_url=base_url, name=conn_name, arguments=payload or {})
                self.write_event_tool_result(session_id=session_id, tool=name, result={"ok": True})
                return {"ok": True, "result": res}
            except Exception as e:
                self.write_event_tool_error(session_id=session_id, tool=name, error=str(e))
                return {"ok": False, "error": str(e)}

        # 其他工具（如本地 Skills）：暫以佔位回傳
        try:
            self.write_event_tool_result(session_id=session_id, tool=name, result={"ok": True})
            return {"ok": True, "result": {"tool": name, "data": payload or {}}}
        except Exception as e:
            self.write_event_tool_error(session_id=session_id, tool=name, error=str(e))
            return {"ok": False, "error": str(e)}

    # 取得最近一次回合的度量（tokens/cost 等）
    def last_metrics(self) -> dict | None:
        return self._last_metrics

    # 啟用結構化輸出模式（T017 預留 API）
    def enable_structured_output(self, *, schema: dict) -> None:
        self._structured_mode = {"schema": schema}

    # 取得最近一次結構化輸出結果
    def last_structured(self) -> dict | None:
        return self._last_structured

    # 實作 StructuredOutput 工具（暫時骨架）：
    # - 真實行為會以 langchain-litellm 的結構化輸出或對應工具替換
    # - 這裡回傳一個最小可驗證物件，以滿足「成功即結束本輪」的行為
    def _call_structured_output(self, *, schema: dict, user_message: str) -> dict:
        self._log.info("structured.call", schema_keys=list(schema.keys()))
        # 簡易佔位輸出：實務上會依 schema 解析/驗證
        return {
            "tool": "StructuredOutput",
            "ok": True,
            "summary": user_message[:200],
            "schema_keys": list(schema.keys()),
        }

    # T011：事件/parts 寫盤（函式簽名與日誌佔位，後續以持久化替換）
    def write_event_start(self, *, session_id: str, agent_id: str) -> None:
        self._log.info("event.start", session_id=session_id, agent_id=agent_id)
        self._audit(action="event.start", session_id=session_id, details={"agent_id": agent_id})
        self._write_part(session_id=session_id, type_="start", payload={"agent_id": agent_id})

    def write_event_step_start(self, *, session_id: str) -> None:
        self._log.info("event.step_start", session_id=session_id)
        self._audit(action="event.step_start", session_id=session_id, details={})
        self._write_part(session_id=session_id, type_="step-start", payload={})

    def write_event_reasoning(self, *, session_id: str, delta: str) -> None:
        self._log.info("event.reasoning", session_id=session_id, delta_len=len(delta))
        self._audit(action="event.reasoning", session_id=session_id, details={"len": len(delta)})
        self._write_part(session_id=session_id, type_="reasoning", payload={"len": len(delta)})

    def write_event_text(self, *, session_id: str, delta: str) -> None:
        self._log.info("event.text", session_id=session_id, delta_len=len(delta))
        self._audit(action="event.text", session_id=session_id, details={"len": len(delta)})
        self._write_part(session_id=session_id, type_="text", payload={"delta": delta})

    def write_event_tool_input(self, *, session_id: str, tool: str, payload: dict) -> None:
        self._log.info("event.tool_input", session_id=session_id, tool=tool, keys=list(payload.keys()))
        self._audit(action="event.tool_input", session_id=session_id, details={"tool": tool, "keys": list(payload.keys())})
        self._write_part(session_id=session_id, type_="tool_input", payload={"tool": tool, "keys": list(payload.keys())})
        # T040：檢測 doom loop
        if self._check_doom_loop(session_id=session_id, tool=tool, payload=payload):
            # 記錄並結束本輪
            self.write_event_tool_error(session_id=session_id, tool=tool, error="doom_loop_denied")
            self.write_event_step_finish(session_id=session_id)
            self.write_event_finish(session_id=session_id)
            return

    def write_event_tool_result(self, *, session_id: str, tool: str, result: dict) -> None:
        self._log.info("event.tool_result", session_id=session_id, tool=tool, keys=list(result.keys()))
        self._audit(action="event.tool_result", session_id=session_id, details={"tool": tool, "keys": list(result.keys())})
        self._write_part(session_id=session_id, type_="tool_result", payload={"tool": tool, "keys": list(result.keys())})

    def write_event_tool_error(self, *, session_id: str, tool: str, error: str) -> None:
        self._log.error("event.tool_error", session_id=session_id, tool=tool, error=error)
        self._audit(action="event.tool_error", session_id=session_id, details={"tool": tool, "error": error})
        self._write_part(session_id=session_id, type_="tool_error", payload={"tool": tool, "error": error})

    def write_event_step_finish(self, *, session_id: str) -> None:
        self._log.info("event.step_finish", session_id=session_id)
        self._audit(action="event.step_finish", session_id=session_id, details={})
        self._write_part(session_id=session_id, type_="step-finish", payload={})

    def write_event_finish(self, *, session_id: str) -> None:
        self._log.info("event.finish", session_id=session_id)
        self._audit(action="event.finish", session_id=session_id, details={})
        self._write_part(session_id=session_id, type_="finish", payload={})

    # 以 Log 表記錄審計（若提供 DB）
    def _audit(self, *, action: str, session_id: str, details: dict[str, Any]) -> None:
        if not hasattr(self, "_db") or self._db is None:
            return
        try:
            resource_uuid = uuid.UUID(session_id)
        except Exception:
            # 若非 UUID，跳過持久化，僅保留日誌
            return
        log = Log(
            user_id=None,
            level='info',
            action=action,
            resource_type='conversation',
            resource_id=resource_uuid,
            details=details,
            ip_address=None,
        )
        self._db.add(log)
        self._db.commit()

    # T018：錯誤分類與對應訊息
    def _classify_error(self, e: Exception) -> str:
        msg = str(e).lower()
        if "auth" in msg or "unauthor" in msg or "forbidden" in msg:
            return "provider_auth_error"
        if "timeout" in msg or "econnreset" in msg or "temporar" in msg or "retry" in msg:
            return "network_retryable_error"
        if "context overflow" in msg or "max token" in msg or "too long" in msg:
            return "context_overflow"
        return "unknown_error"

    def _friendly_error_message(self, reason: str) -> str:
        if reason == "provider_auth_error":
            return "抱歉，模型授權設定有誤，請稍後再試或聯絡管理員。"
        if reason == "network_retryable_error":
            return "抱歉，網路暫時異常，已嘗試重試仍失敗，請稍後再試。"
        if reason == "context_overflow":
            # T043：在溢出情況下建立壓縮任務（於 step-finish 前）
            # 注意：此處僅建立任務與審計記錄；實際處理建議由外部迭代/runner 執行
            try:
                # 這裡無 session_id，因此在呼叫端（except 區塊）觸發 _create_compaction_task
                pass
            except Exception:
                pass
            return "抱歉，此次內容超過可處理長度，請精簡後再試。"
        return "抱歉，系統暫時無法回應，請稍後再試。"

    # T040：Doom loop 檢測與授權詢問
    def _check_doom_loop(self, *, session_id: str, tool: str, payload: dict) -> bool:
        norm = self._normalize_tool_input(payload)
        self._recent_tool_calls.append((tool, norm))

        # 計算最近連續出現相同工具+輸入的次數
        same = 0
        for t, n in reversed(self._recent_tool_calls):
            if t == tool and n == norm:
                same += 1
            else:
                break
        if same < max(1, settings.DOOM_LOOP_THRESHOLD):
            return False

        # 觸發授權詢問（規格：ask('doom_loop', patterns=[tool])）
        self._log.warning("doom_loop.detected", session_id=session_id, tool=tool, repeats=same)
        try:
            # 本服務為同步，暫以 best-effort 呼叫；實務上建議外層 loop runner 以 async 模式處理
            import asyncio

            async def _ask():
                await self._perm.ask(session_id=session_id, permission="doom_loop", patterns=[tool])

            asyncio.run(_ask())
            # 若無例外，視為允許繼續（具體決策交由 ask UI/規則）
            return False
        except NotImplementedError:
            # 權限互動尚未實作：為安全起見，直接停止本輪
            return True
        except RuntimeError:
            # 若 asyncio 事件迴圈衝突，記錄並採保守停止策略
            self._log.warning("doom_loop.ask_failed_runtime", session_id=session_id, tool=tool)
            return True
        except Exception as e:
            self._log.error("doom_loop.ask_failed", error=str(e))
            return True

    def _normalize_tool_input(self, payload: dict) -> str:
        try:
            # 以鍵排序後序列化；避免值順序導致的等價不等
            import json

            return json.dumps(payload, sort_keys=True, ensure_ascii=False)
        except Exception:
            return str(payload)

    # T043：建立/處理壓縮任務（僅審計與 API 佔位，實際壓縮流程由後續迭代處理）
    def _create_compaction_task(self, *, session_id: str, overflow: bool) -> None:
        # 使用 WARNING 提升可見度，確保測試與運行期都能被捕捉
        self._log.warning("compaction.created", session_id=session_id, auto=True, overflow=overflow)
        self._audit(action="compaction.created", session_id=session_id, details={"auto": True, "overflow": overflow})
        self._write_part(session_id=session_id, type_="compaction.created", payload={"auto": True, "overflow": overflow})

    def process_compaction(self, *, session_id: str) -> None:
        # 佔位：真實實作應壓縮歷史訊息以降低上下文
        self._log.info("compaction.processed", session_id=session_id)
        self._audit(action="compaction.processed", session_id=session_id, details={})
        self._write_part(session_id=session_id, type_="compaction.processed", payload={})

    def _write_part(self, *, session_id: str, type_: str, payload: dict) -> None:
        if not hasattr(self, "_db") or self._db is None:
            return
        try:
            conv_uuid = uuid.UUID(session_id)
        except Exception:
            return
        try:
            part = EventPart(conversation_id=conv_uuid, type=type_, payload=payload)
            self._db.add(part)
            self._db.commit()
        except Exception as e:
            # 若資料表不存在或寫入失敗，僅記錄並不中斷流程
            self._log.warning("event_part.write_failed", error=str(e), type=type_)
