# Robinhood Agentic Trading — sanctioned MCP execution rail

**Status:** headless-refresh + safety core landed (INERT until operator runs the one-time consent + pins the account); live flip still operator-gated
**Owner:** trading-brain / execution layer
**Related:** `docs/DESIGN/MOMENTUM_LANE.md`, `app/services/trading/venue/protocol.py`, `app/services/trading/execution_family_registry.py`, `app/services/trading/venue/rh_oauth.py`, `app/services/trading/venue/rh_mcp_client.py`, `app/services/trading/venue/robinhood_mcp.py`, `app/services/trading/venue/rh_agentic_orphan_sweep.py`, `scripts/rh_agentic_oauth.py`

## 1. What Robinhood shipped (2026-05-27)

Robinhood launched **Agentic Trading** — the **first officially-sanctioned path for
automated/agentic order placement** on Robinhood. Any MCP-compatible agent connects to
a hosted MCP server and can place real orders.

Verified live (probe, 2026-06-08):

- **Endpoint:** `https://agent.robinhood.com/mcp/trading` (envoy + CloudFront).
- **Auth:** OAuth 2.1 protected resource per **RFC 9728**. `GET /.well-known/oauth-protected-resource`
  returns `{"authorization_servers":["https://agent.robinhood.com/mcp/trading"],
  "bearer_methods_supported":["header"],"resource":".../mcp/trading","scopes_supported":["internal"]}`.
  Unauthenticated calls get `401` with
  `WWW-Authenticate: Bearer resource_metadata="https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading"`.
- **Transport:** standard MCP **Streamable HTTP** — `Mcp-Session-Id` header, `GET/POST/OPTIONS/DELETE`,
  bearer in `Authorization` header.
- **Account model:** trades land in a dedicated, isolated **Agentic account** — separate from the
  primary portfolio; the agent only touches funds deposited there. One-tap disconnect (kill switch),
  per-trade push notification, live activity + P&L feed in-app. Up to 10 self-directed accounts incl. this one.
- **Assets:** **equities only** in beta. Options / crypto / event-contracts / futures "coming soon".
- **Modes:** review (approve each action) vs autonomous (no per-trade confirmation). User bears full liability.

## 2. Why this matters for CHILI — rail vs brain

The consumer framing ("let a chatbot trade for you") is the naive use. CHILI is the opposite end:
a deterministic, policy-bound trading brain (kill switch, drawdown breaker, CPCV, broker reconciliation).

**So the opportunity is not to become the naive RH agent — it is to use Robinhood's new sanctioned
RAIL while keeping CHILI's BRAIN.** Decouple *the rail* (where orders go) from *the agent* (who decides).

Two concrete wins:

1. **Sanctioned execution rail.** CHILI's equity execution today rides the **unofficial** `robin_stocks`
   reverse-engineered private API (`venue/robinhood_spot.py` -> `broker_service`). That path is ToS-gray,
   fragile (breaks on endpoint/MFA/device-token changes), and carries bot-flag/ban risk. The MCP rail is
   the first *blessed* path -> de-risks ToS/ban exposure for the equity arms.
2. **Structurally-bounded live sandbox.** The isolated Agentic account (pre-funded, capped, one-tap kill)
   is exactly the bounded-blast-radius live environment the momentum lane needs for its **M5 flip-live-on**
   — real fills, bounded downside, sanctioned by the venue.

**Determinism is preserved:** MCP tools are plain JSON-RPC over HTTP. CHILI calls `tools/call` directly
from its deterministic pipeline — **no LLM in the loop**. The brain stays authoritative.

Targeting note: equities-only today maps onto exactly the throughput-constrained **equity arms**
(see the trading-throughput analysis); the crypto hot-path stays on the existing rails until RH ships crypto.

## 3. Architecture

A new execution family + VenueAdapter that conforms to the **existing** `VenueAdapter` protocol — the
neural/momentum brain and the runner/safety stack are unchanged; only the routing target is new.

```
momentum/auto-trader decision  ->  execution_family_registry
                                       |  equity + rail=robinhood_agentic_mcp + token present
                                       v
                              RobinhoodAgenticMcpAdapter (VenueAdapter)
                                       |  deterministic tools/call (no LLM)
                                       v
                                  RhMcpClient  --bearer-->  agent.robinhood.com/mcp/trading
                                       (isolated Agentic account)
```

Components (this spike):

- **`venue/rh_mcp_client.py`** — deterministic MCP Streamable-HTTP client: `initialize` / `tools/list` /
  `tools/call`, session + bearer auth, SSE-or-JSON response parse. Transport is injectable for tests.
  *Fully knowable from the MCP spec + the live probe — no schema guessing here.*
- **`venue/robinhood_mcp.py`** — `VenueAdapter` impl. `is_enabled()` gates on **token presence** (a real
  dependency, not a dark flag). Discovers tools via a live `tools/list` and **capability-matches** them to
  adapter operations (place order / list orders / positions / quote) so it is robust to the exact RH tool
  names. The thin field-extraction in the normalize helpers is finalized against the introspection dump.
- **`execution_family_registry.py`** — new `robinhood_agentic_mcp` family + adapter factory; equities route
  to it when selected (`chili_equity_execution_rail`) **and** the adapter is enabled (token present),
  else fall back to `robinhood_spot`.
- **`scripts/rh_agentic_introspect.py`** — operator-run; dumps the real `tools/list` schema to
  `logs/rh_agentic_tools.json`.

## 4. Phased plan

- **P0 — Foundation (this PR):** client + adapter scaffold + registry/config wiring + introspection script
  + unit tests (mock MCP server). No live calls.
- **P1 — Introspect (operator, ~5 min + me):** operator connects + opens/funds a small Agentic account +
  provides a token; runs the introspect script. We finalize the tool-name map + normalize field-extraction
  against the real schema.
- **P2 — Parity + go-live in the sandbox:** parity-test the MCP rail's NormalizedOrder/Fill mapping vs
  `robinhood_spot` shapes; route the equity momentum arms to the Agentic account (autonomous mode) with the
  existing safety stack in front; measure fills + latency vs `robin_stocks`.
- **P3 — Harden:** OAuth token refresh for headless operation; reconciliation against the Agentic-account
  activity feed; expand to crypto when RH ships it.

## 5. Operator runbook (the part only you can do)

1. **Connect** (desktop): `claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`
   -> `/mcp` -> authenticate. Complete onboarding to **open + fund** a dedicated Agentic account (start small).
2. **Provide a token** to CHILI's service: `set CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN=<bearer>` (or a token file
   via `CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN_FILE` / `settings.chili_robinhood_agentic_mcp_token_file`).
3. **Introspect:** `conda run -n chili-env python scripts/rh_agentic_introspect.py` -> share
   `logs/rh_agentic_tools.json`.
4. **Select the rail** when ready: `settings.chili_equity_execution_rail = "robinhood_agentic_mcp"`.
   Equities then route to the Agentic account; everything else is unchanged.

CHILI cannot open the account, run the OAuth flow, fund it, or place the first live order on your behalf —
those are yours by design (and by the financial-safety rule).

## 6. Headless OAuth refresh — RESOLVED (the P3 token-refresh open question)

Headless persistence is built. The bearer is short-lived; CHILI runs OAuth **refresh** out of a
self-contained on-disk **token bundle** (access + refresh + rotation metadata + endpoints + client_id).

- **Consent (operator, ONE time, on the desktop):** `scripts/rh_agentic_oauth.py` runs the full
  authorization-code + PKCE (S256) flow as a **public client** (`token_endpoint_auth_method=none`):
  RFC 7591 dynamic registration → PKCE → print the auth URL → capture the loopback callback (or `--paste`
  the URL headless) → exchange the code → write the bundle via `rh_oauth.write_bundle_atomic`. It prints
  **only** `bundle.redacted()` — never the access/refresh token, the auth code, or the code_verifier.
- **Refresh (headless, in-process):** `RhRefreshingTokenSource` refreshes proactively (300s skew before
  expiry) and reactively (on a transport 401 → one forced refresh + replay). It is:
  - **single-flight** (in-process `threading.Lock`; a peer thread that already rotated is detected and its
    token reused without a 2nd network call);
  - **multi-process safe** (write-ahead `pending_refresh`; on an `invalid_grant` rejection it re-reads the
    bundle and adopts a peer's externally-rotated refresh token rather than forcing re-consent);
  - **fail-closed** (`NeedsReauth` on no-bundle / no-refresh-token / refresh-rejected / grant-revoked →
    the rail reports DISABLED and **never** places an order with stale/ambiguous auth);
  - **transient-tolerant** (5xx / 429 / network keep the still-valid token and retry next tick).
- **Transport hardening:** every token/transport POST asserts `https` + host ∈ RH allow-list and
  `allow_redirects=False`; a 3xx on a credentialed call is a transient error, never followed. Token-endpoint
  errors carry **no** raw body (`RhMcpError.__repr__` omits `.raw`), so a traceback can't leak token text.
- **Bundle storage (out of repo by default):** `rh_oauth.default_bundle_path()` resolves OUTSIDE the repo
  (`%LOCALAPPDATA%\chili\rh_agentic\token.json` on Windows; `~/.chili/rh_agentic/token.json` /
  `$CHILI_SECRETS_DIR` in the container — mount this into the scheduler). 0600 / owner-only ACL, atomic write.
  `.gitignore` also anchors `*agentic*token*.json`, `*rh_agentic*.json`, `*.client.json`, `.rh_tok_*.tmp`,
  `/secrets/` as defense-in-depth. Override the path via `CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN_FILE`.

## 7. Account pin — the safety latch (blast-radius bound)

Every order on the rail is **structurally** pinned to the isolated Agentic account:

- The pinned account (`CHILI_ROBINHOOD_AGENTIC_MCP_ACCOUNT_NUMBER`, e.g. **674153143**) is frozen at adapter
  construction. `_build_order_args` injects it UNCONDITIONALLY — there is **no parameter** that lets the
  caller/brain supply an account, so a misrouted order to the main portfolio (`5UV17626`,
  `agentic_allowed=false`) is impossible. An empty pin → `no_agentic_account` (order blocked, zero transport).
- Every order/cancel/review/BP-read routes through one chokepoint (`_place` for orders) that (1) asserts the
  pin is on the args, (2) verifies via `get_accounts` that the pinned account's `agentic_allowed == True`
  (latching `_pin_invalid` and reporting the rail DISABLED if not), (3) optionally previews via
  `review_equity_order` and aborts on a HARD pre-trade alert (conservative + fail-open on soft/ambiguous),
  (4) passes `ref_id = client_order_id` (the runner's **deterministic** idempotency token — see §9).

## 8. Tool map (default for `CHILI_ROBINHOOD_AGENTIC_MCP_TOOL_MAP`)

Finalized against the real RH Agentic schema:

```json
{"place_order":"place_equity_order","preview_order":"review_equity_order",
 "cancel_order":"cancel_equity_order","list_orders":"get_equity_orders",
 "get_order":"get_equity_orders","positions":"get_equity_positions","account":"get_accounts"}
```

`place_equity_order(account_number, symbol, side, type[market/limit/stop_market/stop_limit],
quantity|dollar_amount, limit_price, stop_price, time_in_force[gfd/gtc], market_hours[regular_hours/
extended_hours/all_day_hours], ref_id)`; `review_equity_order` = same minus `ref_id`. CHILI defaults TIF
`gfd` (day) / `gtc` (resting limit) and `market_hours=regular_hours`. The adapter still capability-matches
off a live `tools/list` when no override is set, so the keyword hints resolve to these names automatically.

### BP-based sizing (operator-critical)
`risk_policy._account_equity_usd` has a `robinhood_agentic_mcp` branch that reads the agentic account's
**buying_power** (the real, unleveraged spendable amount — the agentic account is a **CASH** account) via
`adapter.get_buying_power_usd()` (`get_accounts` → confirm pin → `get_portfolio` → `buying_power.buying_power`).
The 2× margin multiple (which exists only to recover robin_stocks' under-reporting on the MARGIN main
account) is **NOT** applied here — effective multiple = 1.0 — so CHILI never submits orders exceeding the
cash balance (~$13,800). Fail-open (None → documented fixed cap). The `robinhood_spot` + coinbase branches
are byte-identical.

## 9. Idempotency — deterministic entry order-id (B1)

`live_runner.py` builds the entry `client_order_id` as
`chili_ml_e_{sess.id}_{corr[:8]}_{sha1(f"{sess.id}|{sess.correlation_id}|entry")[:10]}`. The suffix is now
**deterministic** (was `uuid4().hex[:10]`), so a re-submit of the SAME logical entry reuses the SAME id —
with the agentic rail's `ref_id=cid`, RH can dedup and a retried entry can't double-submit. Format/length are
byte-identical to the old form; `robin_stocks` ignores `ref_id` so that path is unchanged.

**Follow-up (NOT this pass):** the exit / scale-out / bailout cids still carry random suffixes. They are
idempotent enough today (CHILI manages those in-process and the agentic rail's exit path is taker-side), but
for full ref_id dedup on those legs they should get the same deterministic treatment before heavy live use.

## 10. B3 residual — agentic-account orphan protection (ACTIVATION BLOCKER)

CHILI's momentum lane places **no broker-side stop** (it polls price in-process, then places a market/limit
exit). The broker-sync reconciler runs a separate `robin_stocks` session on the **MAIN** account and is
**blind to the Agentic account** → a filled agentic position has no reconciler backstop → on a scheduler
restart it is an unmanaged orphan at RH **with no stop**.

`venue/rh_agentic_orphan_sweep.py` (`sweep_agentic_orphans`) is the minimal, agentic-rail-ONLY backstop: it
reads the agentic account's open positions/orders (`get_agentic_open_positions` /
`get_agentic_open_orders(placed_agent="agentic")`) and surfaces (error-level log + structured report) any
momentum position with **no live in-process session** so it can be re-adopted — mirroring the robin_stocks
adoption (`cancel_automation_session` `FILLED_NEEDS_ADOPTION` + `management_scope='momentum_neural'`). It
never touches the robin_stocks reconciler or the main account.

**Residual / blocker:** this is **detect + surface**, not yet a fully-automated continuous reconciliation
loop wired into the runner's restart/adopt path. Before flipping the rail LIVE, the operator must (a) wire
`sweep_agentic_orphans` into the restart/adopt path or a scheduled job, and (b) confirm the adopt branch
re-adopts a real agentic orphan. Until then, a restart with an open agentic position is unprotected.

## 11. Open questions (still to resolve in P1/P2 with live access)

- **Tool schema confirmation.** The default map above is finalized from the documented schema; confirm against
  a live `tools/list` / `review_equity_order` response (alert/severity field names for the HARD-block check).
- **Rate limits / latency.** Undocumented. Measure vs `robin_stocks` to confirm the rail is viable for
  Ross-style fast entries (P2).
- **Token lifetime.** The first live consent reveals the real `expires_in`; the refresh skew (300s) and the
  hard-dead heuristic assume a multi-minute-to-hours TTL — verify and tune if RH issues very short tokens.
