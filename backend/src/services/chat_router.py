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
from src.models import Log
from src.models.events import EventPart
from src.core.config import settings
from src.services.permission_service import PermissionService


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
            for delta in self._llm.stream_complete(prompt=user_message, tier=tier):
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
