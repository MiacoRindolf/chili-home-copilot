"""MCP (Model Context Protocol) client — let CHILI consume EXTERNAL MCP servers.

CHILI was not previously an MCP client. This adds a minimal, config-driven,
**read-only-by-policy** client so the brain can pull from external MCP tool
servers (e.g. SEC filings, news, broker docs) without bloating the core.

SAFETY CONTRACT (load-bearing for a trading brain):
  CHILI must never let an external MCP server place orders or move money. Two
  independent gates enforce this:
    1. A per-server ALLOWLIST (`allowed_tools`). Deny-by-default once set.
    2. A hard, in-code DENYLIST of dangerous tool-name patterns
       (order/trade/buy/sell/withdraw/transfer/...) that blocks a tool EVEN IF
       it was mistakenly allowlisted. This cannot be disabled via config.
  Both gates are applied at tool-discovery time AND re-applied at call time.

Default state is fully DORMANT: `settings.mcp_enabled` defaults False and
`settings.mcp_servers_json` defaults empty, so nothing connects and no behavior
changes until an operator opts in. The `mcp` SDK import is guarded — absent it,
the client degrades to disabled rather than crashing.

Salvaged/adapted (MIT) from odysseus `src/mcp_manager.py`; reshaped to be
config-driven (not DB-backed), CHILI-native, and trading-safety-gated. This
module is a ready capability — it is intentionally not yet wired into any live
agent path.
"""
from __future__ import annotations

import json
import logging
import os
import re
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

from .config import settings

logger = logging.getLogger(__name__)

# Guarded SDK import — module stays importable (disabled) without `mcp`.
try:
    import mcp as _mcp  # noqa: F401
    _HAS_MCP = True
except Exception:  # pragma: no cover - defensive
    _HAS_MCP = False


# ---------------------------------------------------------------------------
# Safety policy (pure functions — unit-testable without any connection)
# ---------------------------------------------------------------------------

# Tool-name substrings that could move money or place/modify orders. Matched
# case-insensitively against the tool name. A match is ALWAYS blocked, allowlist
# or not. Keep this conservative and broad — false-positives (a blocked benign
# tool) are acceptable; a false-negative (an executable trade tool reaching the
# brain) is not.
_DANGEROUS_TOOL_PATTERNS = re.compile(
    r"(?i)("
    r"place[_-]?order|submit[_-]?order|cancel[_-]?order|modify[_-]?order|"
    r"\border\b|\btrade\b|\bbuy\b|\bsell\b|short[_-]?sell|"
    r"withdraw|deposit|transfer|wire|payout|pay[_-]?out|\bpay\b|send[_-]?money|"
    r"fund|liquidat|close[_-]?position|open[_-]?position|"
    r"approve|sign[_-]?transaction|broadcast[_-]?tx|execute[_-]?trade"
    r")"
)


def is_dangerous_tool(tool_name: str) -> bool:
    """True if the tool name looks like it could place orders / move money."""
    return bool(_DANGEROUS_TOOL_PATTERNS.search(tool_name or ""))


def tool_permitted(server_cfg: Dict[str, Any], tool_name: str) -> bool:
    """Policy gate: may CHILI use this tool from this server?

    Blocked if it looks dangerous (hard denylist), or if the server defines a
    non-empty allowlist and the tool isn't in it.
    """
    if not tool_name:
        return False
    if is_dangerous_tool(tool_name):
        return False
    allowed = server_cfg.get("allowed_tools") or []
    if allowed and tool_name not in allowed:
        return False
    return True


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

def _load_server_configs() -> List[Dict[str, Any]]:
    """Parse settings.mcp_servers_json into a validated list of server configs.

    Drops malformed entries (and ids containing '__', which would break the
    mcp__{id}__{tool} qualified-name scheme) rather than raising.
    """
    raw = (getattr(settings, "mcp_servers_json", "") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception as e:
        logger.warning("[mcp_client] mcp_servers_json is not valid JSON: %s", e)
        return []
    if not isinstance(parsed, list):
        logger.warning("[mcp_client] mcp_servers_json must be a JSON array")
        return []
    out: List[Dict[str, Any]] = []
    seen_ids = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("id") or "").strip()
        transport = str(entry.get("transport") or "").strip().lower()
        if not sid or "__" in sid:
            logger.warning("[mcp_client] skipping server with missing/invalid id: %r", sid)
            continue
        if sid in seen_ids:
            logger.warning("[mcp_client] skipping duplicate server id: %r", sid)
            continue
        if transport not in ("stdio", "sse"):
            logger.warning("[mcp_client] server %r has unsupported transport %r", sid, transport)
            continue
        seen_ids.add(sid)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class MCPClient:
    """Connects to configured MCP servers and exposes their policy-permitted tools."""

    def __init__(self) -> None:
        self._sessions: Dict[str, Any] = {}
        self._stacks: Dict[str, AsyncExitStack] = {}
        self._tools: Dict[str, List[Dict]] = {}      # server_id -> permitted tool dicts
        self._configs: Dict[str, Dict] = {}          # server_id -> config
        self._status: Dict[str, Dict] = {}           # server_id -> status

    def enabled(self) -> bool:
        return bool(getattr(settings, "mcp_enabled", False)) and _HAS_MCP

    async def connect_all(self) -> int:
        """Connect to every configured, enabled server. Returns count connected."""
        if not getattr(settings, "mcp_enabled", False):
            logger.info("[mcp_client] disabled (mcp_enabled=False); not connecting")
            return 0
        if not _HAS_MCP:
            logger.warning("[mcp_client] `mcp` SDK not installed; client disabled")
            return 0
        connected = 0
        for cfg in _load_server_configs():
            if await self.connect_server(cfg):
                connected += 1
        return connected

    async def connect_server(self, cfg: Dict[str, Any]) -> bool:
        sid = cfg["id"]
        self._configs[sid] = cfg
        transport = cfg.get("transport")
        try:
            if transport == "stdio":
                return await self._connect_stdio(sid, cfg)
            if transport == "sse":
                return await self._connect_sse(sid, cfg)
            self._status[sid] = {"status": "error", "error": f"bad transport {transport}"}
            return False
        except Exception as e:
            logger.error("[mcp_client] connect failed for %s: %s", sid, e)
            self._status[sid] = {"status": "error", "error": str(e), "name": cfg.get("name", sid)}
            return False

    async def _finalize_session(self, sid: str, cfg: Dict, session, stack: AsyncExitStack) -> bool:
        await session.initialize()
        tools_result = await session.list_tools()
        permitted, blocked = [], []
        for tool in tools_result.tools:
            entry = {
                "name": tool.name,
                "description": getattr(tool, "description", "") or "",
                "input_schema": getattr(tool, "inputSchema", {}) or {},
            }
            if tool_permitted(cfg, tool.name):
                permitted.append(entry)
            else:
                blocked.append(tool.name)
        self._sessions[sid] = session
        self._stacks[sid] = stack
        self._tools[sid] = permitted
        self._status[sid] = {
            "status": "connected",
            "name": cfg.get("name", sid),
            "transport": cfg.get("transport"),
            "tool_count": len(permitted),
            "blocked_count": len(blocked),
        }
        if blocked:
            logger.info("[mcp_client] %s: blocked %d tool(s) by policy: %s",
                        sid, len(blocked), ", ".join(sorted(blocked)))
        logger.info("[mcp_client] connected %s — %d permitted tool(s)", sid, len(permitted))
        return True

    async def _connect_stdio(self, sid: str, cfg: Dict) -> bool:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = cfg.get("env") or {}
        params = StdioServerParameters(
            command=cfg.get("command"),
            args=cfg.get("args") or [],
            env={**os.environ, **env} if env else None,
        )
        stack = AsyncExitStack()
        read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        return await self._finalize_session(sid, cfg, session, stack)

    async def _connect_sse(self, sid: str, cfg: Dict) -> bool:
        from mcp import ClientSession
        from mcp.client.sse import sse_client
        url = cfg.get("url")
        if not url:
            self._status[sid] = {"status": "error", "error": "sse transport requires url"}
            return False
        stack = AsyncExitStack()
        read_stream, write_stream = await stack.enter_async_context(sse_client(url))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        return await self._finalize_session(sid, cfg, session, stack)

    def list_tools(self) -> List[Dict]:
        """Flat list of all policy-permitted tools across connected servers."""
        out = []
        for sid, tools in self._tools.items():
            name = self._status.get(sid, {}).get("name", sid)
            for t in tools:
                out.append({
                    "server_id": sid,
                    "server_name": name,
                    "name": t["name"],
                    "qualified_name": f"mcp__{sid}__{t['name']}",
                    "description": t["description"],
                    "input_schema": t["input_schema"],
                })
        return out

    def _parse_qualified(self, qualified_name: str) -> Optional[tuple]:
        if not qualified_name.startswith("mcp__"):
            return None
        for sid in self._tools:
            prefix = f"mcp__{sid}__"
            if qualified_name.startswith(prefix):
                return sid, qualified_name[len(prefix):]
        return None

    async def call_tool(self, qualified_name: str, arguments: Optional[Dict] = None) -> Dict:
        """Call an MCP tool by qualified name (mcp__{server_id}__{tool}).

        Re-validates the policy gate at call time. Returns
        {"ok": bool, "output": str, "error": str}.
        """
        arguments = arguments or {}
        parsed = self._parse_qualified(qualified_name)
        if not parsed:
            return {"ok": False, "output": "", "error": f"unknown MCP tool: {qualified_name}"}
        sid, tool_name = parsed

        # Defense-in-depth: re-apply policy at call time, not just discovery.
        if not tool_permitted(self._configs.get(sid, {}), tool_name):
            logger.warning("[mcp_client] BLOCKED call to %s (policy)", qualified_name)
            return {"ok": False, "output": "", "error": f"tool blocked by policy: {tool_name}"}

        session = self._sessions.get(sid)
        if not session:
            return {"ok": False, "output": "", "error": f"server not connected: {sid}"}

        try:
            result = await session.call_tool(tool_name, arguments)
        except Exception as e:
            logger.error("[mcp_client] call failed %s: %s", qualified_name, e)
            return {"ok": False, "output": "", "error": str(e)}

        parts = []
        for content in getattr(result, "content", []) or []:
            if hasattr(content, "text"):
                parts.append(content.text)
            elif hasattr(content, "data"):
                parts.append(str(content.data))
        output = "\n".join(parts)
        is_error = bool(getattr(result, "isError", False))
        return {"ok": not is_error, "output": "" if is_error else output,
                "error": output if is_error else ""}

    async def disconnect_all(self) -> None:
        for sid in list(self._stacks.keys()):
            stack = self._stacks.pop(sid, None)
            if stack:
                try:
                    await stack.aclose()
                except Exception as e:  # pragma: no cover - cleanup best-effort
                    logger.warning("[mcp_client] error closing %s: %s", sid, e)
            self._sessions.pop(sid, None)
            self._tools.pop(sid, None)
            self._status[sid] = {"status": "disconnected",
                                 "name": self._configs.get(sid, {}).get("name", sid)}

    def get_status(self) -> Dict[str, Dict]:
        return dict(self._status)


# Module-level singleton.
mcp_client = MCPClient()


def get_mcp_client() -> MCPClient:
    return mcp_client


# ---------------------------------------------------------------------------
# Supervisor — owns all live connections inside ONE task (anyio-safe)
# ---------------------------------------------------------------------------

class MCPSupervisor:
    """Runs MCP connections inside a single long-lived task.

    The MCP SDK is built on anyio task groups: a session/transport must be
    entered AND closed within the same task/cancel-scope. So all of connect →
    serve-calls → disconnect happen inside `run()`. Request handlers anywhere
    submit calls via a queue; the supervisor services them in-scope. This avoids
    the "exit cancel scope in a different task" error that naive
    startup/shutdown wiring would hit.
    """

    def __init__(self, client: Optional[MCPClient] = None) -> None:
        self._client = client or MCPClient()
        self._queue: "Any" = None  # asyncio.Queue, created in run()'s loop
        self._stop = None          # asyncio.Event, created in run()
        self._task = None          # asyncio.Task
        self._ready = None         # asyncio.Event
        self._started = False

    async def run(self) -> None:
        import asyncio
        try:
            try:
                n = await self._client.connect_all()
                logger.info("[mcp_client] supervisor connected %d server(s)", n)
            except Exception as e:
                logger.error("[mcp_client] supervisor connect_all failed: %s", e)
            finally:
                self._ready.set()

            while not self._stop.is_set():
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if item is None:  # shutdown sentinel
                    break
                qualified_name, arguments, fut = item
                try:
                    result = await self._client.call_tool(qualified_name, arguments)
                    if not fut.done():
                        fut.set_result(result)
                except Exception as e:  # pragma: no cover - call_tool already guards
                    if not fut.done():
                        fut.set_exception(e)
        finally:
            try:
                await self._client.disconnect_all()
            except Exception as e:  # pragma: no cover - cleanup best-effort
                logger.warning("[mcp_client] supervisor disconnect failed: %s", e)

    def start(self) -> None:
        """Spawn the supervisor task on the running event loop. Idempotent.

        The sync primitives are created here (not in run()) so a caller that
        awaits wait_ready()/call() right after start() never races a not-yet-
        scheduled run() and sees them as None.
        """
        import asyncio
        if self._started:
            return
        self._queue = asyncio.Queue()
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._started = True
        self._task = asyncio.create_task(self.run(), name="mcp-supervisor")

    async def wait_ready(self, timeout: float = 10.0) -> bool:
        import asyncio
        if self._ready is None:
            return False
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def call(self, qualified_name: str, arguments: Optional[Dict] = None,
                   timeout: float = 30.0) -> Dict:
        """Submit a tool call to the supervisor and await its result."""
        import asyncio
        if not self._started or self._stop is None or self._stop.is_set():
            return {"ok": False, "output": "", "error": "mcp supervisor not running"}
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        await self._queue.put((qualified_name, arguments or {}, fut))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            if not fut.done():
                fut.cancel()
            return {"ok": False, "output": "", "error": f"mcp call timeout: {qualified_name}"}

    async def stop(self, timeout: float = 10.0) -> None:
        """Signal shutdown and wait for the task to close connections in-scope."""
        import asyncio
        if not self._started or self._task is None:
            return
        if self._stop is not None:
            self._stop.set()
        try:
            if self._queue is not None:
                self._queue.put_nowait(None)  # unblock the get()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except asyncio.TimeoutError:
            self._task.cancel()
        finally:
            self._started = False

    def status(self) -> Dict[str, Dict]:
        return self._client.get_status()


# Live supervisor singleton (started by the app lifespan when mcp_enabled).
mcp_supervisor = MCPSupervisor(client=mcp_client)


def get_mcp_supervisor() -> MCPSupervisor:
    return mcp_supervisor
