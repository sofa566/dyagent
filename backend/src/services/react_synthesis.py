from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional
import json
import re


ToolCaller = Callable[[str, dict[str, Any]], dict[str, Any]]
LLMComplete = Callable[[str, Optional[str]], str]


DEFAULT_MAX_STEPS = 3
DEFAULT_MAX_OBSERVATION_CHARS = 4000
MIN_MAX_OBSERVATION_CHARS = 500
MAX_SYNTHESIZE_RESULT_CHARS = 5000
MAX_RAW_RESULT_CHARS = 1000


@dataclass
class ReActStep:
    kind: str
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    answer: str | None = None


@dataclass
class ReActConfig:
    max_steps: int = DEFAULT_MAX_STEPS
    max_observation_chars: int = DEFAULT_MAX_OBSERVATION_CHARS


class WeatherDomainHeuristics:
    """天氣相關規則封裝。

    註解：這層只處理領域規則，避免把通用 ReAct 流程與特定領域耦合在一起。
    """

    def is_weather_intent(self, msg: str) -> bool:
        low = (msg or "").lower()
        return ("天氣" in (msg or "")) or ("weather" in low)

    def extract_city(self, msg: str) -> str:
        m = re.search(r"([\u4e00-\u9fff]{1,12})(?:的)?天氣", msg or "")
        if m:
            cand = m.group(1)
            for p in ("請問", "今天", "今日", "現在", "目前", "一下", "幫我查", "幫我", "查詢"):
                cand = cand.replace(p, "")
            cand = cand.strip("，。!?！？ ")
            if cand.endswith("的"):
                cand = cand[:-1]
            return cand or "台北"

        m2 = re.search(r"weather\s+in\s+([A-Za-z\-\s]{2,40})", msg or "", flags=re.IGNORECASE)
        if m2:
            return m2.group(1).strip()
        return "台北"

    def fallback_action_args(self, user_message: str) -> dict[str, Any]:
        return {"q": self.extract_city(user_message)}

    def format_domain_answer(self, *, args: dict[str, Any], result_obj: Any, user_message: str) -> str | None:
        data = result_obj
        if isinstance(data, dict) and isinstance(data.get("result"), dict):
            data = data.get("result")
        if not isinstance(data, dict):
            return None

        cur = None
        if isinstance(data.get("current_weather"), dict):
            cur = data.get("current_weather")
        elif isinstance(data.get("current"), dict):
            cur = data.get("current")
        if not isinstance(cur, dict):
            return None

        temp = cur.get("temperature") if "temperature" in cur else cur.get("temperature_2m")
        wind = cur.get("windspeed") if "windspeed" in cur else cur.get("wind_speed_10m")
        code = cur.get("weathercode") if "weathercode" in cur else cur.get("weather_code")

        city = ""
        if isinstance(data.get("location"), dict):
            city = str(data.get("location", {}).get("name") or "").strip()
        if not city:
            city = str((args or {}).get("q") or self.extract_city(user_message))

        parts = [f"目前 {city} 天氣"]
        if temp is not None:
            parts.append(f"氣溫約 {temp}°C")
        if wind is not None:
            parts.append(f"風速約 {wind}")
        if code is not None:
            parts.append(f"天氣代碼 {code}")
        if len(parts) <= 1:
            return None
        return "，".join(parts) + "。"


class ReActSynthesis:
    """以 ReAct 迴圈執行工具規劃與最終回覆合成。"""

    def __init__(
        self,
        *,
        llm_complete: LLMComplete,
        call_tool: ToolCaller,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_observation_chars: int = DEFAULT_MAX_OBSERVATION_CHARS,
    ) -> None:
        self._llm_complete = llm_complete
        self._call_tool = call_tool
        self._cfg = ReActConfig(
            max_steps=max(1, int(max_steps or DEFAULT_MAX_STEPS)),
            max_observation_chars=max(MIN_MAX_OBSERVATION_CHARS, int(max_observation_chars or DEFAULT_MAX_OBSERVATION_CHARS)),
        )
        self._last_trace: list[dict[str, Any]] = []
        self._domain = WeatherDomainHeuristics()

    def run(self, *, user_message: str, allowed_tools: list[str], tier: Optional[str] = None) -> str | None:
        if not allowed_tools:
            return None

        allowed_set = set(allowed_tools)
        observations: list[dict[str, Any]] = []
        last_success: dict[str, Any] | None = None
        self._last_trace = []

        for i in range(self._cfg.max_steps):
            step = self._plan_step(
                user_message=user_message,
                allowed_tools=allowed_tools,
                observations=observations,
                tier=tier,
            )

            terminal = self._handle_planning_outcome(step=step, step_index=i)
            if terminal is not None:
                return terminal
            if step is None:
                break
            if step.kind != "action" or not step.tool:
                break

            if step.tool not in allowed_set:
                self._record_tool_not_allowed(step_index=i, tool=step.tool, observations=observations)
                continue

            action_result = self._execute_tool(step_index=i, step=step, observations=observations)
            if action_result.get("status") == "failed":
                return str(action_result.get("message") or "")
            if action_result.get("status") == "ok":
                last_success = {
                    "tool": step.tool,
                    "arguments": action_result.get("arguments") if isinstance(action_result.get("arguments"), dict) else {},
                    "result": action_result.get("result") if isinstance(action_result.get("result"), dict) else {},
                }

        return self._finalize_answer(
            user_message=user_message,
            tier=tier,
            last_success=last_success,
            observations=observations,
        )

    def _handle_planning_outcome(self, *, step: ReActStep | None, step_index: int) -> str | None:
        if step is None:
            self._last_trace.append({"step": step_index + 1, "phase": "plan", "result": "none"})
            return None

        if step.kind == "final":
            ans = (step.answer or "").strip()
            if ans:
                self._last_trace.append({"step": step_index + 1, "phase": "final", "ok": True})
                return ans
            self._last_trace.append({"step": step_index + 1, "phase": "final", "ok": False})
            return None

        if step.kind != "action" or not step.tool:
            self._last_trace.append({"step": step_index + 1, "phase": "plan", "result": "invalid_action"})
            return None

        # 有效的 action step：回傳 None 讓 run() 繼續執行工具呼叫
        return None

    def _record_tool_not_allowed(self, *, step_index: int, tool: str, observations: list[dict[str, Any]]) -> None:
        self._last_trace.append(
            {"step": step_index + 1, "phase": "act", "tool": tool, "ok": False, "error": "tool_not_allowed"}
        )
        observations.append({"ok": False, "error": "tool_not_allowed", "tool": tool})

    def _execute_tool(self, *, step_index: int, step: ReActStep, observations: list[dict[str, Any]]) -> dict[str, Any]:
        if not step.tool:
            return {"status": "unknown"}
        args = step.arguments if isinstance(step.arguments, dict) else {}
        res = self._call_tool(step.tool, args)
        observations.append({"tool": step.tool, "arguments": args, "result": res})
        self._last_trace.append(
            {
                "step": step_index + 1,
                "phase": "act",
                "tool": step.tool,
                "ok": bool(isinstance(res, dict) and res.get("ok")),
            }
        )

        if isinstance(res, dict) and res.get("ok"):
            return {"status": "ok", "arguments": args, "result": res.get("result", {})}

        if isinstance(res, dict) and not res.get("ok"):
            err = str(res.get("error") or "tool_failed")
            return {"status": "failed", "message": f"抱歉，我嘗試查詢但失敗：{err}"}

        return {"status": "unknown"}

    def _finalize_answer(
        self,
        *,
        user_message: str,
        tier: Optional[str],
        last_success: dict[str, Any] | None,
        observations: list[dict[str, Any]],
    ) -> str | None:
        if last_success is not None:
            direct = self._domain.format_domain_answer(
                args=last_success["arguments"] if isinstance(last_success.get("arguments"), dict) else {},
                result_obj=last_success.get("result", {}),
                user_message=user_message,
            )
            if direct:
                return direct

            llm_answer = self._synthesize_answer(
                user_message=user_message,
                tool=str(last_success["tool"]),
                arguments=last_success["arguments"] if isinstance(last_success.get("arguments"), dict) else {},
                result=last_success.get("result", {}) if isinstance(last_success.get("result"), dict) else {},
                tier=tier,
            )
            if llm_answer:
                return llm_answer

        if observations:
            last = observations[-1]
            if isinstance(last, dict) and isinstance(last.get("result"), dict):
                raw = json.dumps(last.get("result", {}), ensure_ascii=False)
                return f"已完成查詢，但目前無法產生最佳敘述。工具結果：{raw[:MAX_RAW_RESULT_CHARS]}"
        return None

    def _plan_step(
        self,
        *,
        user_message: str,
        allowed_tools: list[str],
        observations: list[dict[str, Any]],
        tier: Optional[str],
    ) -> ReActStep | None:
        obs_text = self._compact_observations(observations)
        prompt = (
            "You are a ReAct planner for tool usage.\n"
            "Return STRICT JSON only.\n"
            "Schema: {\"kind\":\"action\"|\"final\",\"tool\":string?,\"arguments\":object?,\"answer\":string?}.\n"
            "Rules:\n"
            "1) Use kind=action only when a tool is required.\n"
            "2) tool must be one of allowed tools.\n"
            "3) If observations are enough, output kind=final with answer in Traditional Chinese.\n"
            "4) Never output markdown.\n"
            f"Allowed tools: {', '.join(allowed_tools)}\n"
            f"User: {user_message}\n"
            f"Observations: {obs_text}\n"
            "Output JSON only."
        )

        text = self._safe_complete(prompt=prompt, tier=tier)
        obj = self._extract_json(text)
        if not isinstance(obj, dict):
            return self._keyword_fallback(user_message=user_message, allowed_tools=allowed_tools, observations=observations)

        kind = str(obj.get("kind") or "").strip().lower()
        if kind == "final":
            return ReActStep(kind="final", answer=str(obj.get("answer") or "").strip())

        if kind == "action":
            tool = str(obj.get("tool") or "").strip()
            args = obj.get("arguments") if isinstance(obj.get("arguments"), dict) else {}
            if not args and self._domain.is_weather_intent(user_message):
                args = self._domain.fallback_action_args(user_message)
            return ReActStep(kind="action", tool=tool, arguments=args)

        return self._keyword_fallback(user_message=user_message, allowed_tools=allowed_tools, observations=observations)

    def _keyword_fallback(
        self,
        *,
        user_message: str,
        allowed_tools: list[str],
        observations: list[dict[str, Any]],
    ) -> ReActStep | None:
        # 已有 observation 代表至少走過一輪，不再盲目追加 action
        if observations:
            return None
        if self._domain.is_weather_intent(user_message):
            for tool in allowed_tools:
                tl = tool.lower()
                if ("天氣" in tool) or ("weather" in tl) or ("meteo" in tl):
                    return ReActStep(kind="action", tool=tool, arguments=self._domain.fallback_action_args(user_message))
        return None

    def _synthesize_answer(
        self,
        *,
        user_message: str,
        tool: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        tier: Optional[str],
    ) -> str | None:
        prompt = (
            "你是繁體中文助理。請根據工具結果產生自然、精簡且可讀的最終回答。\n"
            "規則：\n"
            "1) 不要輸出 JSON。\n"
            "2) 不要提及內部工具流程。\n"
            "3) 若資料不足，直接說明缺少什麼。\n"
            f"使用者問題：{user_message}\n"
            f"工具：{tool}\n"
            f"輸入：{json.dumps(arguments, ensure_ascii=False)}\n"
            f"輸出：{json.dumps(result, ensure_ascii=False)[:MAX_SYNTHESIZE_RESULT_CHARS]}\n"
            "請直接輸出最後回答："
        )
        ans = self._safe_complete(prompt=prompt, tier=tier)
        ans = (ans or "").strip()
        if not ans:
            return None
        if ans.startswith("{") and ans.endswith("}"):
            return None
        return ans

    def last_trace(self) -> list[dict[str, Any]]:
        return list(self._last_trace)

    def _compact_observations(self, observations: list[dict[str, Any]]) -> str:
        if not observations:
            return "[]"
        text = json.dumps(observations, ensure_ascii=False)
        if len(text) <= self._cfg.max_observation_chars:
            return text
        tail = observations[-2:] if len(observations) >= 2 else observations[-1:]
        compressed = {
            "truncated": True,
            "count": len(observations),
            "tail": tail,
        }
        ctext = json.dumps(compressed, ensure_ascii=False)
        if len(ctext) <= self._cfg.max_observation_chars:
            return ctext
        return ctext[: self._cfg.max_observation_chars]

    def _safe_complete(self, *, prompt: str, tier: Optional[str]) -> str:
        try:
            return str(self._llm_complete(prompt, tier) or "")
        except Exception:
            return ""

    def _extract_json(self, text: str) -> dict[str, Any] | None:
        s = (text or "").strip()
        if not s:
            return None
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass

        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            snippet = s[start : end + 1]
            try:
                obj = json.loads(snippet)
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None
        return None
