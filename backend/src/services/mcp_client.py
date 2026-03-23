"""
MCP 客戶端骨架

目標：
- 以最小介面封裝 MCP 伺服端的工具清單與呼叫；
- 不做真實網路呼叫，先回傳佔位結果，以便 ChatRouter 打通白名單與事件流程；
- 後續可替換為實際 SDK/協定實作。
"""

from __future__ import annotations

from typing import Any, Dict, AsyncGenerator, Optional
import httpx
from src.core.logging import get_logger
from src.core.config import settings
from urllib.parse import urlparse, urlunparse
import subprocess
import os
import json as _json
import threading
import queue


class MCPClient:
    def __init__(self) -> None:
        self._log = get_logger("MCPClient")
        self._timeout = max(500, int(getattr(settings, "MCP_HTTP_TIMEOUT_MS", 4000))) / 1000.0
        # 持久 stdio 連線快取（以 command+args+sorted(env) 做 key）
        self._stdio_sessions: dict[str, _StdioSession] = {}
        self._stdio_idle_sec: int = int(getattr(settings, "MCP_STDIO_IDLE_SEC", 120) or 120)
        self._janitor_started = False
        # 安全與限額
        try:
            allow = getattr(settings, 'MCP_STDIO_ALLOWED_CMDS', '') or ''
            self._stdio_allowed = [s.strip() for s in str(allow).split(',') if s.strip()]
        except Exception:
            self._stdio_allowed = []
        try:
            self._stdio_max_sessions = int(getattr(settings, 'MCP_STDIO_MAX_SESSIONS', 8) or 8)
        except Exception:
            self._stdio_max_sessions = 8

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

    def discover_tools(self, *, base_url: str, auth: Dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """盡力探索遠端 MCP 的工具描述。

        優先順序：
        1) 既有 HTTP GET `/tools`
        2) JSON-RPC `tools/list`
        3) JSON-RPC `tools.list`
        """
        tools = self.list_tools(base_url=base_url, auth=auth)
        if tools:
            return tools
        for method in ('tools/list', 'tools.list'):
            try:
                rpc = self.rpc_call(base_url=base_url, method=method, params={}, auth=auth)
                if not rpc.get('ok'):
                    continue
                result = rpc.get('result')
                if isinstance(result, dict) and isinstance(result.get('tools'), list):
                    return result.get('tools') or []
                if isinstance(result, list):
                    return result
            except Exception as e:
                self._log.warning('mcp.discover_tools.rpc_failed', method=method, error=str(e))
                continue
        return []

    def discover_tools_stdio(self, *, command: str, args: list[str] | None = None, env: Dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """以 stdio 啟動正式 MCP server，完成 initialize 後嘗試 tools/list。"""
        if not command or not isinstance(command, str):
            return []
        if self._stdio_allowed and (command not in getattr(self, '_stdio_allowed', [])):
            return []
        argv = [command] + (args or [])
        env_map = os.environ.copy()
        if isinstance(env, dict):
            for k, v in env.items():
                if isinstance(k, str) and isinstance(v, (str, int, float)):
                    env_map[k] = str(v)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env_map,
            )
        except Exception as e:
            self._log.warning('mcp.discover_tools_stdio.spawn_failed', error=str(e), command=command)
            return []

        try:
            assert proc.stdin is not None and proc.stdout is not None
            read_timeout = max(self._timeout, 30.0)
            # 1) initialize
            init_req = {
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2024-11-05',
                    'capabilities': {},
                    'clientInfo': {'name': 'dyagent', 'version': '0.1.0'},
                },
            }
            self._write_rpc_stdio(proc.stdin, init_req)
            init_resp = self._read_rpc_stdio_timeout(proc.stdout, read_timeout)
            if not isinstance(init_resp, dict) or 'result' not in init_resp:
                return []
            # 2) initialized notification（無回應）
            self._write_rpc_stdio(proc.stdin, {
                'jsonrpc': '2.0',
                'method': 'notifications/initialized',
                'params': {},
            })
            # 3) tools/list（官方）
            for method, req_id in (('tools/list', 2), ('tools.list', 3)):
                try:
                    self._write_rpc_stdio(proc.stdin, {'jsonrpc': '2.0', 'id': req_id, 'method': method, 'params': {}})
                    resp = self._read_rpc_stdio_timeout(proc.stdout, read_timeout)
                    if isinstance(resp, dict):
                        result = resp.get('result')
                        if isinstance(result, dict) and isinstance(result.get('tools'), list):
                            return result.get('tools') or []
                        if isinstance(result, list):
                            return result
                except Exception as e:
                    self._log.warning('mcp.discover_tools_stdio.list_failed', method=method, error=str(e))
                    continue
            return []
        except Exception as e:
            self._log.warning('mcp.discover_tools_stdio.failed', error=str(e), command=command)
            return []
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    def select_tool_schema(self, *, tools: list[dict[str, Any]], preferred_name: str | None = None) -> dict[str, Any]:
        """從探索結果中挑出最適合的工具與 schema。

        規則：
        - 先找名稱完全等於 preferred_name
        - 再找名稱去空白後相等
        - 否則若只有一個工具取第一個
        - 否則取第一個有 schema 的工具
        """
        def _schema_of(tool: dict[str, Any]) -> dict[str, Any]:
            for key in ('inputSchema', 'input_schema', 'schema', 'parameters'):
                val = tool.get(key) if isinstance(tool, dict) else None
                if isinstance(val, dict):
                    return val
            return {}

        pref = (preferred_name or '').strip()
        chosen: dict[str, Any] | None = None
        if pref:
            for t in tools:
                nm = str((t or {}).get('name') or '').strip()
                if nm == pref:
                    chosen = t
                    break
            if chosen is None:
                for t in tools:
                    nm = str((t or {}).get('name') or '').replace(' ', '')
                    if nm == pref.replace(' ', ''):
                        chosen = t
                        break
        if chosen is None and len(tools) == 1:
            chosen = tools[0]
        if chosen is None:
            for t in tools:
                if _schema_of(t):
                    chosen = t
                    break
        if chosen is None and tools:
            chosen = tools[0]
        if not chosen:
            return {'tool': None, 'input_schema': {}, 'tools': tools}
        return {
            'tool': chosen,
            'input_schema': _schema_of(chosen),
            'tools': tools,
        }

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

    async def stream_rpc_call_stdio(self, *, command: str, args: Optional[list[str]] = None, env: Optional[Dict[str, Any]] = None, method: str, params: Optional[Dict[str, Any]] = None) -> AsyncGenerator[Dict[str, Any], None]:
        """以持久 stdio 連線進行 JSON-RPC 串流呼叫。

        - 若尚無對應進程，會啟動並快取；之後重用。
        - 逐段 yield 讀到的 JSON（dict），直到該 id 的 result/error 出現為止。
        """
        if not command or not isinstance(command, str):
            yield {"ok": False, "error": "invalid_command"}
            return
        # 安全檢查：白名單
        if self._stdio_allowed and (command not in self._stdio_allowed):
            yield {"ok": False, "error": "command_not_allowed"}
            return
        key = self._stdio_key(command, args or [], env or {})
        session = self._stdio_sessions.get(key)
        if session is None or not session.alive:
            # 限額：最大會話數
            alive_count = sum(1 for s in self._stdio_sessions.values() if s and s.alive)
            if alive_count >= self._stdio_max_sessions:
                yield {"ok": False, "error": "too_many_sessions"}
                return
            try:
                session = _StdioSession(command=command, args=args or [], env=env or {})
                session.start()
                self._stdio_sessions[key] = session
                self._maybe_start_janitor()
            except Exception as e:
                yield {"ok": False, "error": f"spawn_failed: {e}"}
                return
        # 發送請求並串流該 id 的訊息
        try:
            session.touch()
            async for frame in session.stream_request(method=method, params=params or {}):
                session.touch()
                yield frame
        except Exception as e:
            yield {"ok": False, "error": f"stdio_stream_failed: {e}"}

    def _stdio_key(self, command: str, args: list[str], env: Dict[str, Any]) -> str:
        try:
            env_items = sorted([(str(k), str(env[k])) for k in env]) if isinstance(env, dict) else []
        except Exception:
            env_items = []
        return _json.dumps({"cmd": command, "args": args, "env": env_items}, sort_keys=True)

    def _maybe_start_janitor(self) -> None:
        if self._janitor_started:
            return
        self._janitor_started = True
        import threading, time

        def _run():
            try:
                while True:
                    time.sleep(30)
                    try:
                        keys = list(self._stdio_sessions.keys())
                        now = time.time()
                        for k in keys:
                            sess = self._stdio_sessions.get(k)
                            if not sess:
                                continue
                            idle = (now - sess.last_used) if sess.last_used else 0
                            if (not sess.in_use) and idle >= self._stdio_idle_sec:
                                try:
                                    sess.close()
                                finally:
                                    self._stdio_sessions.pop(k, None)
                    except Exception:
                        continue
            except Exception:
                pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    # ===== STDIO JSON-RPC (local process) =====
    def _write_rpc_stdio(self, stdin, obj: Dict[str, Any]) -> None:
        try:
            data = _json.dumps(obj, ensure_ascii=False).encode('utf-8')
            header = f"Content-Length: {len(data)}\r\n\r\n".encode('ascii')
            stdin.write(header)
            stdin.write(data)
            stdin.flush()
        except Exception as e:
            raise RuntimeError(f"stdio_write_failed: {e}")

    def _read_rpc_stdio_timeout(self, stdout, timeout_sec: float | None = None) -> Dict[str, Any]:
        q: queue.Queue = queue.Queue(maxsize=1)

        def _worker() -> None:
            try:
                q.put(self._read_rpc_stdio(stdout))
            except Exception as e:
                q.put(e)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        try:
            item = q.get(timeout=timeout_sec or self._timeout)
        except Exception:
            raise RuntimeError('stdio_read_timeout')
        if isinstance(item, Exception):
            raise item
        return item

    def _read_rpc_stdio(self, stdout) -> Dict[str, Any]:
        # 讀取 LSP/MCP 標準的 Content-Length 格式訊息
        try:
            # 讀 header
            headers = b""
            while b"\r\n\r\n" not in headers:
                chunk = stdout.read(1)
                if not chunk:
                    break
                headers += chunk
            if b"Content-Length:" not in headers:
                # 嘗試整行 JSON（容錯）
                line = headers + stdout.readline()
                return _json.loads(line.decode('utf-8').strip() or '{}')
            try:
                head_text = headers.decode('ascii', errors='ignore')
                for line in head_text.split("\r\n"):
                    if line.lower().startswith('content-length:'):
                        length = int(line.split(':', 1)[1].strip())
                        break
                else:
                    length = 0
            except Exception:
                length = 0
            body = stdout.read(length) if length > 0 else b""
            txt = body.decode('utf-8', errors='replace')
            return _json.loads(txt or '{}')
        except Exception as e:
            raise RuntimeError(f"stdio_read_failed: {e}")

    def invoke_stdio(self, *, command: str, args: list[str] | None = None, env: Dict[str, Any] | None = None, method: str = 'tools.invoke', params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """以 subprocess 啟動本機 MCP Server 並透過 stdio 進行單次 JSON-RPC 呼叫。

        注意：此為最小可用版，僅做單次請求/回應；長連線/串流可於後續擴充。
        """
        if not command or not isinstance(command, str):
            return {"ok": False, "error": "invalid_command"}
        if self._stdio_allowed and (command not in getattr(self, '_stdio_allowed', [])):
            return {"ok": False, "error": "command_not_allowed"}
        argv = [command] + (args or [])
        env_map = os.environ.copy()
        if isinstance(env, dict):
            for k, v in env.items():
                if isinstance(k, str) and isinstance(v, (str, int, float)):
                    env_map[k] = str(v)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env_map,
            )
        except Exception as e:
            return {"ok": False, "error": f"spawn_failed: {e}"}

        try:
            req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
            assert proc.stdin is not None and proc.stdout is not None
            self._write_rpc_stdio(proc.stdin, req)
            # 讀取一個回應（帶逾時，避免永久阻塞）
            try:
                timeout_sec = float(getattr(settings, 'MCP_TOOL_TIMEOUT_SEC', 30) or 30)
            except Exception:
                timeout_sec = 30.0
            timeout_sec = max(timeout_sec, self._timeout, 3.0)
            resp = self._read_rpc_stdio_timeout(proc.stdout, timeout_sec=timeout_sec)
            # 結束進程（避免殭屍）
            try:
                proc.kill()
            except Exception:
                pass
            if isinstance(resp, dict):
                if 'result' in resp:
                    return {"ok": True, "result": resp.get('result')}
                if 'error' in resp:
                    return {"ok": False, "error": resp.get('error')}
            return {"ok": False, "error": "invalid_rpc_response"}
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            return {"ok": False, "error": f"stdio_invoke_failed: {e}"}


class _StdioSession:
    """持久 stdio 會話：
    - 啟動一個子進程（MCP Server）
    - 背景執行讀取執行緒，解析 Content-Length 協議訊息
    - 以 JSON-RPC id 映射到每次請求的 async 佇列（拉流）
    """

    def __init__(self, *, command: str, args: list[str], env: Dict[str, Any]):
        self._cmd = command
        self._args = args or []
        self._env = env or {}
        self._proc: Optional[subprocess.Popen] = None
        self._stdin = None
        self._stdout = None
        self._reader: Optional[threading.Thread] = None
        self._alive = False
        self._lock = threading.Lock()
        self._id_counter = 1
        # 每個請求 id 對應一個 thread-safe queue.Queue，用於跨執行緒傳遞訊息
        self._streams: dict[int, queue.Queue] = {}
        self._last_used: float = 0.0
        self._active_ids: list[int] = []
        self._req_meta: dict[int, Dict[str, Any]] = {}

    @property
    def alive(self) -> bool:
        return bool(self._alive and self._proc and (self._proc.poll() is None))

    @property
    def in_use(self) -> bool:
        try:
            return any(True for _ in self._streams.keys())
        except Exception:
            return False

    @property
    def last_used(self) -> float:
        return float(self._last_used or 0.0)

    def touch(self) -> None:
        try:
            import time as _t
            self._last_used = _t.time()
        except Exception:
            self._last_used = 0.0

    def start(self) -> None:
        if self.alive:
            return
        env_map = os.environ.copy()
        for k, v in (self._env or {}).items():
            if isinstance(k, str) and isinstance(v, (str, int, float)):
                env_map[k] = str(v)
        self._proc = subprocess.Popen(
            [self._cmd] + self._args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env_map,
        )
        self._stdin = self._proc.stdin
        self._stdout = self._proc.stdout
        if self._stdin is None or self._stdout is None:
            raise RuntimeError("stdio_handles_unavailable")
        self._alive = True
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        self.touch()

    def _reader_loop(self) -> None:
        try:
            while self.alive:
                msg = self._read_frame()
                if msg is None:
                    break
                self.touch()
                _id = msg.get('id') if isinstance(msg, dict) else None
                if isinstance(_id, int) and _id in self._streams:
                    try:
                        self._streams[_id].put(msg, timeout=0.1)
                    except Exception:
                        pass
                else:
                    # 非配對 id 的訊息（如通知）：嘗試路由給最近啟動的請求，或依 tool 名稱匹配，最後才廣播
                    target_id: int | None = None
                    try:
                        with self._lock:
                            active = list(self._active_ids)
                            metas = dict(self._req_meta)
                        if len(active) == 1:
                            target_id = active[0]
                        else:
                            tool_val = None
                            if isinstance(msg, dict):
                                tool_val = msg.get('tool') or msg.get('name')
                                try:
                                    if not tool_val and isinstance(msg.get('context'), dict):
                                        tool_val = msg['context'].get('tool')
                                except Exception:
                                    pass
                            if isinstance(tool_val, str):
                                for rid in reversed(active):
                                    pm = metas.get(rid) or {}
                                    if isinstance(pm, dict) and (
                                        pm.get('tool') == tool_val or pm.get('name') == tool_val
                                    ):
                                        target_id = rid
                                        break
                            if target_id is None and active:
                                target_id = active[-1]
                    except Exception:
                        target_id = None

                    if target_id is not None:
                        try:
                            q = self._streams.get(target_id)
                            if q is not None:
                                q.put(msg, timeout=0.05)
                        except Exception:
                            pass
                    else:
                        try:
                            for q in list(self._streams.values()):
                                try:
                                    q.put(msg, timeout=0.05)
                                except Exception:
                                    continue
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            self._alive = False

    def _read_frame(self) -> Optional[Dict[str, Any]]:
        # 解析 Content-Length 標頭 + JSON 體
        assert self._stdout is not None
        headers = b""
        # 讀取直到 \r\n\r\n（簡易做法）
        while b"\r\n\r\n" not in headers:
            ch = self._stdout.read(1)
            if not ch:
                return None
            headers += ch
        length = 0
        try:
            text = headers.decode('ascii', errors='ignore')
            for line in text.split("\r\n"):
                if line.lower().startswith('content-length:'):
                    length = int(line.split(':', 1)[1].strip())
                    break
        except Exception:
            length = 0
        body = self._stdout.read(length) if length > 0 else b""
        try:
            return _json.loads(body.decode('utf-8', errors='replace') or '{}')
        except Exception:
            return {"ok": False, "error": "invalid_json"}

    def _send(self, obj: Dict[str, Any]) -> None:
        assert self._stdin is not None
        data = _json.dumps(obj, ensure_ascii=False).encode('utf-8')
        header = f"Content-Length: {len(data)}\r\n\r\n".encode('ascii')
        self._stdin.write(header)
        self._stdin.write(data)
        self._stdin.flush()
        self.touch()

    async def stream_request(self, *, method: str, params: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
        # 建立請求 id 與對應佇列
        with self._lock:
            _id = self._id_counter
            self._id_counter += 1
            q: queue.Queue = queue.Queue()
            self._streams[_id] = q
            try:
                self._req_meta[_id] = dict(params or {})
            except Exception:
                self._req_meta[_id] = {}
            self._active_ids.append(_id)
        # 送出請求
        self._send({"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}})
        # 從 thread-safe queue 輪詢取出訊息並 yield
        try:
            import asyncio
            import time as _time
            terminal = False
            started = _time.monotonic()
            last_item_at = started
            max_idle = float(getattr(settings, 'MCP_STDIO_CALL_IDLE_TIMEOUT_SEC', 25) or 25)
            max_total = float(getattr(settings, 'MCP_STDIO_CALL_TIMEOUT_SEC', 90) or 90)
            while not terminal and self.alive:
                now = _time.monotonic()
                if (now - started) > max_total:
                    yield {'ok': False, 'error': 'stdio_call_timeout'}
                    break
                if (now - last_item_at) > max_idle:
                    yield {'ok': False, 'error': 'stdio_call_idle_timeout'}
                    break
                # 以非同步方式從 queue 取資料（避免阻塞 event loop）
                try:
                    item = await asyncio.to_thread(q.get, True, 0.5)
                except Exception:
                    item = None
                if item is None:
                    continue
                if isinstance(item, dict):
                    last_item_at = _time.monotonic()
                    yield item
                    if ('result' in item) or ('error' in item):
                        terminal = True
                        break
                self.touch()
            if (not terminal) and (not self.alive):
                yield {'ok': False, 'error': 'stdio_session_closed'}
        finally:
            # 請求結束，移除映射；保留進程供後續重用
            with self._lock:
                try:
                    self._streams.pop(_id, None)
                except Exception:
                    pass
                try:
                    self._req_meta.pop(_id, None)
                except Exception:
                    pass
                try:
                    if _id in self._active_ids:
                        self._active_ids.remove(_id)
                except Exception:
                    pass

    def close(self) -> None:
        self._alive = False
        try:
            if self._proc and (self._proc.poll() is None):
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=1.5)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
        except Exception:
            pass
