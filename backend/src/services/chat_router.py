"""
聊天路由（骨架）

職責：
- 組織單輪回合（single-turn）流程：彙整歷史 → 呼叫 LLMClient → 回傳助理回覆
- 後續將擴充事件/parts 寫盤、工具呼叫、結構化輸出等能力
"""

from __future__ import annotations

from typing import Optional, List, Any, Tuple
from datetime import datetime
from decimal import Decimal
import uuid
from collections import deque
import os
import shlex
import subprocess
import json as _json
import re

from src.services.llm_client import LLMClient
from src.core.logging import get_logger
from sqlalchemy.orm import Session
from src.models import Log, Agent, MCPConnection, SkillEntry, FunctionProfile
from src.models.events import EventPart
from src.core.config import settings
from src.services.permission_service import PermissionService
from src.services.mcp_client import MCPClient
from src.services.react_synthesis import ReActSynthesis
from src.services.skill_executor import execute_skill
from src.services.skill_interaction_service import SkillInteractionService
from src.services.tool_policy_service import ToolPolicyService, ToolPolicyDecision


DEFAULT_LAST_METRICS = {
    "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
    "cost": 0.0,
}

_log = get_logger("services.chat_router")

class ChatRouter:
    """單輪聊天管線骨架。"""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self._llm = llm or LLMClient()
        self._log = get_logger("services.chat_router")
        # 最近一次回合的度量（供外部查詢）
        self._last_metrics: dict | None = None
        # 結構化輸出模式（T017 預留）
        self._structured_mode: dict | None = None  # {"schema": {...}}
        self._last_structured: dict | None = None
        # Doom loop 檢測：保存最近工具呼叫紀錄 (tool, normalized_input)
        self._recent_tool_calls: deque[Tuple[str, str]] = deque(maxlen=32)
        self._perm = PermissionService()
        self._mcp = MCPClient()
        # 記錄工具開始時間以計算耗時
        self._tool_start_times: dict[tuple[str, str], datetime] = {}
        self._mcp_tool_name_cache: dict[str, str] = {}
        # 每回合可覆蓋的工具呼叫協定模板
        self._custom_toolcall_guide: Optional[str] = None
        self._function_profile_template: Optional[str] = None
        self._tool_policy_service = ToolPolicyService()
        self._current_user_id: str = ''
        self._current_agent_id: str = ''
        self._current_conversation_id: str = ''
        self._current_agent_class: str = ''
        self._active_tool_policy_decisions: dict[tuple[str, str], ToolPolicyDecision] = {}

    def set_execution_context(self, *, user_id: str | None, agent_id: str | None, conversation_id: str | None) -> None:
        self._current_user_id = str(user_id or '').strip()
        self._current_agent_id = str(agent_id or '').strip()
        self._current_conversation_id = str(conversation_id or '').strip()

    def _strip_policy_runtime_fields(self, payload: dict) -> dict:
        # 目的：移除策略確認流程的內部欄位後再交給實際工具。
        # 為什麼：避免一次性 token 或控制旗標被傳入外部工具與事件紀錄。
        source_payload = payload if isinstance(payload, dict) else {}
        internal_fields = {'_policy_confirm_token', '_policy_confirmed', '_confirmed'}
        return {
            key: value
            for key, value in source_payload.items()
            if str(key) not in internal_fields
        }

    def _resolve_mcp_tool_name(self, *, conn_name: str, conn: dict[str, Any]) -> str:
        """目的：解析 MCP 連線名稱對應的實際工具名稱。
        為什麼：部分 MCP server 的工具名稱不等於連線名稱，若直接以連線名稱呼叫會出現 Unknown tool。
        """
        normalized_conn_name = str(conn_name or "").strip()
        if not normalized_conn_name:
            return normalized_conn_name
        cached_name = self._mcp_tool_name_cache.get(normalized_conn_name)
        if isinstance(cached_name, str) and cached_name.strip():
            return cached_name.strip()

        resolved_name = normalized_conn_name
        transport = str((conn or {}).get("transport") or "remote").strip() or "remote"
        try:
            discovered_tools: list[dict[str, Any]] = []
            if transport == "stdio":
                command = str((conn or {}).get("command") or "").strip()
                args = (conn or {}).get("args") if isinstance((conn or {}).get("args"), list) else []
                env = (conn or {}).get("env") if isinstance((conn or {}).get("env"), dict) else {}
                discovered_tools = self._mcp.discover_tools_stdio(command=command, args=args, env=env)
            else:
                base_url = str((conn or {}).get("base_url") or "").strip()
                auth = (conn or {}).get("auth") if isinstance((conn or {}).get("auth"), dict) else None
                if base_url:
                    discovered_tools = self._mcp.list_tools(base_url=base_url, auth=auth)

            if discovered_tools:
                selected_tool = self._mcp.select_tool_schema(tools=discovered_tools, preferred_name=normalized_conn_name)
                selected_entry = (selected_tool or {}).get("tool") if isinstance(selected_tool, dict) else None
                selected_tool_name = str((selected_entry or {}).get("name") or "").strip()
                if selected_tool_name:
                    resolved_name = selected_tool_name
        except Exception as error:
            self._log.warning("mcp.resolve_tool_name.failed", conn_name=normalized_conn_name, error=str(error))

        self._mcp_tool_name_cache[normalized_conn_name] = resolved_name
        return resolved_name

    def _normalize_mcp_arguments(self, payload: dict) -> dict:
        """目的：盡量將常見字串參數修復為合法 JSON 型別。
        為什麼：模型偶爾把陣列寫成字串或函式樣式，會造成 MCP 工具參數型別錯誤。
        """
        if not isinstance(payload, dict):
            return {}

        normalized: dict[str, Any] = {}
        for key, value in payload.items():
            if not isinstance(value, str):
                normalized[key] = value
                continue

            text = value.strip()
            repaired: Any = value

            # Case 1: 直接是 JSON 字串（如 "[1,2,3]"）
            if text.startswith('[') or text.startswith('{'):
                try:
                    repaired = _json.loads(text)
                    normalized[key] = repaired
                    continue
                except Exception:
                    pass

            # Case 2: 函式樣式（如 get_hot_news([1,3,5,6])）
            func_like = re.match(r'^[A-Za-z_][A-Za-z0-9_]*\((.*)\)$', text)
            if func_like:
                inner = (func_like.group(1) or '').strip()
                try:
                    repaired = _json.loads(inner)
                    normalized[key] = repaired
                    continue
                except Exception:
                    list_like = re.search(r'(\[[\s\S]*\])', inner)
                    if list_like:
                        try:
                            repaired = _json.loads(list_like.group(1))
                            normalized[key] = repaired
                            continue
                        except Exception:
                            pass

            # Case 3: 字串中包含陣列片段
            list_like = re.search(r'(\[[\s\S]*\])', text)
            if list_like:
                try:
                    repaired = _json.loads(list_like.group(1))
                    normalized[key] = repaired
                    continue
                except Exception:
                    pass

            normalized[key] = value

        return normalized

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

        # 確保後續工具呼叫可使用資料庫（避免 skill_backend_not_available）
        self._set_db_if_present(db)
        agent_ctx = self._prepare_integrations(db=db, agent_id=agent_id)

        # 嘗試自動判別是否需要呼叫工具（Skills 或 MCP）
        try:
            auto_text = self._auto_select_and_call(session_id=session_id, user_message=user_message, agent_ctx=agent_ctx, tier=tier, db=db, agent_id=agent_id)
            if isinstance(auto_text, str) and auto_text.strip():
                # 正常結束事件
                self.write_event_step_finish(session_id=session_id)
                self.write_event_finish(session_id=session_id)
                return auto_text
        except Exception as _e:
            self._log.warning("auto_tool.selection_failed", error=str(_e))

        # 若啟用 RAG，嘗試構建最小檢索上下文（目前無嵌入/檢索，先提供來源標籤提示）
        rag_prefix, refs_block = self._build_rag_hints(agent_ctx)

        # 為模型加入能力提示前綴，讓回覆能考量可用能力（即使目前為骨架）
        prefix = self._build_capability_prefix(agent_ctx, rag_prefix)
        guide = self._render_toolcall_guide(agent_ctx)
        composed_user_message = f"{prefix}{guide}\n{user_message}"

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
            self._last_metrics = dict(DEFAULT_LAST_METRICS)
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
        self._last_metrics = self._estimate_metrics(output_text)

        # 若啟用「無可用路由即硬失敗」，且本輪為降級/骨架輸出，則回傳錯誤（不寫助理訊息）
        is_fallback = self._is_fallback_route()
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

    # 結構重構：以下 helper 僅抽取重複流程，不改變既有行為。
    def _set_db_if_present(self, db: Optional[Session]) -> None:
        try:
            if db is not None:
                self._db = db
        except Exception:
            pass

    def _build_rag_hints(self, agent_ctx: dict[str, Any]) -> tuple[str, str]:
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
        return rag_prefix, refs_block

    def _build_capability_prefix(self, agent_ctx: dict[str, Any], rag_prefix: str) -> str:
        mcp_names = ",".join(
            [str(c.get("name") or "").strip() for c in agent_ctx.get("mcp", []) if isinstance(c, dict) and c.get("enabled")]
        )
        skills_names = ",".join(agent_ctx.get("skills", []))
        return f"[Agent Capabilities] skills={skills_names} mcp={mcp_names}{rag_prefix}\n"

    def _estimate_metrics(self, output_text: str) -> dict[str, Any]:
        approx_tokens = max(1, len(output_text) // 4)
        return {
            "tokens": {"input": 0, "output": approx_tokens, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "cost": 0.0,
        }

    def _is_fallback_route(self) -> bool:
        try:
            info = self._llm.last_route_info()
            return (not info) or bool(info.get("fallback")) or (str(info.get("provider") or "") in {"", "fallback"})
        except Exception:
            return False

    def _auto_select_and_call(self, *, session_id: str, user_message: str, agent_ctx: dict[str, Any], tier: Optional[str], db: Optional[Session] = None, agent_id: Optional[str] = None) -> Optional[str]:
        """使用 ReAct 合成層：規劃工具呼叫、執行、再輸出可讀答案。"""
        try:
            skills = [s for s in (agent_ctx.get("skills", []) or []) if isinstance(s, str) and s.strip()]
            mcps = []
            for c in (agent_ctx.get("mcp", []) or []):
                if isinstance(c, dict) and c.get("enabled") and c.get("name"):
                    mcps.append(f"mcp:{str(c.get('name')).strip()}")
            allowed = skills + mcps
            if not allowed:
                return None

            # Why: ReAct tool path 仍需共用 DB 寫盤與技能查詢能力。
            self._set_db_if_present(db)

            synthesis = ReActSynthesis(
                llm_complete=lambda prompt, t: self._llm.complete(prompt=prompt, tier=t),
                call_tool=lambda tool_name, payload: self.call_tool(
                    session_id=session_id,
                    tool=tool_name,
                    payload=payload,
                ),
                max_steps=getattr(settings, "REACT_MAX_STEPS", 3),
                max_observation_chars=getattr(settings, "REACT_MAX_OBSERVATION_CHARS", 4000),
            )
            out = synthesis.run(user_message=user_message, allowed_tools=allowed, tier=tier)
            # 將 ReAct 追蹤寫入事件流，便於前端/除錯觀察規劃流程
            try:
                trace = synthesis.last_trace()
                if trace:
                    self._write_part(
                        session_id=session_id,
                        type_="react.trace",
                        payload={"trace": trace[:20]},
                    )
            except Exception:
                pass
            return out
        except Exception:
            return None

    def _prepare_integrations(self, *, db: Optional[Session], agent_id: str) -> dict[str, Any]:
        agent_ctx: dict[str, Any] = {"skills": [], "mcp": [], "rag": {"enabled": False, "sources": [], "topK": 5}}
        self._function_profile_template = None
        try:
            if db is not None:
                ag = db.query(Agent).filter(Agent.id == agent_id).first()
                if ag is not None:
                    self._current_agent_class = str(getattr(ag, 'agent_class', '') or '').strip()
                    # 讀取 skills 示例參數（來自 model_config.skill_examples）
                    self._skill_examples = {}
                    try:
                        cfg = ag.model_config or {}
                        if isinstance(cfg, dict) and isinstance(cfg.get("skill_examples"), dict):
                            self._skill_examples = cfg.get("skill_examples") or {}
                    except Exception:
                        self._skill_examples = {}
                    # 1) 以 id 參照的 MCP：從全域表解析
                    mcp_list: list[dict] = []
                    try:
                        ids = []
                        if isinstance(getattr(ag, 'model_config', None), dict):
                            ids = list((ag.model_config or {}).get('mcp_ids') or [])
                        if ids:
                            rows = db.query(MCPConnection).filter(MCPConnection.id.in_(ids)).all()
                            for r in rows:
                                if not bool(r.enabled):
                                    continue
                                mcp_list.append({
                                    'name': r.name,
                                    'enabled': True,
                                    'transport': r.transport,
                                    'base_url': r.base_url,
                                    'auth': r.auth,
                                    'progress_field': r.progress_field,
                                    'eta_field': r.eta_field,
                                    'command': r.command,
                                    'args': r.args,
                                    'env': r.env,
                                    'input_schema': getattr(r, 'input_schema', {}) or {},
                                })
                    except Exception:
                        pass
                    # 2) 舊版/自訂 MCP：僅在未使用 mcp_ids（無 registry 參照）時才採用 inline
                    try:
                        using_ids = bool((getattr(ag, 'model_config', {}) or {}).get('mcp_ids'))
                    except Exception:
                        using_ids = False
                    if not using_ids:
                        try:
                            mcp_raw = ag.mcp_config or []
                            if isinstance(mcp_raw, dict):
                                mcp_inline = list(mcp_raw.values())
                            elif isinstance(mcp_raw, list):
                                mcp_inline = mcp_raw
                            else:
                                mcp_inline = []
                            # 以 name 作為 key 合併（inline 覆蓋 registry）
                            merged: dict[str, dict] = {}
                            for c in mcp_list:
                                if isinstance(c, dict) and c.get('name'):
                                    merged[str(c.get('name'))] = dict(c)
                            for c in mcp_inline:
                                if isinstance(c, dict) and c.get('name'):
                                    merged[str(c.get('name'))] = dict(c)
                            mcp_list = [v for v in merged.values() if v.get('enabled')]
                        except Exception:
                            pass
                    agent_ctx["mcp"] = [c for c in (mcp_list or []) if isinstance(c, dict) and c.get("enabled")]

                    # Skills：合併 id 參照與字串名單
                    skills_names: list[str] = []
                    try:
                        if isinstance(getattr(ag, 'model_config', None), dict):
                            sids = list((ag.model_config or {}).get('skill_ids') or [])
                            if sids:
                                rows = db.query(SkillEntry).filter(SkillEntry.id.in_(sids)).all()
                                # 準備 schema 映射
                                self._skill_schemas = {}
                                for r in rows:
                                    if bool(r.enabled) and isinstance(r.name, str) and r.name.strip():
                                        skills_names.append(r.name.strip())
                                        try:
                                            if isinstance(getattr(r, 'input_schema', None), dict):
                                                self._skill_schemas[r.name.strip()] = r.input_schema
                                        except Exception:
                                            pass
                    except Exception:
                        pass
                    # 永遠合併 agent.skills（歷史相容），與 _extract_agent_skill_names 邏輯一致
                    # 若 skill_ids 已有的名稱重複，dict.fromkeys 去重會保留第一筆
                    try:
                        for s in (ag.skills or []):
                            if isinstance(s, str) and s.strip():
                                skills_names.append(s.strip())
                    except Exception:
                        pass
                    # 去重
                    agent_ctx["skills"] = list(dict.fromkeys(skills_names).keys())
                    rc = ag.rag_config or {}
                    agent_ctx["rag"] = {
                        "enabled": bool(rc.get("enabled", False)),
                        "sources": list(rc.get("sources", []) or []),
                        "topK": int(rc.get("topK", 5) or 5),
                    }
                    try:
                        cfg = ag.model_config if isinstance(ag.model_config, dict) else {}
                        fid = str((cfg or {}).get('function_profile_id') or '').strip()
                        if not fid and getattr(ag, 'function_profile_id', None) is not None:
                            fid = str(ag.function_profile_id)
                        if fid:
                            fp = db.query(FunctionProfile).filter(FunctionProfile.id == fid, FunctionProfile.enabled == True).first()  # noqa: E712
                            if fp and isinstance(fp.template, str) and fp.template.strip():
                                self._function_profile_template = fp.template.strip()
                    except Exception:
                        self._function_profile_template = None
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

    def _toolcall_guide(self) -> str:
        try:
            if not getattr(settings, "LLM_TOOLCALL_GUIDE", True):
                return ""
        except Exception:
            pass
        # 會話級覆蓋
        if isinstance(self._custom_toolcall_guide, str) and self._custom_toolcall_guide.strip():
            return "\n" + self._custom_toolcall_guide.strip() + "\n"
        if isinstance(self._function_profile_template, str) and self._function_profile_template.strip():
            return "\n" + self._function_profile_template.strip() + "\n"
        # 預設模板：簡短且機械可解析，避免模型誤觸
        return (
            "\n[Tool-Call Protocol]\n"
            "- Only if a tool is REQUIRED, output EXACTLY this format and nothing else before it:\n"
            "  [[CALL tool=<allowed-tool-name>]]\n{{EXAMPLE_ARGS}}\n"
            "- <allowed-tool-name> must be one of: {{ALLOWED_TOOL_NAMES}}\n"
            "- The JSON body are the arguments for the tool.\n"
            "- Otherwise, answer normally without the CALL block.\n"
            "\n[Allowed Tools]\n"
            "Skills:\n{{SKILL_TOOLS_BLOCK}}\n"
            "MCP:\n{{MCP_TOOLS_BLOCK}}\n"
            "\n[Schemas]\n"
            "- Input JSON schema hints for skills (if any):\n{{SCHEMA_BLOCK}}\n"
            "\n[RAG Context]\n"
            "- Enabled: {{RAG_ENABLED}}\n"
            "- Sources: {{RAG_SOURCES}}\n"
            "- topK: {{RAG_TOPK}}\n"
            "{{RAG_HINTS}}\n"
            "\n{{USAGE_HINTS}}\n"
        )

    def _render_toolcall_guide(self, agent_ctx: dict[str, Any]) -> str:
        """將模板中的變數替換成當前代理者可用工具清單與示例參數。"""
        raw = self._toolcall_guide() or ""
        if not raw:
            return ""
        try:
            skills = [s for s in (agent_ctx.get("skills", []) or []) if isinstance(s, str) and s.strip()]
            mcps = []
            for c in (agent_ctx.get("mcp", []) or []):
                if isinstance(c, dict) and c.get("enabled") and c.get("name"):
                    mcps.append(f"mcp:{str(c.get('name')).strip()}")
            allowed = skills + mcps
            allowed_names = ", ".join(allowed) if allowed else "<none>"
            # 簡化：不再依賴 example_args，固定以 {} 作為示例
            import json as _json
            example_args = _json.dumps({}, ensure_ascii=False, indent=2)
            examples_block = "\n".join([f"- {n}: {{}}" for n in allowed]) if allowed else "- <none>"
            # Schema block：僅針對 skills，若有 input_schema 則輸出其 JSON；否則 {}
            schema_lines = []
            try:
                m: dict[str, dict] = getattr(self, "_skill_schemas", {}) if hasattr(self, "_skill_schemas") else {}
                for s in skills:
                    sch = m.get(s) if isinstance(m, dict) else None
                    import json as _json
                    if isinstance(sch, dict) and sch:
                        schema_lines.append(f"- {s}: " + _json.dumps(sch, ensure_ascii=False))
                    else:
                        schema_lines.append(f"- {s}: {{}}")
            except Exception:
                pass
            schema_block = "\n".join(schema_lines) if schema_lines else "- <none>"
            # 進行替換
            out = raw
            out = out.replace("{{ALLOWED_TOOL_NAMES}}", allowed_names)
            out = out.replace("{{SKILL_NAMES}}", ", ".join(skills) if skills else "<none>")
            out = out.replace("{{MCP_NAMES}}", ", ".join(mcps) if mcps else "<none>")
            out = out.replace("{{EXAMPLE_ARGS}}", example_args)
            if "{{EXAMPLE_ARGS_BLOCK}}" in out:
                out = out.replace("{{EXAMPLE_ARGS_BLOCK}}", examples_block)
            # 區塊形式的工具清單
            if "{{ALLOWED_TOOLS_BLOCK}}" in out:
                block = "\n".join([f"- {n}" for n in allowed]) if allowed else "- <none>"
                out = out.replace("{{ALLOWED_TOOLS_BLOCK}}", block)
            # 分欄清單：Skills / MCP
            skill_block = "\n".join([f"- {s}" for s in skills]) if skills else "- <none>"
            mcp_block = "\n".join([f"- {m}" for m in mcps]) if mcps else "- <none>"
            out = out.replace("{{SKILL_TOOLS_BLOCK}}", skill_block)
            out = out.replace("{{MCP_TOOLS_BLOCK}}", mcp_block)
            out = out.replace("{{SCHEMA_BLOCK}}", schema_block)
            # RAG 變數
            try:
                rc = agent_ctx.get("rag", {}) or {}
                rag_enabled = bool(rc.get("enabled", False))
                rag_sources = ", ".join(rc.get("sources", []) or []) if rag_enabled else "<disabled>"
                rag_topk = str(int(rc.get("topK", 5) or 5)) if rag_enabled else "<disabled>"
                rag_hints = (
                    "[RAG 說明]\n"
                    "- 問題與知識檢索高度相關時，先檢索再作答，並於回覆中引用來源或摘要。\n"
                    "- 若查無結果，請明確說明並僅根據可得上下文作答（避免捏造）。\n"
                ) if rag_enabled else ""
                out = out.replace("{{RAG_ENABLED}}", "true" if rag_enabled else "false")
                out = out.replace("{{RAG_SOURCES}}", rag_sources if rag_sources else "<none>")
                out = out.replace("{{RAG_TOPK}}", rag_topk)
                out = out.replace("{{RAG_HINTS}}", rag_hints)
            except Exception:
                out = out.replace("{{RAG_ENABLED}}", "false")
                out = out.replace("{{RAG_SOURCES}}", "<none>")
                out = out.replace("{{RAG_TOPK}}", "<n/a>")
                out = out.replace("{{RAG_HINTS}}", "")
            # 使用說明（繁中）
            hints = (
                "[使用說明]\n"
                "- 回覆語言一律使用繁體中文。\n"
                "- 不可輸出任何中間推理、規劃、檢查或自我對話內容。\n"
                "- 僅在確定需要呼叫工具時再輸出 CALL 區塊；否則請以自然語言作答。\n"
                "- 一旦輸出 CALL 區塊，前後不可夾帶其他說明文字。\n"
                "- MCP 工具名稱請使用 'mcp:<name>' 的精確字串。\n"
                "- 參數務必為合法 JSON（鍵為字串、無註解、逗號位置正確）。\n"
                "- 僅可使用上方允許清單中的工具名稱。\n"
            )
            out = out.replace("{{USAGE_HINTS}}", hints)
            return out
        except Exception:
            return raw

    def set_toolcall_guide(self, guide: Optional[str]) -> None:
        self._custom_toolcall_guide = guide if isinstance(guide, str) else None

    async def call_tool_async(self, *, session_id: str, tool: str, payload: dict, db: Optional[Session] = None, agent_id: Optional[str] = None) -> dict:
        """非同步工具呼叫入口。

        Why: 將 SSE 串流與同步工具邏輯橋接在同一入口，避免路由層分散處理白名單與審計。
        """
        _log.debug("Start call_tool_async.request", tool=tool, session_id=session_id)
        name, early = self._validate_async_tool_call_request(
            session_id=session_id,
            tool=tool,
            payload=payload,
            db=db,
            agent_id=agent_id,
        )
        if early is not None:
            return early
        sanitized_payload = self._strip_policy_runtime_fields(payload or {})

        # 僅在 MCP 工具時優先走 WS JSON-RPC 串流（stdio 模式則直接走同步）
        if name.startswith("mcp:"):
            _log.debug("call_tool_async.mcp_attempt", tool=name, session_id=session_id)

            conn_name = name.split(":", 1)[1]
            conn = getattr(self, "_mcp_map", {}).get(conn_name)
            if not conn:
                self.write_event_tool_error(session_id=session_id, tool=name, error="mcp_connection_not_found")
                return {"ok": False, "error": "mcp_connection_not_found"}
            transport = str(conn.get("transport") or "remote").strip() or "remote"
            target_tool_name = self._resolve_mcp_tool_name(conn_name=conn_name, conn=conn)
            if transport == "stdio":
                # stdio：走持久會話串流（含 initialize），避免單次 invoke 缺少握手造成 timeout
                try:
                    cmd = str(conn.get("command") or "").strip()
                    args = conn.get("args") if isinstance(conn.get("args"), list) else []
                    env = conn.get("env") if isinstance(conn.get("env"), dict) else {}
                    async for frame in self._mcp.stream_rpc_call_stdio(
                        command=cmd,
                        args=args,
                        env=env,
                        method="tools/call",
                        params={"name": target_tool_name, "arguments": self._normalize_mcp_arguments(sanitized_payload)},
                    ):
                        if isinstance(frame, dict) and frame.get("ok") is True:
                            self.write_event_tool_result(session_id=session_id, tool=name, result=frame)
                            return {"ok": True, "result": frame.get("result", frame)}
                        if isinstance(frame, dict) and frame.get("ok") is False:
                            err = str(frame.get("error") or "tool_failed")
                            self.write_event_tool_error(session_id=session_id, tool=name, error=err)
                            return {"ok": False, "error": err}
                except Exception as e:
                    self.write_event_tool_error(session_id=session_id, tool=name, error=str(e))
                    return {"ok": False, "error": str(e)}
                return {"ok": False, "error": "mcp_stdio_no_terminal_frame"}
            base_url = str(conn.get("base_url") or "").strip()
            auth = conn.get("auth") if isinstance(conn, dict) else None
            if not base_url:
                self.write_event_tool_error(session_id=session_id, tool=name, error="mcp_invalid_base_url")
                return {"ok": False, "error": "mcp_invalid_base_url"}
            # 嘗試 WS 串流呼叫
            try:
                async for frame in self._mcp.stream_rpc_call_ws(base_url=base_url, method="tools/call", params={"name": target_tool_name, "arguments": self._normalize_mcp_arguments(sanitized_payload)}, auth=auth if isinstance(auth, dict) else None):
                    # 可在此寫入逐段事件，先保留最小行為：若拿到 result 即成功
                    if isinstance(frame, dict) and ("result" in frame or frame.get("ok") is True):
                        self.write_event_tool_result(session_id=session_id, tool=name, result=frame)
                        return {"ok": True, "result": frame.get("result", frame)}
                    if isinstance(frame, dict) and ("error" in frame or frame.get("ok") is False):
                        # 結束於錯誤
                        self.write_event_tool_error(session_id=session_id, tool=name, error=str(frame.get("error")))
                        return {"ok": False, "error": str(frame.get("error"))}
            except Exception as e:
                # WS 不可用或失敗時，落回同步 HTTP 邏輯
                self._log.warning("mcp.ws.stream_failed_fallback_http", error=str(e))
                return self.call_tool(session_id=session_id, tool=name, payload=sanitized_payload, skip_precheck=True)

        _log.debug("call_tool", tool=name, session_id=session_id)
        # 其他情況沿用同步路徑
        return self.call_tool(session_id=session_id, tool=name, payload=sanitized_payload, skip_precheck=True)

    def _validate_async_tool_call_request(
        self,
        *,
        session_id: str,
        tool: str,
        payload: dict,
        db: Optional[Session],
        agent_id: Optional[str],
    ) -> tuple[str, Optional[dict]]:
        """統一非同步工具請求前置檢查。

        Why: 保持 call_tool_async 主流程聚焦於執行路徑，降低重複邏輯維護成本。
        """
        if agent_id and db is not None:
            self._prepare_integrations(db=db, agent_id=agent_id)
        self._set_db_if_present(db)

        name = (tool or "").strip()
        if not name:
            self.write_event_tool_error(session_id=session_id, tool="<empty>", error="invalid_tool")
            return name, {"ok": False, "error": "invalid_tool"}
        if name not in getattr(self, "_allowed_tools", set()):
            self.write_event_tool_error(session_id=session_id, tool=name, error="tool_not_allowed")
            return name, {"ok": False, "error": "tool_not_allowed"}

        policy_error = self._evaluate_tool_policy_before_execute(session_id=session_id, tool=name, payload=payload or {})
        if policy_error is not None:
            return name, policy_error

        sanitized_payload = self._strip_policy_runtime_fields(payload or {})
        self.write_event_tool_input(session_id=session_id, tool=name, payload=sanitized_payload)
        if self._check_doom_loop(session_id=session_id, tool=name, payload=sanitized_payload):
            return name, {"ok": False, "error": "doom_loop_denied"}
        return name, None

    def _maybe_handle_tool_call(self, *, session_id: str, text: str) -> str | None:
        marker = "[[CALL tool="
        idx = text.find(marker)
        if idx < 0:
            return self._maybe_handle_json_tool_calls(session_id=session_id, text=text)
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
            if not result.get("ok"):
                return f"抱歉，我嘗試使用工具「{tool_name}」但失敗：{result.get('error')}"

            # 工具成功後，要求模型根據工具結果輸出最終回覆（避免把 CALL 區塊原樣回給使用者）
            try:
                import json as _json
                tool_res = result.get("result", {})
                tool_res_s = _json.dumps(tool_res, ensure_ascii=False)
                # 取 CALL 之前可能的自然語言上下文（若有）
                preface = (text[:idx] or "").strip()
                prompt = (
                    "你是繁體中文助理。請根據以下工具結果，直接回覆使用者可讀答案。\n"
                    "規則：\n"
                    "1) 不可輸出 [[CALL ...]] 區塊\n"
                    "2) 不可提及內部協定或工具執行細節\n"
                    "3) 若資料不足要明確說明\n\n"
                    f"工具名稱：{tool_name}\n"
                    f"工具輸入：{_json.dumps(payload or {}, ensure_ascii=False)}\n"
                    f"工具輸出：{tool_res_s[:6000]}\n"
                    f"原始上下文：{preface[:1000]}\n\n"
                    "請直接給最終回答："
                )
                final = self._llm.complete(prompt=prompt)
                ans = (final or "").strip()
                if ans:
                    return ans
            except Exception:
                pass

            # 後備：至少回傳工具結果摘要（不回 CALL 區塊）
            try:
                import json as _json
                return f"已取得工具結果：{_json.dumps(result.get('result', {}), ensure_ascii=False)[:1200]}"
            except Exception:
                return "已取得工具結果。"
        except Exception:
            return None

    def _maybe_handle_json_tool_calls(self, *, session_id: str, text: str) -> str | None:
        """目的：解析並執行 JSON `tool_calls` 協定。
        為什麼：部分模型會輸出 OpenAI-style tool_calls，而非 [[CALL ...]] 標記。
        """
        raw = str(text or '').strip()
        if '"tool_calls"' not in raw:
            return None

        import json as _json

        start = raw.find('{')
        end = raw.rfind('}')
        if start < 0 or end <= start:
            return None

        try:
            obj = _json.loads(raw[start:end + 1])
        except Exception:
            return None

        tool_calls = obj.get('tool_calls') if isinstance(obj, dict) else None
        if not isinstance(tool_calls, list) or not tool_calls:
            return None

        executed: list[tuple[str, dict, dict]] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get('function') if isinstance(call.get('function'), dict) else {}
            tool_name = str(fn.get('name') or '').strip()
            if not tool_name:
                continue
            raw_args = fn.get('arguments')
            payload: dict = {}
            if isinstance(raw_args, dict):
                payload = raw_args
            elif isinstance(raw_args, str) and raw_args.strip():
                try:
                    loaded = _json.loads(raw_args)
                    if isinstance(loaded, dict):
                        payload = loaded
                except Exception:
                    payload = {}
            result = self.call_tool(session_id=session_id, tool=tool_name, payload=payload)
            if not result.get('ok'):
                return f"抱歉，我嘗試使用工具「{tool_name}」但失敗：{result.get('error')}"
            executed.append((tool_name, payload, result))

        if not executed:
            return None

        # 若最後一個工具已直接產生可讀文字，優先直接回覆
        last_result = executed[-1][2].get('result', {})
        text_output = self._extract_text_from_tool_result(last_result)
        if text_output:
            return text_output

        # 其餘情況交由模型整合工具輸出
        try:
            rows = []
            for idx, (tool_name, payload, result) in enumerate(executed, start=1):
                rows.append(
                    f"[{idx}] 工具：{tool_name}\n"
                    f"輸入：{_json.dumps(payload or {}, ensure_ascii=False)}\n"
                    f"輸出：{_json.dumps(result.get('result', {}), ensure_ascii=False)[:6000]}"
                )
            prompt = (
                "你是繁體中文助理。請根據以下工具結果，直接輸出最終可讀答案。\n"
                "規則：\n"
                "1) 不可輸出 tool_calls JSON\n"
                "2) 不可提及內部協定或工具執行細節\n"
                "3) 若資料不足要明確說明\n\n"
                f"{chr(10).join(rows)}\n\n"
                "請直接給最終回答："
            )
            final = self._llm.complete(prompt=prompt)
            ans = (final or '').strip()
            if ans:
                return ans
        except Exception:
            pass

        return None

    def _extract_text_from_tool_result(self, result: Any) -> str:
        """目的：從工具結果抽取最終文字。
        為什麼：skills 常直接回傳 text 欄位，應優先輸出避免二次總結失真。
        """
        if isinstance(result, str):
            return result.strip()
        if isinstance(result, dict):
            candidates = [
                result.get('text'),
                (result.get('result') or {}).get('text') if isinstance(result.get('result'), dict) else None,
                result.get('output'),
                result.get('content'),
            ]
            for item in candidates:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        return ''

    def _extract_interactive_skill_result(self, script_outputs: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        # 目的：從技能腳本輸出辨識互動模式結果。
        # 為什麼：HTML 技能需讓 scripts/main.js 直接驅動 ui/final/error，不經 LLM 二次改寫。
        outputs = script_outputs if isinstance(script_outputs, list) else []
        if not outputs:
            return None

        for output_item in reversed(outputs):
            if not isinstance(output_item, dict) or not bool(output_item.get('ok')):
                continue
            stdout_text = str(output_item.get('stdout') or '').strip()
            if not stdout_text:
                continue

            parsed_result: Any = None
            try:
                parsed_result = _json.loads(stdout_text)
            except Exception:
                json_start = stdout_text.find('{')
                json_end = stdout_text.rfind('}')
                if json_start >= 0 and json_end > json_start:
                    try:
                        parsed_result = _json.loads(stdout_text[json_start:json_end + 1])
                    except Exception:
                        parsed_result = None

            if not isinstance(parsed_result, dict):
                continue
            mode_text = str(parsed_result.get('mode') or '').strip().lower()
            if mode_text in {'ui', 'final', 'error'}:
                return parsed_result
        return None

    def _run_skill_command(self, *, command: str, payload: dict[str, Any], timeout_ms: int) -> dict:
        """目的：執行技能 command 並回傳工具結果格式。
        為什麼：executable 技能需支援 uvx/npx/java 等命令，避免僅能用 python handler。
        """
        _log.debug("run_skill_command", command=command, payload=payload, timeout_ms=timeout_ms)
        cmd_text = str(command or '').strip()
        if not cmd_text:
            return {"ok": False, "error": "empty_command"}
        try:
            args = shlex.split(cmd_text)
        except Exception as e:
            return {"ok": False, "error": f"invalid_command: {e}"}
        if not args:
            return {"ok": False, "error": "empty_command"}

        allowed_prefixes = {'uvx', 'npx', 'node', 'java', 'python', 'python3'}
        if args[0] not in allowed_prefixes:
            return {"ok": False, "error": f"command_not_allowed: {args[0]}"}

        env = os.environ.copy()
        env['SKILL_INPUT'] = _json.dumps(payload or {}, ensure_ascii=False)
        timeout_seconds = max(1, int((timeout_ms or 8000) / 1000))
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"command_timeout_{timeout_seconds}s"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        stdout = str(proc.stdout or '').strip()
        stderr = str(proc.stderr or '').strip()

        _log.debug("skill_command_completed", command=cmd_text, returncode=proc.returncode, stdout=stdout[:200], stderr=stderr[:200])
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": f"command_exit_{proc.returncode}",
                "stdout": stdout[:2000],
                "stderr": stderr[:2000],
            }

        if not stdout:
            return {"ok": True, "result": {"text": "", "return_code": 0}}
        try:
            data = _json.loads(stdout)
            return {"ok": True, "result": data}
        except Exception:
            return {"ok": True, "result": {"text": stdout[:6000], "return_code": 0}}

    # 公用：以白名單強制的工具呼叫（未來供工具規劃/LLM function call 整合）
    def call_tool(self, *, session_id: str, tool: str, payload: dict, skip_precheck: bool = False) -> dict:
        _log.debug("Start call_tool.request", tool=tool, session_id=session_id)

        name = (tool or '').strip()
        if not skip_precheck:
            name, early = self._validate_sync_tool_call_request(session_id=session_id, tool=tool, payload=payload)
            if early is not None:
                return early
        sanitized_payload = self._strip_policy_runtime_fields(payload or {})

        # MCP 工具：命名慣例 mcp:<conn-name>
        if name.startswith("mcp:"):
            conn_name = name.split(":", 1)[1]
            conn = self._mcp_map.get(conn_name)
            if not conn:
                self.write_event_tool_error(session_id=session_id, tool=name, error="mcp_connection_not_found")
                return {"ok": False, "error": "mcp_connection_not_found"}
            transport = str(conn.get("transport") or "remote").strip() or "remote"
            target_tool_name = self._resolve_mcp_tool_name(conn_name=conn_name, conn=conn)
            if transport == "stdio":
                try:
                    cmd = str(conn.get("command") or "").strip()
                    args = conn.get("args") if isinstance(conn.get("args"), list) else []
                    env = conn.get("env") if isinstance(conn.get("env"), dict) else {}
                    res = self._mcp.invoke_stdio(
                        command=cmd,
                        args=args,
                        env=env,
                        method="tools/call",
                        params={"name": target_tool_name, "arguments": self._normalize_mcp_arguments(sanitized_payload)},
                    )
                    if res.get("ok"):
                        self.write_event_tool_result(session_id=session_id, tool=name, result=res)
                        return {"ok": True, "result": res.get("result", res)}
                    self.write_event_tool_error(session_id=session_id, tool=name, error=str(res.get("error")))
                    return {"ok": False, "error": str(res.get("error"))}
                except Exception as e:
                    self.write_event_tool_error(session_id=session_id, tool=name, error=str(e))
                    return {"ok": False, "error": str(e)}
            else:
                base_url = str(conn.get("base_url") or "").strip()
                if not base_url:
                    self.write_event_tool_error(session_id=session_id, tool=name, error="mcp_invalid_base_url")
                    return {"ok": False, "error": "mcp_invalid_base_url"}
                try:
                    res = self._mcp.invoke(base_url=base_url, name=target_tool_name, arguments=self._normalize_mcp_arguments(sanitized_payload))
                    self.write_event_tool_result(session_id=session_id, tool=name, result=res)
                    return {"ok": True, "result": res}
                except Exception as e:
                    self.write_event_tool_error(session_id=session_id, tool=name, error=str(e))
                    return {"ok": False, "error": str(e)}

        # 其他工具（Skills）：支援 webhook 與 python handler
        try:
            db = getattr(self, "_db", None)
            if db is None:
                self.write_event_tool_error(session_id=session_id, tool=name, error="skill_backend_not_available")
                return {"ok": False, "error": "skill_backend_not_available"}
            row = db.query(SkillEntry).filter(SkillEntry.name == name).first()
            if not row or not bool(row.enabled):
                self.write_event_tool_error(session_id=session_id, tool=name, error="skill_not_found_or_disabled")
                return {"ok": False, "error": "skill_not_found_or_disabled"}

            interaction_service = None
            prepare_result = None
            skill_payload = sanitized_payload
            try:
                interaction_service = SkillInteractionService(db)
                _log.debug("SkillInteractionService.prepare_request", tool=name, session_id=session_id)
                prepare_result = interaction_service.prepare_request(
                    conversation_id=session_id,
                    tool_name=name,
                    skill_id=(str(getattr(row, 'id', '') or '') or None),
                    payload=sanitized_payload,
                )
                if not prepare_result.ok:
                    error_text = str(prepare_result.error or 'interaction_prepare_failed')
                    self.write_event_tool_error(session_id=session_id, tool=name, error=error_text)
                    return {"ok": False, "error": error_text}
                skill_payload = prepare_result.skill_payload if isinstance(prepare_result.skill_payload, dict) else sanitized_payload
            except Exception as interaction_error:
                try:
                    db.rollback()
                except Exception:
                    pass
                self._log.warning('skill.interaction.prepare_failed_fallback', tool=name, error=str(interaction_error))
                interaction_service = None
                prepare_result = None
                skill_payload = payload or {}

            def _finalize_success_if_possible(result_payload: dict) -> dict:
                if interaction_service is None or prepare_result is None:
                    return result_payload
                try:
                    return interaction_service.finalize_success(prepare_result=prepare_result, result_payload=result_payload)
                except Exception as finalize_error:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    self._log.warning('skill.interaction.finalize_success_failed_ignore', tool=name, error=str(finalize_error))
                    return result_payload

            def _finalize_error_if_possible(error_text: str) -> None:
                if interaction_service is None or prepare_result is None:
                    return
                try:
                    interaction_service.finalize_error(prepare_result=prepare_result, error_text=error_text)
                except Exception as finalize_error:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    self._log.warning('skill.interaction.finalize_error_failed_ignore', tool=name, error=str(finalize_error))

            # 可選：依 input_schema 驗證 payload
            try:
                schema = getattr(row, 'input_schema', None)
                if isinstance(schema, dict) and schema:
                    validation_payload = skill_payload if isinstance(skill_payload, dict) else {}
                    if isinstance(validation_payload, dict) and '_interaction' in validation_payload:
                        validation_payload = {k: v for k, v in validation_payload.items() if k != '_interaction'}
                    if isinstance(validation_payload, dict) and '_attachment_context' in validation_payload:
                        validation_payload = {k: v for k, v in validation_payload.items() if k != '_attachment_context'}
                    valid = True
                    err_msg = ''
                    try:
                        # 優先 fastjsonschema
                        try:
                            import fastjsonschema  # type: ignore
                            validator = fastjsonschema.compile(schema)
                            validator(validation_payload or {})
                        except ImportError:
                            import jsonschema  # type: ignore
                            jsonschema.validate(instance=validation_payload or {}, schema=schema)
                    except Exception as ve:
                        valid = False
                        err_msg = str(ve)
                    if not valid:
                        self.write_event_tool_error(session_id=session_id, tool=name, error="schema_validation_failed")
                        return {"ok": False, "error": f"schema_validation_failed: {err_msg[:180]}"}
            except Exception:
                pass

            skill_type = str(getattr(row, 'skill_type', 'executable') or 'executable').strip()
            _log.debug("Skill type and bundle check", skill_type=skill_type, session_id=session_id)
            has_zip_bundle = bool(getattr(row, 'zip_bundle', None))
            should_use_claude_skill = skill_type in {'prompt', 'hybrid'} or has_zip_bundle

            if should_use_claude_skill and isinstance(skill_payload, dict):
                attachment_context = str(skill_payload.get('_attachment_context') or '').strip()
                if attachment_context and not str(skill_payload.get('text') or '').strip():
                    normalized_skill_payload = dict(skill_payload)
                    normalized_skill_payload['text'] = attachment_context
                    skill_payload = normalized_skill_payload

            _log.debug("Skill execution path decision", tool=name, skill_type=skill_type, has_zip_bundle=has_zip_bundle, should_use_claude_skill=should_use_claude_skill, session_id=session_id)
            if should_use_claude_skill:
                skill_exec = execute_skill(
                    skill_id=str(getattr(row, 'id', '') or name),
                    zip_bundle=getattr(row, 'zip_bundle', None),
                    prompt_template=str(getattr(row, 'prompt_template', '') or ''),
                    input_data=skill_payload or {},
                    execute_scripts=True,
                )
                _log.debug("Skill execution completed", tool=name, skill_exec_ok=skill_exec.ok if skill_exec else None, session_id=session_id)

                if not skill_exec.ok:
                    error_text = str(skill_exec.error or 'skill_zip_execution_failed')
                    _finalize_error_if_possible(error_text)
                    self.write_event_tool_error(session_id=session_id, tool=name, error=error_text)
                    return {"ok": False, "error": error_text}
                failed_scripts = [x for x in (skill_exec.script_outputs or []) if not bool(x.get('ok'))]
                if failed_scripts:
                    err = f"script_execution_failed: {failed_scripts[0].get('script') or ''}".strip()
                    _finalize_error_if_possible(err)
                    self.write_event_tool_error(session_id=session_id, tool=name, error=err)
                    return {"ok": False, "error": err, "script_outputs": skill_exec.script_outputs}

                interactive_result = self._extract_interactive_skill_result(skill_exec.script_outputs)
                if isinstance(interactive_result, dict):
                    result = {
                        "ok": True,
                        "result": interactive_result,
                        "script_outputs": skill_exec.script_outputs or [],
                    }
                    result = _finalize_success_if_possible(result)
                    self.write_event_tool_result(session_id=session_id, tool=name, result=result)
                    return result

                merged_prompt_base = str(skill_exec.prompt or '').strip()
                if not merged_prompt_base:
                    _finalize_error_if_possible('empty_prompt_template')
                    self.write_event_tool_error(session_id=session_id, tool=name, error="empty_prompt_template")
                    return {"ok": False, "error": "empty_prompt_template"}
                import json as _json
                prompt_payload = skill_payload or {}
                if isinstance(prompt_payload, dict):
                    prompt_payload = {k: v for k, v in prompt_payload.items() if k not in {'_script', '_args'}}
                payload_json = _json.dumps(prompt_payload or {}, ensure_ascii=False)
                merged_prompt = (
                    f"{merged_prompt_base}\n\n"
                    "[技能輸入(JSON)]\n"
                    f"{payload_json}\n\n"
                    "請嚴格依模板要求輸出最終結果。"
                )
                try:
                    out = self._llm.complete(prompt=merged_prompt, tier=None)
                    result = {
                        "ok": True,
                        "result": {
                            "text": str(out or ""),
                            "mode": "claude_skill",
                            "script_outputs": skill_exec.script_outputs or [],
                        },
                    }
                    result = _finalize_success_if_possible(result)
                    self.write_event_tool_result(session_id=session_id, tool=name, result=result)
                    return result
                except Exception as e:
                    _finalize_error_if_possible(str(e))
                    self.write_event_tool_error(session_id=session_id, tool=name, error=str(e))
                    return {"ok": False, "error": str(e)}

            try:
                timeout_ms = int(getattr(row, 'timeout_ms', 8000) or 8000)
            except Exception:
                timeout_ms = 8000

            command_text = str(getattr(row, 'command', '') or '').strip()
            if command_text:
                _log.debug("Before run skill command ", tool=name, command=command_text, session_id=session_id)
                result = self._run_skill_command(command=command_text, payload=skill_payload or {}, timeout_ms=timeout_ms)
                if bool(result.get('ok')):
                    result = _finalize_success_if_possible(result)
                    self.write_event_tool_result(session_id=session_id, tool=name, result=result)
                    return result
                _finalize_error_if_possible(str(result.get('error') or 'skill_command_failed'))
                self.write_event_tool_error(session_id=session_id, tool=name, error=str(result.get('error')))
                return result

            if str(getattr(row, 'type', 'webhook')) == 'python':
                # 本地 Python handler：package.module:function
                handler = str(getattr(row, 'python_handler', '') or '').strip()
                _log.debug('python_handler_execution', tool=name, handler=handler, session_id=session_id)
                if not handler or ':' not in handler:
                    self.write_event_tool_error(session_id=session_id, tool=name, error="invalid_python_handler")
                    return {"ok": False, "error": "invalid_python_handler"}
                mod_name, func_name = handler.split(':', 1)
                try:
                    import importlib
                    mod = importlib.import_module(mod_name)
                    func = getattr(mod, func_name)
                    res = func(skill_payload or {})
                    _log.debug('python_handler_execution_completed', tool=name, handler=handler, session_id=session_id, result=str(res)[:200])
                    if not isinstance(res, dict):
                        res = {"ok": True, "result": res}
                    elif 'ok' not in res:
                        res = {"ok": True, "result": res}
                    elif bool(res.get('ok')) and 'result' not in res:
                        normalized_result = {k: v for k, v in res.items() if k != 'ok'}
                        res = {"ok": True, "result": normalized_result}
                    res = _finalize_success_if_possible(res)
                    self.write_event_tool_result(session_id=session_id, tool=name, result=res)
                    return res
                except Exception as e:
                    _finalize_error_if_possible(str(e))
                    self.write_event_tool_error(session_id=session_id, tool=name, error=str(e))
                    return {"ok": False, "error": str(e)}
            else:
                # webhook
                url = str(getattr(row, 'endpoint_url', '') or '').strip()
                method = str(getattr(row, 'http_method', 'POST') or 'POST').upper()
                headers = getattr(row, 'headers', {}) or {}
                if not url:
                    self.write_event_tool_error(session_id=session_id, tool=name, error="invalid_webhook_url")
                    return {"ok": False, "error": "invalid_webhook_url"}
                # schema 驗證（webhook）
                try:
                    schema = getattr(row, 'input_schema', None)
                    if isinstance(schema, dict) and schema:
                        validation_payload = skill_payload if isinstance(skill_payload, dict) else {}
                        if isinstance(validation_payload, dict) and '_interaction' in validation_payload:
                            validation_payload = {k: v for k, v in validation_payload.items() if k != '_interaction'}
                        try:
                            import fastjsonschema  # type: ignore
                            validator = fastjsonschema.compile(schema)
                            validator(validation_payload or {})
                        except ImportError:
                            import jsonschema  # type: ignore
                            jsonschema.validate(instance=validation_payload or {}, schema=schema)
                        except Exception as ve:
                            self.write_event_tool_error(session_id=session_id, tool=name, error="schema_validation_failed")
                            # 盡量回傳結構化錯誤路徑
                            def _ext(e: Exception):
                                p = getattr(e, 'path', None); m = str(e)
                                if p is not None:
                                    if isinstance(p, (list, tuple)):
                                        return [{'path': '.'.join(map(str, p)), 'message': m}]
                                    return [{'path': str(p), 'message': m}]
                                path = getattr(e, 'path', None)
                                try:
                                    parts = [str(x) for x in list(path)]
                                    return [{'path': '.'.join(parts), 'message': m}]
                                except Exception:
                                    return [{'path': '', 'message': m}]
                            return {"ok": False, "error": "schema_validation_failed", "errors": _ext(ve)}
                except Exception:
                    pass
                try:
                    import httpx
                    with httpx.Client(timeout=timeout_ms / 1000.0) as client:
                        if method == 'GET':
                            resp = client.get(url, params=skill_payload or {}, headers={k: str(v) for k, v in (headers.items() if isinstance(headers, dict) else [])})
                        elif method == 'PUT':
                            resp = client.put(url, json=skill_payload or {}, headers={k: str(v) for k, v in (headers.items() if isinstance(headers, dict) else [])})
                        elif method == 'DELETE':
                            resp = client.delete(url, json=skill_payload or {}, headers={k: str(v) for k, v in (headers.items() if isinstance(headers, dict) else [])})
                        else:
                            resp = client.post(url, json=skill_payload or {}, headers={k: str(v) for k, v in (headers.items() if isinstance(headers, dict) else [])})
                    if resp.status_code >= 400:
                        err = f"http_{resp.status_code}: {(resp.text or '')[:200]}"
                        _finalize_error_if_possible(err)
                        self.write_event_tool_error(session_id=session_id, tool=name, error=err)
                        return {"ok": False, "error": err}
                    # 嘗試 JSON 解析；否則以 text 包裝
                    try:
                        data = resp.json()
                    except Exception:
                        data = {"text": resp.text}
                    result = {"ok": True, "result": data}
                    result = _finalize_success_if_possible(result)
                    self.write_event_tool_result(session_id=session_id, tool=name, result=result)
                    return result
                except Exception as e:
                    _finalize_error_if_possible(str(e))
                    self.write_event_tool_error(session_id=session_id, tool=name, error=str(e))
                    return {"ok": False, "error": str(e)}
        except Exception as e:
            self.write_event_tool_error(session_id=session_id, tool=name, error=str(e))
            return {"ok": False, "error": str(e)}

    def _validate_sync_tool_call_request(self, *, session_id: str, tool: str, payload: dict) -> tuple[str, Optional[dict]]:
        """統一同步工具請求前置檢查。

        Why: 與非同步路徑保持相同防護步驟，避免兩條路徑行為漂移。
        """
        name = (tool or "").strip()
        if not name:
            self.write_event_tool_error(session_id=session_id, tool="<empty>", error="invalid_tool")
            return name, {"ok": False, "error": "invalid_tool"}

        if name not in self._allowed_tools:
            self.write_event_tool_error(session_id=session_id, tool=name, error="tool_not_allowed")
            return name, {"ok": False, "error": "tool_not_allowed"}

        policy_error = self._evaluate_tool_policy_before_execute(session_id=session_id, tool=name, payload=payload or {})
        if policy_error is not None:
            return name, policy_error

        sanitized_payload = self._strip_policy_runtime_fields(payload or {})
        self.write_event_tool_input(session_id=session_id, tool=name, payload=sanitized_payload)
        if self._check_doom_loop(session_id=session_id, tool=name, payload=sanitized_payload):
            return name, {"ok": False, "error": "doom_loop_denied"}

        return name, None

    def _evaluate_tool_policy_before_execute(self, *, session_id: str, tool: str, payload: dict) -> Optional[dict]:
        # 目的：在工具執行前套用風險策略（確認、額度、scope）。
        # 為什麼：工具執行已不依賴 RBAC，需以策略層統一治理風險。
        if not bool(getattr(settings, 'TOOL_EXEC_POLICY_ENABLED', True)):
            return None
        if not hasattr(self, '_db') or self._db is None:
            return None
        if not self._current_user_id:
            return None

        decision = self._tool_policy_service.evaluate_execution(
            db=self._db,
            tool_name=str(tool or '').strip(),
            payload=payload if isinstance(payload, dict) else {},
            agent_class=self._current_agent_class,
            user_id=self._current_user_id,
            agent_id=self._current_agent_id,
            conversation_id=self._current_conversation_id or session_id,
        )
        if decision.passed:
            self._active_tool_policy_decisions[(session_id, str(tool or '').strip())] = decision
            return None

        deny_reason = str(decision.reason or 'policy_denied')
        status = 'confirmation_required' if deny_reason == 'confirmation_required' else 'policy_denied'
        self.write_event_tool_error(session_id=session_id, tool=str(tool or '').strip(), error=status)
        self._tool_policy_service.record_audit(
            db=self._db,
            decision=decision,
            user_id=self._current_user_id,
            agent_id=self._current_agent_id,
            conversation_id=self._current_conversation_id or session_id,
            tool_name=str(tool or '').strip(),
            status='denied',
            deny_reason=deny_reason,
            payload_keys=list(self._strip_policy_runtime_fields(payload or {}).keys()),
            details={'stage': 'before_execute'},
        )
        policy_payload = {
            'reason': deny_reason,
            'risk_level': decision.risk_level,
            'requires_confirmation': decision.requires_confirmation,
            'cost_class': decision.cost_class,
        }
        if deny_reason == 'confirmation_required' and decision.confirmation_token:
            policy_payload['confirm_token'] = decision.confirmation_token
        if deny_reason == 'confirmation_required' and decision.confirmation_token_expires_at:
            policy_payload['confirm_token_expires_at'] = decision.confirmation_token_expires_at.isoformat()
        return {
            'ok': False,
            'error': status,
            'policy': policy_payload,
        }

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
        # 標記開始時間
        try:
            self._tool_start_times[(session_id, tool)] = datetime.now()
        except Exception:
            pass
        # T040：檢測 doom loop
        if self._check_doom_loop(session_id=session_id, tool=tool, payload=payload):
            # 記錄並結束本輪
            self.write_event_tool_error(session_id=session_id, tool=tool, error="doom_loop_denied")
            self.write_event_step_finish(session_id=session_id)
            self.write_event_finish(session_id=session_id)
            return

    def write_event_tool_result(self, *, session_id: str, tool: str, result: dict) -> None:
        # 計算耗時
        duration_ms = None
        try:
            t0 = self._tool_start_times.pop((session_id, tool), None)
            if t0 is not None:
                duration_ms = int((datetime.now() - t0).total_seconds() * 1000)
        except Exception:
            duration_ms = None
        self._log.info("event.tool_result", session_id=session_id, tool=tool, keys=list(result.keys()), duration_ms=duration_ms)
        self._audit(action="event.tool_result", session_id=session_id, details={"tool": tool, "keys": list(result.keys()), "duration_ms": duration_ms})
        payload = {"tool": tool, "result": result, "duration_ms": duration_ms}
        self._write_part(session_id=session_id, type_="tool_result", payload=payload)
        self._record_tool_policy_result(session_id=session_id, tool=tool, status='success', result=result, duration_ms=duration_ms)

    def write_event_tool_error(self, *, session_id: str, tool: str, error: str) -> None:
        duration_ms = None
        try:
            t0 = self._tool_start_times.pop((session_id, tool), None)
            if t0 is not None:
                duration_ms = int((datetime.now() - t0).total_seconds() * 1000)
        except Exception:
            duration_ms = None
        self._log.error("event.tool_error", session_id=session_id, tool=tool, error=error, duration_ms=duration_ms)
        self._audit(action="event.tool_error", session_id=session_id, details={"tool": tool, "error": error, "duration_ms": duration_ms})
        self._write_part(session_id=session_id, type_="tool_error", payload={"tool": tool, "error": error, "duration_ms": duration_ms})
        self._record_tool_policy_result(session_id=session_id, tool=tool, status='error', result={'error': error}, duration_ms=duration_ms)

    def _record_tool_policy_result(self, *, session_id: str, tool: str, status: str, result: dict, duration_ms: int | None) -> None:
        # 目的：將策略判斷與工具最終結果寫入稽核表。
        # 為什麼：治理層需要 who/agent/tool/risk/status 的完整追溯資料。
        if not bool(getattr(settings, 'TOOL_EXEC_POLICY_ENABLED', True)):
            return
        if not hasattr(self, '_db') or self._db is None:
            return
        decision = self._active_tool_policy_decisions.pop((session_id, str(tool or '').strip()), None)
        if decision is None:
            return
        cost_estimate = self._extract_cost_estimate(result)
        self._tool_policy_service.record_audit(
            db=self._db,
            decision=decision,
            user_id=self._current_user_id,
            agent_id=self._current_agent_id,
            conversation_id=self._current_conversation_id or session_id,
            tool_name=str(tool or '').strip(),
            status=status,
            payload_keys=list((result or {}).keys()),
            deny_reason=None if status == 'success' else str((result or {}).get('error') or ''),
            cost_estimate=cost_estimate,
            latency_ms=duration_ms,
            details={'stage': 'after_execute'},
        )

    def _extract_cost_estimate(self, result: dict) -> Decimal | None:
        if not isinstance(result, dict):
            return None
        for key in ('cost_estimate', 'cost_usd', 'cost'):
            raw_value = result.get(key)
            if raw_value is None:
                continue
            try:
                return Decimal(str(raw_value))
            except Exception:
                continue
        inner = result.get('result')
        if isinstance(inner, dict):
            for key in ('cost_estimate', 'cost_usd', 'cost'):
                raw_value = inner.get(key)
                if raw_value is None:
                    continue
                try:
                    return Decimal(str(raw_value))
                except Exception:
                    continue
        return None

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
            import asyncio

            async def _safe_ask():
                try:
                    await self._perm.ask(session_id=session_id, permission="doom_loop", patterns=[tool])
                except NotImplementedError:
                    # 權限互動尚未實作：忽略，採用允許繼續策略
                    pass
                except Exception as e:
                    # 後台任務錯誤不影響當前流程
                    self._log.warning("doom_loop.ask_task_error", session_id=session_id, tool=tool, error=str(e))

            try:
                # 若目前已有事件迴圈，改以背景任務執行，不阻塞、不生成未 await 警告
                loop = asyncio.get_running_loop()
                loop.create_task(_safe_ask())
                return False
            except RuntimeError:
                # 無事件迴圈：同步執行一次
                asyncio.run(_safe_ask())
                return False
        except Exception as e:
            # 任意例外視為無法詢問，為避免誤殺，採取允許繼續策略
            self._log.warning("doom_loop.ask_failed", session_id=session_id, tool=tool, error=str(e))
            return False

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
