"""
MCP 客戶端骨架

目標：
- 以最小介面封裝 MCP 伺服端的工具清單與呼叫；
- 不做真實網路呼叫，先回傳佔位結果，以便 ChatRouter 打通白名單與事件流程；
- 後續可替換為實際 SDK/協定實作。
"""

from __future__ import annotations

from typing import Any, Dict, AsyncGenerator
import httpx
from src.core.logging import get_logger
from src.core.config import settings
from urllib.parse import urlparse, urlunparse


class MCPClient:
    def __init__(self) -> None:
        self._log = get_logger("MCPClient")
        self._timeout = max(500, int(getattr(settings, "MCP_HTTP_TIMEOUT_MS", 4000))) / 1000.0

    def _build_headers(self, auth: Dict[str, Any] | None) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if not auth:
            return headers
        token = auth.get("token") if isinstance(auth, dict) else None
        api_key = auth.get("api_key") if isinstance(auth, dict) else None
        extra = auth.get("headers") if isinstance(auth, dict) else None
        if isinstance(token, str) and token.strip():
            headers["Authorization"] = f"Bearer {token.strip()}"
        if isinstance(api_key, str) and api_key.strip():
            headers["X-API-Key"] = api_key.strip()
        if isinstance(extra, dict):
            for k, v in extra.items():
                if isinstance(k, str) and isinstance(v, str):
                    headers[k] = v
        return headers

    def _classify_error(self, err: Exception, status: int | None = None) -> str:
        msg = str(err).lower()
        if isinstance(err, httpx.ConnectTimeout) or "timeout" in msg:
            return "timeout"
        if isinstance(err, httpx.ConnectError) or "connection refused" in msg:
            return "connection_refused"
        if "name or service not known" in msg or "dns" in msg:
            return "dns_error"
        if isinstance(err, httpx.HTTPStatusError):
            if status == 401:
                return "unauthorized"
            if status == 403:
                return "forbidden"
            if status == 404:
                return "not_found"
            if status and 500 <= status < 600:
                return "server_error"
        if "ssl" in msg or "tls" in msg:
            return "tls_error"
        return "unknown_error"

    def test_connection(self, *, base_url: str, auth: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """嘗試對 MCP base_url 進行最小連線測試。

        策略：
        1) HEAD / 或 GET /.well-known/ 之一；
        2) 回傳 ok 與錯誤分類，不擲例外。
        """
        headers = self._build_headers(auth)
        try:
            with httpx.Client(timeout=self._timeout) as client:
                # 優先 HEAD /
                try:
                    r = client.head(base_url, headers=headers, follow_redirects=True)
                except httpx.HTTPStatusError as he:  # pragma: no cover - 消化為錯誤分類
                    status = he.response.status_code if he.response else None
                    reason = self._classify_error(he, status)
                    return {"ok": False, "error": reason, "status": status}
                except Exception as e:
                    # 改以 GET 嘗試 well-known
                    try:
                        r = client.get(base_url.rstrip("/") + "/.well-known", headers=headers, follow_redirects=True)
                    except Exception as ee:
                        reason = self._classify_error(ee)
                        return {"ok": False, "error": reason, "status": None}
                # 走到此處即代表通了
                return {"ok": True, "error": None, "status": r.status_code}
        except Exception as e:
            reason = self._classify_error(e)
            return {"ok": False, "error": reason, "status": None}

    def list_tools(self, *, base_url: str, auth: Dict[str, Any] | None = None) -> list[dict[str, Any]]:
        headers = self._build_headers(auth)
        paths = [
            "/tools",
            "/.well-known/mcp/tools",
        ]
        with httpx.Client(timeout=self._timeout) as client:
            for p in paths:
                url = base_url.rstrip("/") + p
                try:
                    r = client.get(url, headers=headers)
                    r.raise_for_status()
                    data = r.json()
                    if isinstance(data, dict) and isinstance(data.get("tools"), list):
                        return data["tools"]
                    if isinstance(data, list):
                        return data
                except Exception as e:
                    self._log.warning("mcp.list_tools.attempt_failed", url=url, error=str(e))
                    continue
        return []

    def invoke(self, *, base_url: str, name: str, arguments: Dict[str, Any] | None = None, auth: Dict[str, Any] | None = None) -> Dict[str, Any]:
        headers = self._build_headers(auth)
        # 嘗試幾種可預期的端點
        candidates = [
            ("POST", f"{base_url.rstrip('/')}/tools/{name}"),
            ("POST", f"{base_url.rstrip('/')}/invoke"),
        ]
        payloads = [
            arguments or {},
            {"tool": name, "arguments": arguments or {}},
        ]
        last_error: Exception | None = None
        with httpx.Client(timeout=self._timeout) as client:
            for (method, url), body in zip(candidates, payloads):
                try:
                    if method == "POST":
                        r = client.post(url, headers=headers, json=body)
                    else:
                        r = client.request(method, url, headers=headers, json=body)
                    r.raise_for_status()
                    data = r.json()
                    # 嘗試標準化
                    return {"ok": True, "tool": name, "data": data}
                except Exception as e:
                    self._log.warning("mcp.invoke.attempt_failed", url=url, error=str(e))
                    last_error = e
                    continue
        # 嘗試 JSON-RPC 2.0 端點（HTTP）
        rpc = self.rpc_call(base_url=base_url, method="tools.invoke", params={"tool": name, "arguments": arguments or {}}, auth=auth)
        if rpc.get("ok"):
            return {"ok": True, "tool": name, "data": rpc.get("result")}
        # 全部失敗：回傳分類錯誤
        try:
            reason = self._classify_error(last_error) if last_error is not None else "unknown_error"
        except Exception:
            reason = "unknown_error"
        return {"ok": False, "error": reason, "tool": name}

    def rpc_call(self, *, base_url: str, method: str, params: Dict[str, Any] | None = None, auth: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """以 JSON-RPC 2.0 嘗試呼叫 MCP 服務（HTTP 傳輸）。

        端點猜測：/jsonrpc 或 /rpc；回傳統一結構 {ok, result|error}
        """
        headers = self._build_headers(auth)
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        for path in ("/jsonrpc", "/rpc"):
            url = base_url.rstrip("/") + path
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    r = client.post(url, headers=headers, json=body)
                    r.raise_for_status()
                    data = r.json()
                    if isinstance(data, dict) and data.get("jsonrpc") == "2.0":
                        if "result" in data:
                            return {"ok": True, "result": data.get("result")}
                        if "error" in data:
                            return {"ok": False, "error": data.get("error")}
            except Exception as e:
                self._log.warning("mcp.rpc.attempt_failed", url=url, error=str(e))
                continue
        return {"ok": False, "error": "rpc_unavailable"}

    # ===== WebSocket JSON-RPC (Async) =====
    def _to_ws_url(self, base_url: str, path: str) -> str:
        u = urlparse(base_url)
        scheme = 'wss' if u.scheme == 'https' else 'ws'
        new = u._replace(scheme=scheme)
        p = path if path.startswith('/') else ('/' + path)
        return urlunparse(new._replace(path=p))

    async def rpc_call_ws(self, *, base_url: str, method: str, params: Dict[str, Any] | None = None, auth: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """以 WebSocket JSON-RPC 2.0 呼叫單次方法。

        需求：伺服端支援 WS 並以 settings.MCP_WS_PATH 為端點；
        若環境缺少 websockets 套件或伺服器不可用，回傳 {ok: False, error: ws_not_available}。
        """
        try:
            import websockets  # type: ignore
            import json as _json
        except Exception:
            return {"ok": False, "error": "ws_not_available"}

        url = self._to_ws_url(base_url, getattr(settings, 'MCP_WS_PATH', '/ws'))
        headers = self._build_headers(auth)
        try:
            async with websockets.connect(url, extra_headers=headers, close_timeout=self._timeout) as ws:  # type: ignore
                req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
                await ws.send(_json.dumps(req, ensure_ascii=False))
                raw = await ws.recv()
                data = _json.loads(raw)
                if isinstance(data, dict) and data.get("jsonrpc") == "2.0":
                    if "result" in data:
                        return {"ok": True, "result": data.get("result")}
                    if "error" in data:
                        return {"ok": False, "error": data.get("error")}
                return {"ok": False, "error": "invalid_rpc_response"}
        except Exception as e:
            self._log.warning("mcp.ws.rpc_failed", url=url, error=str(e))
            return {"ok": False, "error": self._classify_error(e)}

    async def stream_rpc_call_ws(self, *, base_url: str, method: str, params: Dict[str, Any] | None = None, auth: Dict[str, Any] | None = None) -> AsyncGenerator[Dict[str, Any], None]:
        """以 WebSocket JSON-RPC 2.0 串流模式收取多段輸出。

        回傳每段訊息的原始 JSON（已解析為 dict）。遇到包含 result 或 error 時結束。
        若不可用則直接 yield 一段 {ok: False, error: 'ws_not_available'} 後結束。
        """
        try:
            import websockets  # type: ignore
            import json as _json
        except Exception:
            yield {"ok": False, "error": "ws_not_available"}
            return

        url = self._to_ws_url(base_url, getattr(settings, 'MCP_WS_PATH', '/ws'))
        headers = self._build_headers(auth)
        try:
            async with websockets.connect(url, extra_headers=headers, close_timeout=self._timeout) as ws:  # type: ignore
                req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
                await ws.send(_json.dumps(req, ensure_ascii=False))
                while True:
                    raw = await ws.recv()
                    data = _json.loads(raw)
                    yield data
                    if isinstance(data, dict) and ("result" in data or "error" in data):
                        break
        except Exception as e:
            yield {"ok": False, "error": self._classify_error(e)}
