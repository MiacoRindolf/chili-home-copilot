# QUEUED: MCP connection lifecycle (the live half of P3)

**Context:** P3 shipped `app/mcp_client.py` (config-driven, safety-gated, dormant)
and W2 shipped a read-only `GET /api/brain/mcp/status` config endpoint. What's NOT
done — deliberately — is establishing and tearing down **live** MCP connections
inside the FastAPI app. This brief is that work.

## Why it was deferred (not rushed)

The MCP Python SDK builds on `anyio` task groups. A `ClientSession` (and the
`stdio_client`/`sse_client` it wraps) must be **entered and closed within the same
async task/cancel-scope**. Naively doing `asyncio.create_task(connect_all())` on
startup and `await disconnect_all()` on shutdown will, in the general case, raise
"Attempted to exit cancel scope in a different task than it was entered in" — a
classic anyio cross-task error. Shipping that half-right into a live trading app
mid-soak was not worth the risk for a dormant feature.

## The correct design

A single long-lived **MCP supervisor task** that owns all connections:

1. On startup (gated on `settings.mcp_enabled`), spawn one supervisor task.
2. The supervisor, inside its own task scope, enters all server connections
   (reusing `MCPClient._connect_stdio` / `_connect_sse`), then awaits a request
   queue.
3. `call_tool` requests are pushed onto the queue (with a future for the result);
   the supervisor services them in-scope and resolves the future. The public
   `MCPClient.call_tool` becomes a thin queue producer.
4. On shutdown, signal the supervisor via an `asyncio.Event`; it closes every
   connection **in its own scope** (no cross-task aclose), then exits.

This keeps every enter/exit in one task and makes call dispatch safe from any
request handler.

## Constraints / safety (unchanged)

- The P3 policy gate (allowlist + hard denylist, re-checked at call time) stays
  exactly as-is. Lifecycle wiring must not weaken it.
- MCP tools go ONLY to read/research paths — never the autotrader decision path
  (operator-confirmed direction).
- Default stays dormant (`mcp_enabled=False`).

## Success criteria

- With a test/stub MCP server configured and `mcp_enabled=1`, the app starts,
  `GET /api/brain/mcp/status` shows it connected, a permitted tool round-trips via
  `call_tool`, a denylisted tool is refused, and shutdown closes cleanly with no
  anyio cancel-scope tracebacks.
- Soak: a server that dies mid-run is reflected in status and doesn't wedge the
  supervisor.

## Reference

- `app/mcp_client.py` (P3) — connection + policy already implemented.
- odysseus `src/mcp_manager.py` had auto-reconnect for builtin servers; consider a
  bounded reconnect in the supervisor.
