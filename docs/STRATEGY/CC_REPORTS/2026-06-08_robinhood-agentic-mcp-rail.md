# CC_REPORT: robinhood-agentic-mcp-rail

**Date:** 2026-06-08
**Initiative:** Take advantage of Robinhood's agentic-trading launch — sanctioned MCP execution rail for CHILI's equity arms.
**Note:** Operator-direct task ("research the Robinhood agentic-trading news, then do the best thing"), not the standing `NEXT_TASK.md`. Built in an isolated worktree off `origin/main` (a parallel codex agent had switched the shared working tree's branch mid-session — exactly the `feedback_sync_before_change` hazard).

## What shipped

- **Commit / PR:** `1fa4d8b` → **[PR #532](https://github.com/MiacoRindolf/chili-home-copilot/pull/532)** (branch `chili/robinhood-agentic-mcp`).
- **One-line:** Add a sanctioned **`robinhood_agentic_mcp`** execution family — a deterministic MCP client + VenueAdapter that routes equity orders through Robinhood's official Agentic Trading MCP rail (no LLM in the loop), inert until the operator opens/funds an Agentic account and provides a token.
- **Files (7):** `venue/rh_mcp_client.py` (new), `venue/robinhood_mcp.py` (new), `execution_family_registry.py` (mod), `config.py` (mod), `scripts/rh_agentic_introspect.py` (new), `tests/test_robinhood_agentic_mcp.py` (new, 18 tests), `docs/DESIGN/ROBINHOOD_AGENTIC_MCP.md` (new).
- **Migrations:** none.

### The opportunity (researched, not assumed)

Robinhood launched **Agentic Trading** on 2026-05-27 — the first *officially-sanctioned* path for automated trading. Live probe (2026-06-08) confirmed: endpoint `https://agent.robinhood.com/mcp/trading`; OAuth 2.1 protected-resource per **RFC 9728** (`/.well-known/oauth-protected-resource` advertises auth server + scope `internal`; unauth → `401` + `WWW-Authenticate: Bearer`); standard MCP **Streamable-HTTP** transport (`Mcp-Session-Id`). Equities-only beta; isolated **Agentic account** (separate funds, one-tap kill, per-trade push + P&L). Crypto/options/futures "coming soon".

The consumer framing is "let a chatbot trade for you." CHILI is the opposite — a deterministic, policy-bound brain. So the play is **not** to become the naive RH agent; it is to use RH's sanctioned **RAIL** while keeping CHILI's **BRAIN**. CHILI's equity execution today rides the *unofficial* `robin_stocks` private API (ToS-gray, fragile, bot-flag/ban risk); the MCP rail de-risks exactly that. And the isolated Agentic account is a structurally-bounded **live sandbox** that fits the momentum lane's M5 flip-live-on (and the operator's live+on / limited-blast-radius style).

### The design (rail vs brain; one chokepoint; isolated unknowns)

- **`rh_mcp_client.py`** — deterministic MCP Streamable-HTTP client (`initialize` / `tools/list` / `tools/call`, session + bearer, SSE-or-JSON parse). 100% knowable from the MCP spec + the probe; transport is injectable, so it is fully unit-tested without network. **No LLM in the loop** — determinism preserved.
- **`robinhood_mcp.py`** — `RobinhoodAgenticMcpAdapter` implements the existing `VenueAdapter` protocol. **Execution + account/position** go through the MCP rail (tools resolved by capability-matching a live `tools/list`); **market data delegates to the proven `robinhood_spot` adapter** (quotes aren't the rail's purpose, and the spot adapter already does fill-venue-accurate equity quotes with the Legend/BOATS overnight fallback). This minimizes the unknown surface.
- The two things RH hasn't published — exact **tool names** and **request/response field names** — are isolated in `_TOOL_HINTS` / `_ARG_KEYS` / `_RESP_KEYS` (+ a `tool_map` override), finalized in one place against the introspection dump. Until a capability resolves to a real tool, execution methods **fail loud** rather than guess.
- **Routing:** new `robinhood_agentic_mcp` family in `execution_family_registry`. Equities route to it only when `chili_equity_execution_rail` selects it **AND** a token is present, else `robinhood_spot`. Default is `robinhood_spot` (unchanged live behavior). Token-presence is the activation switch — a real dependency, not a default-OFF dark flag (`feedback_no_dark_flags`); rail selection is a conscious account-routing choice (which account trades).

## Verification

- **Unit:** 18 new tests (mock MCP transport): client initialize/session/initialized, tools/list pagination, tools/call JSON + **SSE**, 401→`unauthorized`, no-token→`no_token`; adapter is_enabled gating, capability keyword-match + override, place-order arg-build + normalize, unresolved-tool fail-loud, list-orders normalize/filter; registry adaptive routing (default spot / MCP-when-selected+token / ignored-without-token), factory + venue mapping. **38 existing** tests green (execution-family registry/routing + equity broker-readiness/sizing). Compile + import smoke clean (default endpoint resolves).
- **No live calls.** Live finalization is blocked on operator OAuth (by design + financial-safety rule).

## Surprises / deviations

- The MCP `tools/list` schema is not public yet, so the adapter is **discovery-driven** (capability matching) rather than hardcoded — robust to the real tool names, finalized via `scripts/rh_agentic_introspect.py`.
- Built off `origin/main` (#531) in a fresh worktree because the shared tree's branch had been switched by a parallel codex agent mid-session.

## Deferred (design P1/P2 — needs the operator's account)

- **Operator step:** open + fund a dedicated Agentic account via desktop OAuth (`claude mcp add robinhood-trading …`), provide a bearer token, run the introspect script.
- **P1:** finalize tool-name map + response field-extraction against the live dump; confirm OAuth **token lifetime** (short-lived → headless service needs a refresh strategy / own OAuth client registration).
- **P2:** parity-test NormalizedOrder/Fill mapping vs `robinhood_spot`; route the equity momentum arms to the Agentic account (autonomous mode) behind the existing safety stack; measure fills + **latency** vs `robin_stocks` (Ross-style fast entries); wire risk_evaluator venue-readiness for the MCP family (currently the family is accepted as implemented but readiness gating mirrors `robinhood_spot` — verify before live).
- **Future:** crypto rail when RH ships it (CHILI's hot-path).

## Open questions for Cowork

- Appetite to make the MCP rail the **default** equity rail once P1/P2 validate it (retire the unofficial `robin_stocks` order path for equities), or keep it operator-selected per-account?
- Should the Agentic-account sandbox become the home for **all** momentum-lane live equity validation (M5), given its structural blast-radius bound?
