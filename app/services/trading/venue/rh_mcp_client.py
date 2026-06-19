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

# stdlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

# relative service (token-bundle foundation)
from .rh_oauth import (
    ALLOWED_OAUTH_HOSTS,
    MAX_PLAUSIBLE_EXPIRES_IN,
    REFRESH_SKEW_SECONDS,
    TOKEN_HTTP_TIMEOUT,
    NeedsReauth,
    TokenBundle,
    load_bundle,
    write_bundle_atomic,
)

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
    """Raised for Robinhood Agentic MCP transport / protocol failures.

    ``__repr__`` deliberately OMITS ``.raw`` so a token-endpoint error (whose
    body could contain token material) can never leak through a traceback or an
    f-string. Token-endpoint failures are always constructed with ``raw=None``.
    """

    def __init__(self, message: str, *, code: str | None = None, raw: Any = None):
        super().__init__(message)
        self.code = code
        self.raw = raw

    def __repr__(self) -> str:
        return f"RhMcpError(code={self.code!r})"


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


def _resolve_token_file_path() -> str:
    """The configured token-file path: env > settings (empty string if neither)."""
    path = os.environ.get("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN_FILE") or ""
    if not path:
        try:
            from ....config import settings

            path = getattr(settings, "chili_robinhood_agentic_mcp_token_file", "") or ""
        except Exception:
            path = ""
    return path


def _read_token_from_file(path: str) -> Optional[str]:
    """Read a bearer string from the token file, JSON-bundle-aware.

    If the file content (stripped) starts with ``{`` it is a TOKEN BUNDLE and MUST
    be parsed via ``rh_oauth.load_bundle`` to extract the access_token — we NEVER
    return the raw JSON as a bearer (that would leak the refresh_token as the
    Authorization header). A bundle that fails to parse / lacks an access_token
    returns None (fail-closed). Only NON-JSON content is a legacy raw token.
    """
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        logger.debug("[rh_mcp_client] token file not found path=%s", path)
        return None
    except Exception as exc:
        logger.warning("[rh_mcp_client] token file read failed path=%s err=%s", path, type(exc).__name__)
        return None
    stripped = (raw or "").strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        bundle = load_bundle(path)
        if bundle is None or not bundle.access_token:
            return None
        return bundle.access_token
    # Legacy raw-string token (not JSON) — return as-is.
    return stripped


def resolve_mcp_token(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve a bearer token: explicit arg > env var > token file (env or settings).

    The token is **the** activation switch for the rail — present token == enabled.
    No separate default-OFF flag gates a configured token (a missing token is a real
    dependency, not a dark flag).

    The explicit-arg and env branches still return raw strings (the legacy path is
    preserved byte-for-byte); only the FILE branch is bundle-aware (a JSON bundle
    resolves to its access_token, never the raw JSON).
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN")
    if env and env.strip():
        return env.strip()
    return _read_token_from_file(_resolve_token_file_path())


def resolve_token_bundle(path: Optional[str] = None) -> Optional[TokenBundle]:
    """Load the on-disk token BUNDLE (used only by the refreshing token source).

    Returns None if there is no bundle file or it is a legacy raw token. The bundle
    carries the refresh token + rotation metadata needed for headless refresh.
    """
    p = path or _resolve_token_file_path()
    if not p:
        return None
    return load_bundle(p)


def bundle_is_routable(path: Optional[str] = None) -> bool:
    """Cheap, NO-network routability check for the registry gate.

    True iff a bundle loads AND has an access_token AND has a refresh_token AND is
    not ``is_hard_dead()``. A dead/refreshless bundle must NOT select the agentic
    rail (it can never recover headlessly), so this gates rail selection without a
    network round-trip in the hot path.
    """
    bundle = resolve_token_bundle(path)
    if bundle is None:
        return False
    if not bundle.access_token or not bundle.has_refresh_token():
        return False
    return not bundle.is_hard_dead(now=time.time())


def _assert_https_allowed_host(url: str) -> None:
    """Refuse to send credentials anywhere but an https allow-listed RH host.

    A misconfigured endpoint (or a redirect target) that would carry the bearer or
    the refresh token off-host is rejected BEFORE the request leaves the process.
    """
    parts = urlsplit(url or "")
    if parts.scheme != "https":
        raise RhMcpError(f"refusing non-https token/transport URL scheme={parts.scheme!r}", code="bad_scheme")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_OAUTH_HOSTS:
        raise RhMcpError("refusing off-host token/transport URL (not in RH allow-list)", code="bad_host")


def _default_http_post(url: str, headers: dict, body: str, timeout: float) -> "tuple[int, dict, str]":
    import requests

    # Credentials never leave an allow-listed RH https host; redirects are NEVER
    # followed (a 3xx that would re-send the bearer elsewhere is a transient error).
    _assert_https_allowed_host(url)
    resp = requests.post(
        url, headers=headers, data=body.encode("utf-8"), timeout=timeout, allow_redirects=False
    )
    if 300 <= resp.status_code < 400:
        raise RhMcpError(f"refusing to follow {resp.status_code} redirect on a credentialed call", code="redirect")
    lower = {str(k).lower(): v for k, v in resp.headers.items()}
    return resp.status_code, lower, resp.text


class RhRefreshingTokenSource:
    """Headless OAuth bearer source backed by an on-disk token bundle.

    Single source of the live bearer for the agentic rail. Refreshes proactively
    (skew before expiry) and reactively (on a 401). Multi-process safe via the
    bundle's write-ahead ``pending_refresh`` + a re-read-on-reject recovery path
    (Robinhood rotates the refresh token, so two processes racing a refresh would
    otherwise double-spend a single-use refresh token).

    FAIL-CLOSED: any unrecoverable auth state raises ``NeedsReauth`` (the adapter
    turns that into a ``needs_reauth`` result and reports the rail DISABLED).
    TRANSIENT 5xx/timeout keep the current (still-valid) token and retry next tick.

    Token material is NEVER logged — only ``bundle.redacted()`` / reason strings.
    """

    def __init__(
        self,
        path: str,
        *,
        http_post: HttpPost = _default_http_post,
        clock: Callable[[], float] = time.time,
    ):
        self._path = path
        self._http_post: HttpPost = http_post
        self._clock = clock
        self._lock = threading.Lock()

    # ── public ──────────────────────────────────────────────────────────

    def has_access_token(self) -> bool:
        b = load_bundle(self._path)
        return bool(b and b.access_token)

    def bearer(self, *, force: bool = False) -> str:
        cur = load_bundle(self._path)
        if cur is None:
            raise NeedsReauth("no_bundle")
        if not force and not cur.is_expired(skew=REFRESH_SKEW_SECONDS, now=self._clock()):
            return cur.access_token
        return self._refresh_locked(expected=cur.access_token)

    def invalidate_and_refresh(self) -> str:
        """Force exactly one refresh — used by the reactive 401 path."""
        cur = load_bundle(self._path)
        if cur is None:
            raise NeedsReauth("no_bundle")
        return self._refresh_locked(expected=cur.access_token)

    # ── internals ───────────────────────────────────────────────────────

    def _refresh_locked(self, *, expected: Optional[str]) -> str:
        with self._lock:
            cur = load_bundle(self._path)
            if cur is None:
                raise NeedsReauth("no_bundle")
            # Single-flight: a peer thread may have rotated while we waited for the
            # lock. If the on-disk access token changed and is now usable, take it
            # without a second network call.
            if (
                expected is not None
                and cur.access_token != expected
                and not cur.is_expired(skew=REFRESH_SKEW_SECONDS, now=self._clock())
            ):
                return cur.access_token
            if cur.refresh_token is None:
                raise NeedsReauth("no_refresh_token")

            # Write-ahead: mark pending BEFORE the POST so a crash mid-refresh is
            # visible on the next load (the refresh token may already be consumed).
            try:
                pre = TokenBundle.from_dict(cur.to_dict())
                if pre is not None:
                    pre.pending_refresh = True
                    write_bundle_atomic(self._path, pre)
            except Exception:
                # Best-effort write-ahead; the refresh itself is authoritative.
                pass

            try:
                return self._do_refresh(cur)
            except _RefreshRejected:
                # invalid_grant (400/401). FIRST re-read: another process (or an
                # external rotation) may have already swapped in a fresh refresh
                # token. If so, adopt it rather than forcing re-consent.
                reload = load_bundle(self._path)
                if (
                    reload is not None
                    and reload.refresh_token is not None
                    and reload.refresh_token != cur.refresh_token
                    and reload.access_token
                ):
                    if not reload.is_expired(skew=REFRESH_SKEW_SECONDS, now=self._clock()):
                        return reload.access_token
                    # The adopted refresh token is fresh but its access token expired
                    # — retry the refresh once with the adopted token.
                    try:
                        return self._do_refresh(reload)
                    except _RefreshRejected:
                        raise NeedsReauth("refresh_rejected")
                raise NeedsReauth("refresh_rejected")

    def _do_refresh(self, cur: TokenBundle) -> str:
        """POST the refresh grant (public client → no Authorization / client_secret).

        On 200: rotate (carry-forward refresh token), clamp expires_in, persist.
        On 400/401: raise ``_RefreshRejected`` (caller decides re-read vs fail-closed).
        On 5xx/timeout: ``RhMcpError`` (TRANSIENT — token stays valid until expiry).
        On parse failure: ``RhMcpError`` (no raw).
        """
        from urllib.parse import urlencode

        token_endpoint = cur.token_endpoint
        # URL-encode the form body — OAuth tokens are base64 and routinely contain
        # '+', '/', '=' which would corrupt a raw 'k=v&...' body and 400 the refresh.
        body = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": cur.refresh_token or "",
                "client_id": cur.client_id or "",
                "scope": cur.scope or "",
            }
        )
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        try:
            status, _hdrs, text = self._http_post(token_endpoint, headers, body, TOKEN_HTTP_TIMEOUT)
        except RhMcpError:
            # Already a non-leaking transport error (bad host/scheme/redirect/transport).
            raise
        except Exception:
            # Network failure — TRANSIENT, no raw, token still valid until expires_at.
            raise RhMcpError("refresh_http_transport", code="refresh_transport")

        if status in (400, 401):
            raise _RefreshRejected()
        if status >= 500 or status == 429:
            raise RhMcpError(f"refresh_http_{status}", code=f"refresh_http_{status}")
        if status >= 400:
            # Any other 4xx that is not invalid_grant — treat as fail-closed reauth.
            raise NeedsReauth("refresh_rejected")

        try:
            data = json.loads((text or "").strip())
            if not isinstance(data, dict):
                raise ValueError("non-object token response")
        except Exception:
            raise RhMcpError("refresh_parse", code="refresh_parse")

        access = data.get("access_token")
        if not access or not isinstance(access, str):
            raise RhMcpError("refresh_parse", code="refresh_parse")

        try:
            expires_in = float(data.get("expires_in") or 0.0)
        except (TypeError, ValueError):
            expires_in = 0.0
        if expires_in <= 0 or expires_in > MAX_PLAUSIBLE_EXPIRES_IN:
            # Implausible / missing TTL → treat as already-expired so we re-refresh.
            expires_in = 0.0

        now = self._clock()
        rotated = TokenBundle.from_dict(cur.to_dict())
        if rotated is None:  # pragma: no cover - cur came from a valid bundle
            raise RhMcpError("refresh_parse", code="refresh_parse")
        rotated.access_token = access
        # Rotation carry-forward: a new refresh token replaces, else keep the old one.
        rotated.refresh_token = data.get("refresh_token") or cur.refresh_token
        new_scope = data.get("scope")
        if isinstance(new_scope, str) and new_scope.strip():
            rotated.scope = new_scope.strip()
        rotated.expires_at = now + expires_in
        rotated.obtained_at = now
        rotated.pending_refresh = False
        write_bundle_atomic(self._path, rotated)
        return access


class _RefreshRejected(Exception):
    """Internal: the token endpoint rejected the refresh grant (invalid_grant)."""


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
        token_source: Optional[RhRefreshingTokenSource] = None,
    ):
        self.endpoint = resolve_mcp_endpoint(endpoint)
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

        # Auth resolution: an explicit token/env (legacy static string) OR an
        # on-disk bundle (refreshing source). When the configured token FILE is a
        # bundle we build a refreshing source; otherwise we keep the legacy static
        # token so the existing robin-stocks-era path is byte-identical.
        self._token_source: Optional[RhRefreshingTokenSource] = token_source
        self.token: Optional[str] = None
        if self._token_source is None:
            # Explicit/env strings stay legacy-static; only a bundle file refreshes.
            explicit = (token.strip() if (token and token.strip()) else None)
            env = os.environ.get("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN")
            if explicit:
                self.token = explicit
            elif env and env.strip():
                self.token = env.strip()
            else:
                path = _resolve_token_file_path()
                bundle = resolve_token_bundle(path) if path else None
                if bundle is not None:
                    self._token_source = RhRefreshingTokenSource(path, http_post=self._http_post)
                else:
                    self.token = _read_token_from_file(path)

    # ── Public surface ─────────────────────────────────────────────────

    def has_token(self) -> bool:
        if self._token_source is not None:
            try:
                return self._token_source.has_access_token()
            except Exception:
                return False
        return bool(self.token)

    def _bearer(self, *, force: bool = False) -> Optional[str]:
        """Resolve the live bearer: refreshing source (may raise NeedsReauth) or static."""
        if self._token_source is not None:
            return self._token_source.bearer(force=force)
        return self.token

    def ensure_authable(self) -> None:
        """Proactively confirm a usable bearer (raises NeedsReauth if not).

        Used by ``is_enabled()``: a ``NeedsReauth`` propagates (rail DISABLED); a
        TRANSIENT refresh error is swallowed ONLY while the current access token is
        still within its expiry (the lane keeps using it and retries next tick).
        """
        if self._token_source is None:
            if not self.token:
                raise NeedsReauth("no_bundle")
            return
        try:
            self._token_source.bearer()
        except NeedsReauth:
            raise
        except RhMcpError:
            # Transient refresh failure — only OK if the static-on-disk access token
            # is still unexpired; otherwise we cannot authenticate this tick. Use the
            # source's clock so a test (or a mocked clock) is honored consistently.
            bundle = load_bundle(self._token_source._path)
            now = self._token_source._clock()
            if bundle is None or bundle.is_expired(skew=0.0, now=now):
                raise NeedsReauth("refresh_rejected")
            return

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
        if not self.has_token():
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
        bearer = self._bearer()
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        if self._negotiated_version:
            h["MCP-Protocol-Version"] = self._negotiated_version
        return h

    def _rpc(self, method: str, params: Optional[dict] = None, *, is_notification: bool = False) -> Any:
        return self._rpc_inner(method, params, is_notification=is_notification, _already_retried=False)

    def _rpc_inner(
        self,
        method: str,
        params: Optional[dict] = None,
        *,
        is_notification: bool = False,
        _already_retried: bool = False,
    ) -> Any:
        if not self.has_token():
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
            # Reactive refresh: a 401 means the bearer was rejected. If we have a
            # refreshing token source and have not yet retried, force ONE refresh
            # and replay the call. A still-401 after a successful refresh = the
            # GRANT was revoked (not a stale token) → NeedsReauth.
            if self._token_source is not None and not _already_retried:
                self._token_source.invalidate_and_refresh()  # NeedsReauth propagates
                return self._rpc_inner(
                    method, params, is_notification=is_notification, _already_retried=True
                )
            if self._token_source is not None and _already_retried:
                raise NeedsReauth("grant_revoked")
            raise RhMcpError(
                "unauthorized — Robinhood Agentic token missing/expired; re-auth via OAuth",
                code="unauthorized",
                raw=text,
            )
        if status == 404 and method != "initialize" and self._session_id:
            # A session-expiry 404 must NEVER trigger a refresh — re-negotiate only.
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
