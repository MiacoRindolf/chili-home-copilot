"""Deterministic MCP (Model Context Protocol) client for Robinhood Agentic Trading.

Robinhood exposes an officially-sanctioned agentic-trading rail as an MCP server
(``https://agent.robinhood.com/mcp/trading`` — OAuth 2.1 bearer per RFC 9728,
Streamable-HTTP transport, scope ``internal``). This client speaks that transport
**without an LLM in the loop** — CHILI's deterministic pipeline calls ``tools/call``
directly, so the trading brain stays authoritative and the rail is just a
sanctioned execution endpoint. The inversion: use Robinhood's RAIL, keep CHILI's BRAIN.

Scope: transport only — ``initialize`` / ``tools/list`` / ``tools/call`` + session
+ bearer auth + SSE-or-JSON response parsing. The mapping of specific Robinhood
tool names -> VenueAdapter operations lives in ``robinhood_mcp.py``, driven by a
live ``tools/list`` (see ``scripts/rh_agentic_introspect.py``) so we never guess
the schema.

Auth note: a bearer token is obtained via Robinhood's interactive desktop OAuth
onboarding (e.g. ``claude mcp add robinhood-trading --transport http <endpoint>``).
Tokens are short-lived; persistent headless operation needs OAuth refresh — tracked
as an open question in ``docs/DESIGN/ROBINHOOD_AGENTIC_MCP.md``. This client accepts
a token from an explicit arg, env var, or token file; it does not itself run the
interactive flow.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Irreducible transport defaults (overridable via env/settings — see resolve_* helpers).
DEFAULT_MCP_ENDPOINT = "https://agent.robinhood.com/mcp/trading"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "chili-trading-brain", "version": "0.1.0"}
_PAGINATION_GUARD = 50  # hard cap on tools/list cursor follows (runaway backstop)

# Transport callable: (url, headers, body, timeout) -> (status_code, lower_cased_headers, text).
# Injectable so tests drive the client against a mock without real HTTP.
HttpPost = Callable[[str, dict, str, float], "tuple[int, dict, str]"]


class RhMcpError(Exception):
    """Raised for Robinhood Agentic MCP transport / protocol failures."""

    def __init__(self, message: str, *, code: str | None = None, raw: Any = None):
        super().__init__(message)
        self.code = code
        self.raw = raw


@dataclass
class McpToolResult:
    """Normalized result of a ``tools/call``."""

    is_error: bool
    structured: Optional[dict]  # structuredContent, when the tool returns it
    text: Optional[str]  # concatenated text content blocks
    raw: dict = field(default_factory=dict)

    def data(self) -> Any:
        """Best-effort structured payload: structuredContent > parsed-JSON text > text."""
        if self.structured is not None:
            return self.structured
        if self.text:
            t = self.text.strip()
            if t and t[0] in "{[":
                try:
                    return json.loads(t)
                except Exception:
                    return self.text
            return self.text
        return None


def resolve_mcp_endpoint(explicit: Optional[str] = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("CHILI_ROBINHOOD_AGENTIC_MCP_ENDPOINT")
    if env and env.strip():
        return env.strip()
    try:
        from ....config import settings

        cfg = getattr(settings, "chili_robinhood_agentic_mcp_endpoint", "") or ""
        if cfg.strip():
            return cfg.strip()
    except Exception:
        pass
    return DEFAULT_MCP_ENDPOINT


def resolve_mcp_token(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve a bearer token: explicit arg > env var > token file (env or settings).

    The token is **the** activation switch for the rail — present token == enabled.
    No separate default-OFF flag gates a configured token (a missing token is a real
    dependency, not a dark flag).
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN")
    if env and env.strip():
        return env.strip()
    path = os.environ.get("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN_FILE") or ""
    if not path:
        try:
            from ....config import settings

            path = getattr(settings, "chili_robinhood_agentic_mcp_token_file", "") or ""
        except Exception:
            path = ""
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                tok = fh.read().strip()
                if tok:
                    return tok
        except FileNotFoundError:
            logger.debug("[rh_mcp_client] token file not found path=%s", path)
        except Exception as exc:
            logger.warning("[rh_mcp_client] token file read failed path=%s err=%s", path, exc)
    return None


def _default_http_post(url: str, headers: dict, body: str, timeout: float) -> "tuple[int, dict, str]":
    import requests

    resp = requests.post(url, headers=headers, data=body.encode("utf-8"), timeout=timeout)
    lower = {str(k).lower(): v for k, v in resp.headers.items()}
    return resp.status_code, lower, resp.text


class RhMcpClient:
    """Minimal, deterministic MCP Streamable-HTTP client for the RH agentic rail.

    Thread-safe id allocation; one session per client instance. Reuses the
    ``Mcp-Session-Id`` the server hands back at ``initialize`` for the lifetime
    of the instance, and re-negotiates if the server returns 404 (session expired).
    """

    def __init__(
        self,
        *,
        endpoint: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 15.0,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        http_post: Optional[HttpPost] = None,
    ):
        self.endpoint = resolve_mcp_endpoint(endpoint)
        self.token = resolve_mcp_token(token)
        self.timeout = float(timeout)
        self.protocol_version = protocol_version
        self._http_post: HttpPost = http_post or _default_http_post
        self._session_id: Optional[str] = None
        self._negotiated_version: Optional[str] = None
        self._server_info: dict = {}
        self._server_capabilities: dict = {}
        self._initialized = False
        self._id = 0
        self._lock = threading.Lock()

    # ── Public surface ─────────────────────────────────────────────────

    def has_token(self) -> bool:
        return bool(self.token)

    @property
    def server_info(self) -> dict:
        return dict(self._server_info)

    def connect(self, *, force: bool = False) -> None:
        """initialize handshake + the required ``notifications/initialized``."""
        if self._initialized and not force:
            return
        if force:
            self._session_id = None
            self._initialized = False
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        if isinstance(result, dict):
            self._negotiated_version = result.get("protocolVersion") or self.protocol_version
            self._server_info = result.get("serverInfo") or {}
            self._server_capabilities = result.get("capabilities") or {}
        # Spec-required notification; servers may reject tools/* before it.
        self._rpc("notifications/initialized", is_notification=True)
        self._initialized = True

    def list_tools(self) -> list[dict]:
        """Return the server's advertised tools (follows ``nextCursor`` pagination)."""
        self.connect()
        tools: list[dict] = []
        cursor: Optional[str] = None
        for _ in range(_PAGINATION_GUARD):
            params = {"cursor": cursor} if cursor else {}
            result = self._rpc("tools/list", params) or {}
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> McpToolResult:
        """Invoke ``tools/call`` and normalize the content blocks."""
        self.connect()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}}) or {}
        text_parts: list[str] = []
        for c in result.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                text_parts.append(str(c.get("text")))
        return McpToolResult(
            is_error=bool(result.get("isError", False)),
            structured=result.get("structuredContent"),
            text="\n".join(text_parts) or None,
            raw=result,
        )

    def is_reachable(self) -> bool:
        """True if a fresh initialize handshake succeeds (used by adapter health)."""
        if not self.token:
            return False
        try:
            self.connect(force=True)
            return True
        except Exception as exc:
            logger.debug("[rh_mcp_client] is_reachable failed: %s", exc)
            return False

    # ── Transport internals ────────────────────────────────────────────

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        if self._negotiated_version:
            h["MCP-Protocol-Version"] = self._negotiated_version
        return h

    def _rpc(self, method: str, params: Optional[dict] = None, *, is_notification: bool = False) -> Any:
        if not self.token:
            raise RhMcpError(
                "no Robinhood Agentic MCP token configured "
                "(set CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN or a token file)",
                code="no_token",
            )
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not is_notification:
            payload["id"] = self._next_id()

        status, headers, text = self._http_post(
            self.endpoint, self._headers(), json.dumps(payload), self.timeout
        )

        sid = headers.get("mcp-session-id")
        if sid:
            self._session_id = sid

        if status == 401:
            raise RhMcpError(
                "unauthorized — Robinhood Agentic token missing/expired; re-auth via OAuth",
                code="unauthorized",
                raw=text,
            )
        if status == 404 and method != "initialize" and self._session_id:
            self._initialized = False
            self._session_id = None
            raise RhMcpError("MCP session expired (404)", code="session_expired", raw=text)
        if status >= 400:
            raise RhMcpError(f"MCP HTTP {status} for {method}", code=f"http_{status}", raw=text)

        if is_notification:
            return None

        msg = self._extract_jsonrpc(text, headers.get("content-type", ""))
        if msg is None:
            raise RhMcpError(f"empty JSON-RPC response for {method}", code="empty", raw=text)
        err = msg.get("error")
        if err:
            raise RhMcpError(
                str(err.get("message") or "MCP error"), code=str(err.get("code")), raw=err
            )
        return msg.get("result")

    @staticmethod
    def _extract_jsonrpc(text: str, content_type: str) -> Optional[dict]:
        text = text or ""
        ct = (content_type or "").lower()
        looks_sse = (
            "text/event-stream" in ct
            or text.lstrip().startswith("event:")
            or text.lstrip().startswith("data:")
            or "\ndata:" in text
        )
        if looks_sse:
            return RhMcpClient._parse_sse_jsonrpc(text)
        t = text.strip()
        if not t:
            return None
        try:
            return json.loads(t)
        except Exception:
            return RhMcpClient._parse_sse_jsonrpc(text)

    @staticmethod
    def _parse_sse_jsonrpc(text: str) -> Optional[dict]:
        """Pull the JSON-RPC response out of an SSE body (data: frames separated by blank lines)."""
        messages: list[dict] = []
        data_lines: list[str] = []

        def _flush() -> None:
            if not data_lines:
                return
            try:
                obj = json.loads("\n".join(data_lines))
                if isinstance(obj, dict):
                    messages.append(obj)
            except Exception:
                pass

        for raw_line in (text or "").splitlines():
            line = raw_line.rstrip("\r")
            if line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
            elif line == "":
                _flush()
                data_lines = []
        _flush()

        for m in reversed(messages):
            if "result" in m or "error" in m:
                return m
        return messages[-1] if messages else None


def get_default_client() -> RhMcpClient:
    """Build a client from settings/env (timeout sourced from settings when available)."""
    timeout = 15.0
    try:
        from ....config import settings

        timeout = float(getattr(settings, "chili_robinhood_agentic_mcp_timeout_seconds", 15.0))
    except Exception:
        pass
    return RhMcpClient(timeout=timeout)
