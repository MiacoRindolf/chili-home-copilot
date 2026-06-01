# CC_REPORT: f-odysseus-salvage-wiring-w2-mcp-status

**Type:** operator-directed, out-of-band ("yes continue" → wire the dormant
salvage utilities, 2026-06-01; commit→push→PR→merge per change). `NEXT_TASK.md`
(phase-5i soak) untouched. Second wiring follow-up.

## What shipped

- **`GET /api/brain/mcp/status`** (new, `app/routers/brain.py`) — a read-only
  status + config-sanity view for the P3 MCP client. It does NOT establish live
  connections. Returns:
  - `enabled` (the `mcp_enabled` flag), `sdk_present` (is the `mcp` SDK importable),
  - `configured_servers` count + per-server `{id, name, transport, allowed_tools}`
    — **never URLs or env secrets**,
  - `allowlist_blocked_by_denylist`: a config-sanity flag listing any allowlisted
    tool that the hard safety denylist would block anyway (catches operator
    mistakes like allowlisting `place_order`),
  - `live_status`: the client's current per-server status map.

- **Deliberately NOT shipped (deferred to dedicated briefs, not rushed):**
  - **MCP live connection lifecycle** → `docs/STRATEGY/QUEUED/f-mcp-connection-lifecycle.md`.
    The MCP SDK's anyio task-scope rules (a session must be entered+closed in the
    same task) make naive startup/shutdown wiring unsafe; it needs a supervisor
    task that owns all connections. Not appropriate to rush into the live app
    mid-soak for a dormant feature.
  - **Teacher-escalation live hook** → `docs/STRATEGY/QUEUED/f-teacher-escalation-live-hook.md`.
    Needs a failed agent-with-tools turn (user request + tool_results + reply);
    CHILI's single-shot planner doesn't cleanly produce that, so a forced hook
    would be shallow. Brief lays out the fire-and-forget chat-path option.

Files: `app/routers/brain.py` modified (+1 route), 1 test added
(`tests/test_mcp_status_endpoint.py`), 2 QUEUED briefs added, backlog updated. No
schema, no migrations, no live connections, no trading code.

## Verification

- `tests/test_mcp_status_endpoint.py` (4 cases): dormant-by-default (enabled
  False, 0 servers); configured servers reported **without echoing the URL**
  (secret-leak guard); **config-sanity flags a dangerous allowlisted tool**
  (`place_order`) while leaving benign ones; bad-JSON config → empty. All 4 pass.
  The handler takes no request args, so it's tested directly (fast, ~3s) — route
  registration on the `brain` router is already covered by W1's client test.

  (First pass used full-app-boot client tests with `monkeypatch` on the pydantic
  settings object; one errored in teardown. Switched to direct handler calls with
  `patch.object` context managers — faster and avoids the settings-teardown issue.)

## Surprises / deviations

- None. Endpoint is pure config introspection over `mcp_client._load_server_configs`
  + `is_dangerous_tool`.

## Deferred

- The two live-wiring tasks above (lifecycle, teacher hook) — written up as
  briefs for Cowork to schedule deliberately rather than rushed here.

## Open questions for Cowork

1. Schedule `f-mcp-connection-lifecycle` and `f-teacher-escalation-live-hook`, or
   leave both dormant indefinitely? They only pay off once there's a concrete
   external MCP server / a real tool-using agent turn to hook.
2. Should `/api/brain/mcp/status` be surfaced in the Brain admin UI?
