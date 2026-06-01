# CC_REPORT: f-odysseus-salvage-mcp-client (P3)

**Type:** operator-directed, out-of-band (operator chose "Build both P3 and P4",
2026-06-01, commit→push→PR→merge per change). `NEXT_TASK.md` (phase-5i soak)
untouched.

## What shipped

- **New `app/mcp_client.py`** — a minimal, config-driven external MCP (Model
  Context Protocol) client so CHILI can consume external MCP servers (SEC
  filings, news, broker docs). CHILI was previously not an MCP client at all.
  - Transports: `stdio` and `sse`, via the official `mcp` SDK (guarded import).
  - `MCPClient.connect_all()` / `connect_server()` / `list_tools()` /
    `call_tool()` / `disconnect_all()` / `get_status()`; module singleton
    `mcp_client`.
  - Qualified tool names `mcp__{server_id}__{tool}`; results normalized to
    `{ok, output, error}`.

- **Safety contract (load-bearing for a trading brain).** Two independent gates,
  both pure/unit-tested:
  1. Per-server **allowlist** (`allowed_tools`) — deny-by-default once set.
  2. Hard in-code **denylist** (`is_dangerous_tool`) matching order/trade/buy/
     sell/withdraw/deposit/transfer/wire/pay/fund/liquidate/close_position/
     sign_transaction/... — blocks a tool **even if allowlisted**, and **cannot
     be disabled via config**.
  Both are applied at tool-discovery AND re-applied at call time, so a dangerous
  tool can never reach the server even if it slipped into the registry.

- **Dormant by default.** Config `mcp_enabled: bool = False` +
  `mcp_servers_json: str = ""`. Nothing connects, no behavior changes, until an
  operator both flips the flag and configures servers. The lifecycle is NOT
  auto-wired into app startup/shutdown — deliberately inert.

- **requirements.txt:** `mcp>=1.0` declared and installed into chili-env
  (verified import). Module degrades to disabled if the SDK is absent.

Files: 1 added (`app/mcp_client.py`), 1 test added (`tests/test_mcp_client.py`),
`config.py` + `requirements.txt` modified, backlog updated. No schema, no
migrations, no trading/LLM code touched.

## Verification

- `tests/test_mcp_client.py` (50 cases): denylist blocks 25 dangerous name forms
  and allows benign ones; allowlist blocks unlisted; **denylist beats allowlist**;
  config parsing drops malformed/duplicate/bad-transport/`__`-in-id entries;
  `_finalize_session` filters dangerous tools out of the registry; qualified-name
  construction; successful call output normalization; **call-time policy re-block**
  (a dangerous tool injected into the registry is still refused and never reaches
  the mocked server); error-result surfacing; dormant-when-disabled;
  enabled-requires-flag-AND-SDK. **All 50 pass.** The `mcp` SDK is mocked — no
  live server needed.

## Surprises / deviations

- None notable. The async client methods are exercised from sync tests via
  `asyncio.run()` (no `asyncio_mode` is configured in the repo, so this avoids a
  pytest-asyncio config dependency).

## Deferred

- Not wired into any agent/LLM path — `list_tools()` / `call_tool()` are a ready
  capability. A future task would: (a) connect_all() on startup / disconnect_all()
  on shutdown behind `mcp_enabled`, (b) surface permitted tools to the brain's
  tool layer. Both kept out so this is a pure, inert addition.
- No admin UI for managing servers (odysseus had a DB-backed one); config-JSON is
  enough to start.

## Open questions for Cowork

1. When wiring into the brain later: should MCP tools be exposed only to
   research/reasoning paths, never to the autotrader decision path? (My
   recommendation: yes — keep MCP strictly on the read/research side.)
2. Is the dangerous-tool denylist conservative enough, or should it also block
   by server-declared annotations (e.g. MCP tool `destructiveHint`) when present?
