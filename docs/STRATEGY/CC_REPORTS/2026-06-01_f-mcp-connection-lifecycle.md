# CC_REPORT: f-mcp-connection-lifecycle (W3)

**Type:** operator-directed, out-of-band ("continue", 2026-06-01; commit→push→PR→
merge per change). Implements the deferred brief
`docs/STRATEGY/QUEUED/f-mcp-connection-lifecycle.md`. `NEXT_TASK.md` (phase-5i
soak) untouched.

## What shipped

- **`MCPSupervisor`** (new, in `app/mcp_client.py`) — runs all MCP connections
  inside ONE long-lived task, the anyio-safe design the brief specified:
  - `run()` does connect_all → serve a request queue → disconnect_all, so every
    session enter/exit/call happens in the same task/cancel-scope. This avoids the
    "exit cancel scope in a different task" error that naive startup/shutdown
    wiring hits with the MCP SDK.
  - `start()` (idempotent; creates the sync primitives synchronously so a caller
    that awaits `wait_ready()`/`call()` immediately never races a not-yet-scheduled
    `run()`), `wait_ready()`, `call(qualified_name, args, timeout)` (queue +
    future), `stop()` (signals shutdown, closes connections in-scope), `status()`.
  - Module singleton `mcp_supervisor` + `get_mcp_supervisor()`.

- **Lifespan wiring** (`app/main.py`) — gated on `settings.mcp_enabled` and skipped
  under `CHILI_PYTEST`: starts the supervisor before `yield`, awaits
  `supervisor.stop()` after `yield`. Inert at the default config (`mcp_enabled=
  False`) — nothing starts.

- **Status endpoint** (`app/routers/brain.py`) — `GET /api/brain/mcp/status` now
  reports `supervisor_running` and reads live status from the supervisor's client.

## Verification

- `tests/test_mcp_client.py` supervisor suite (mock client, no real server):
  start→ready→call→stop lifecycle (connect AND disconnect both happen in-task),
  idempotent start, call-before-start / call-after-stop → "not running", **survives
  a connect_all failure** (ready still set, queue still serves), **call timeout**,
  stop-without-start is safe. Plus the existing policy/config suite and the
  handler-direct status tests. **61 passed** (`test_mcp_client.py` +
  `test_mcp_status_endpoint.py`), ~4s.
- Full-app-boot smoke to confirm the lifespan edit executes cleanly: GREEN.
  A direct `TestClient(app)` startup+shutdown smoke printed
  `LIFESPAN_SMOKE_OK` (exit 0), and a clean isolated re-run of
  `test_reasoning_research_report.py` passed (exit 0). An earlier concurrent run
  showed fixture-setup ERRORs that were environmental (this session ran ~10+
  full-app-boots in parallel; 13–15 min runtimes were the tell) — not a
  regression. The MCP startup block is gated on `CHILI_PYTEST` + `mcp_enabled`,
  so it is provably skipped under pytest regardless.

## Surprises / deviations

- First run had 3 supervisor failures from a real race: `start()` scheduled
  `run()` but returned before it executed, so the queue/stop/ready Events (created
  inside `run()`) were still None when `wait_ready`/`call` checked them. Fixed by
  creating the sync primitives in `start()` (synchronously, before the task runs).
  Good catch by the lifecycle tests.

## Deferred

- A real end-to-end test against a live/stub MCP server (the brief's stretch
  goal). The supervisor's same-task ownership is structural; the mock-client tests
  cover the lifecycle/queue/timeout logic. A stub-server soak can come when a real
  external MCP server is provisioned.
- Bounded auto-reconnect for a server that dies mid-run (odysseus had this for
  builtins) — noted in the brief; not needed until a live server exists.

## Open questions for Cowork

1. Provision a stub MCP server in CI to add the end-to-end test, or wait for a real
   external server use case?
2. Confirm the standing guardrail: MCP tools route to research/reasoning only,
   never the autotrader decision path.
