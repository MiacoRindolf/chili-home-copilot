# Robinhood Agentic Trading — sanctioned MCP execution rail

**Status:** spike (foundation landed; live finalization blocked on operator OAuth + funded account)
**Owner:** trading-brain / execution layer
**Related:** `docs/DESIGN/MOMENTUM_LANE.md`, `app/services/trading/venue/protocol.py`, `app/services/trading/execution_family_registry.py`

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

## 6. Open questions (resolve in P1/P2)

- **Token lifetime / refresh.** OAuth tokens are short-lived. Headless persistence needs a refresh strategy
  (own OAuth client registration against the advertised auth server, or a token-refresh side-channel). The
  introspection step tells us the observed lifetime.
- **Tool schema.** Exact tool names, argument shapes, supported order types (market/limit, TIF), and the
  response JSON for orders/fills/positions — captured by the introspect dump, not assumed.
- **Rate limits / latency.** Undocumented. Measure vs `robin_stocks` to confirm the rail is viable for
  Ross-style fast entries (P2).
- **Account selection.** Confirm the tool surface lets us target the Agentic account explicitly and read its
  positions/P&L for reconciliation.
