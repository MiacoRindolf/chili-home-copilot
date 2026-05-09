# f-coinbase-autotrader-enablement-phase-2-auth-verification

STATUS: QUEUED
SLUG: coinbase-autotrader-enablement-phase-2-auth-verification
PROPOSED: 2026-05-09
SEVERITY: medium (gating prerequisite for Phases 3-7; <1h scope; LOW risk)

## TL;DR

Phase 2 of the Coinbase enablement initiative. **Audit-mostly, with
ONE paper-test order** (place a $5 BTC limit-buy far below market
that won't fill, then immediately cancel) to prove the auth +
order-placement chain works end-to-end without leaving any open
broker exposure.

Phase 1 audit (`39e9807`, 2026-05-09) confirmed the architectural
plan; Phase 2 confirms the credentials are wired and the API
responses match what `coinbase_service.py` expects. If auth is
broken, Phase 3 can't ship.

## Why now

Phase 1 surfaced the architectural plan. Phase 2 is the
prerequisite check before Phase 3 (broker selector + entry
routing) can ship. If Coinbase auth doesn't work in any of chili's
worker processes, Phase 3 would silently route entries that fail
at the broker call — exactly tonight's silent-broker-empty-state
failure mode.

Phase 1 finding worth re-checking in Phase 2: per Section D,
multi-process auth liveness wasn't directly verified — only the
RH path's `_logged_in` cache divergence was documented as a known
risk class. Phase 2's first acceptance criterion forces the
multi-process audit for Coinbase up front.

## Operator-decided design constraints (binding for Phases 3-7)

These were locked in by the operator after Phase 1 (2026-05-09):

1. **Cross-venue position cap: SEPARATE per-venue caps.** Phase 5
   does NOT aggregate same-ticker exposure across venues. Each
   venue has independent caps (e.g.,
   `CHILI_AUTOTRADER_MAX_COINBASE_NOTIONAL_PCT` and
   `CHILI_AUTOTRADER_MAX_RH_NOTIONAL_PCT` separately).
2. **Kill switch: GLOBAL.** One operator-pulled lever stops both
   venues. No per-venue trip granularity.
3. **Selector preference for tickers in BOTH whitelists: RH-first**
   (cost-cheaper). Coinbase routes only for the long tail (tickers
   RH doesn't list).
4. **Fast-path overlap: skip-on-fast-path-active.** Autotrader
   skips Coinbase routing if fast-path is currently active for
   that ticker. No cross-pipeline lock.

These are NOT in scope to implement in Phase 2 — they're documented
here so Phase 3+ briefs cite them as binding.

## Goal

Confirm Coinbase auth + order placement works for the autotrader's
future use. Concretely, audit + paper-verify:

1. **Credentials configured.** `CHILI_COINBASE_API_KEY` /
   `CHILI_COINBASE_API_SECRET` (or equivalent) present in `.env`.
   If missing, surface to operator and stop.
2. **`coinbase_service.is_connected()` returns True** in all four
   worker processes (chili, autotrader-worker, scheduler-worker,
   broker-sync-worker). Multi-process auth liveness verified.
3. **`get_portfolio()` returns the funded $2.2k cash.** Sanity
   check that the API agrees with operator's wallet balance.
4. **`get_positions()` returns Coinbase holdings** (initially
   expected to be empty). Confirms position-fetch surface works
   without authentication errors.
5. **Paper-test order placement and cancellation.** Place a $5
   limit-buy for BTC-USD at 50% below current spot (won't fill in
   any reasonable time). Verify the response is well-formed.
   Immediately cancel via `cancel_order_by_id`. Verify cancel
   response. Confirm zero residual orders via `list_open_orders`.
6. **Document the auth flow and any gotchas.** Output is a CC
   report for operator review BEFORE Phase 3 ships.

## Acceptance criteria

1. Read-only audit covering items 1-4 above (no order placed).
   Surface any failures BEFORE proceeding to step 5.
2. **Paper-test order placement** (item 5) gated explicitly: the
   test only runs if items 1-4 all pass.
3. **Paper-test cancellation** completes within 5 seconds of
   placement. The test must guarantee zero residual open orders
   on Coinbase regardless of how the test exits (use
   `try/finally`).
4. **Multi-process auth check** verified across chili,
   autotrader-worker, scheduler-worker, broker-sync-worker. If
   ANY process fails to connect when others succeed, surface the
   gap.
5. **CC report** at
   `docs/STRATEGY/CC_REPORTS/2026-05-09_f-coinbase-autotrader-enablement-phase-2-auth-verification.md`
   documenting:
   - Each item's pass/fail status
   - The actual `get_portfolio()` and `get_positions()` response
     shapes (sanity-check that they match what
     `coinbase_service.py` expects)
   - The paper-test order's full request + response payload
   - Any gotchas or issues that need surfacing for Phase 3
6. **Zero live trading.** The paper-test order is canceled within
   5s; no fills should occur. If a fill DOES occur (e.g., extreme
   spread compression), the test treats it as a failure and
   surfaces immediately.
7. **Operator's $2.2k cash unchanged** post-test (modulo any
   trivial fee on a partial fill — which shouldn't happen but
   document if it does).
8. **No code changes other than the test script + report.**
   `coinbase_service.py` and the adapter are READ-ONLY in this
   brief. If a bug surfaces (e.g., `get_portfolio` parsing the
   response wrong), surface in the report; the fix is Phase 2.5
   or Phase 3.

## Brain integration (read-only except for the paper-test)

- `app/services/coinbase_service.py` — read; call its public
  surface (is_connected, get_portfolio, get_positions,
  place_buy_order, cancel_order_by_id, list_open_orders).
- `app/services/trading/venue/coinbase_spot.py` — read; can
  optionally exercise the adapter surface as a parallel check.
- `.env` — read for credential variable presence (not values).
- No DB writes; no migrations.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Operator's directive: don't break what works.** RH autotrader
  + RH crypto reconciler chain (Phases A+B+C) untouched.
- **Paper-test order ONLY.** $5 notional, far-below-market limit
  price, immediately canceled. NO market orders, NO live entry.
- **Hard timeout on the cancel.** If the cancel doesn't complete
  within 10s, surface as a CRITICAL failure and stop.
- **Test must clean up.** `try/finally` around the order
  placement; cancel runs unconditionally on any exception.
- **DO NOT auto-promote Phase 3 brief from this brief.** Operator
  approves Phase 3 after reading the Phase 2 report.
- **Edit-tool truncation discipline (HARD).**
- **Tests use `_test`-suffixed DB if any DB interaction.**
- **No magic numbers** — paper-test prices and notional come
  from settings or are documented constants.

## Out of scope

- Broker selector logic (Phase 3).
- Bracket writer Coinbase paths (Phase 4).
- Cost-aware sizing (Phase 5).
- Paper-trade soak (Phase 6 — different from Phase 2's single
  paper-test).
- Live verification (Phase 7).
- Any changes to RH path.
- Any code changes to `coinbase_service.py` or the adapter (read
  only; surface bugs for Phase 3).

## Sequencing

1. Truncation scan on `coinbase_service.py` and
   `coinbase_spot.py`.
2. Item 1: credentials probe (read `.env` for the relevant
   variable names; do NOT print values).
3. Item 2: multi-process `is_connected()` probe via
   `docker exec` into each of the four containers.
4. Item 3: `get_portfolio()` probe; verify cash matches operator's
   funded amount ($2.2k).
5. Item 4: `get_positions()` probe.
6. **STOP and surface to operator** if items 1-4 don't all pass.
   Operator decides whether to proceed to step 7.
7. Item 5: paper-test order. Place + cancel within 5s. Strict
   `try/finally`.
8. Item 6: write up the CC report with all findings.
9. Commit + push.

## Operator-side after Phase 2 ships

1. Read the report.
2. If the report shows auth + order-placement working cleanly,
   Phase 3 (broker selector) becomes the next NEXT_TASK.
3. If the report surfaces auth gaps, fix them in a Phase 2.5
   brief BEFORE Phase 3.

## Rollback plan

The paper-test order should be canceled before this brief
returns. If for any reason an order remains open at the end of
the test (e.g., cancel call failed), the rollback is operator-side
manual cancel via Coinbase web UI. The brief surfaces the order ID
in the report for traceability.

## What CC should do if it's unsure

1. **If credentials aren't configured**, STOP and surface in the
   report's first section with the exact env var name(s) the
   operator needs to set. Do not attempt the auth probe without
   credentials.
2. **If the multi-process auth check shows divergence** (one
   container connected, another not), surface as a CRITICAL
   finding — this is the same class of bug as tonight's RH
   silent-empty incident.
3. **If `get_portfolio()` returns a cash value that doesn't match
   the operator's $2.2k**, surface for operator review BEFORE the
   paper-test. The mismatch could be:
   - Wallet pending-deposit not yet settled (operator should
     wait + retry tomorrow).
   - API returning a sub-account / wrong-currency view.
   - A `coinbase_service.get_portfolio` parsing bug.
4. **If the paper-test order cannot be canceled within 10s**, the
   test is a CRITICAL failure. Surface immediately. Operator must
   manually cancel via Coinbase UI.
5. **If the paper-test order accidentally fills** (extreme market
   conditions), surface as a CRITICAL failure. Document the
   resulting position; operator must manually close it.
