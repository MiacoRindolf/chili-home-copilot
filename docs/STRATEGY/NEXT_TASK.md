# NEXT_TASK: f-coinbase-autotrader-enablement-phase-2-auth-verification

STATUS: PENDING

## Goal

Phase 2 of the Coinbase enablement initiative. **Audit-mostly,
with ONE paper-test order** (place a $5 BTC limit-buy at 50%
below current spot — won't fill — and immediately cancel) to
prove auth + order-placement works end-to-end. <1h CC scope.
LOW risk to existing system.

The full multi-phase brief is at
`docs/STRATEGY/QUEUED/f-coinbase-autotrader-enablement.md`.
This phase's brief is at
`docs/STRATEGY/QUEUED/f-coinbase-autotrader-enablement-phase-2-auth-verification.md`
— **read it first.**

## Why now

Phase 1 audit (commit `39e9807`) confirmed the architectural plan.
Phase 2 is the prerequisite check before Phase 3 (broker selector
+ entry routing) can ship. Without auth verification, Phase 3
would silently route entries that fail at the broker call — the
same class of failure mode as tonight's silent-broker-empty
incident.

## Operator-decided design constraints (locked, binding for Phases 3-7)

After reading Phase 1's audit, the operator made the following
decisions. These are NOT in scope to implement in Phase 2 — they
are documented here so Phase 3+ briefs cite them as binding
constraints:

1. **Cross-venue position cap: SEPARATE per-venue caps.** No
   cross-venue position aggregation. Each venue has independent
   caps.
2. **Kill switch: GLOBAL.** One operator-pulled lever stops both
   venues.
3. **Selector preference for tickers in BOTH whitelists:
   RH-first** (cost-cheaper). Coinbase routes only for the long
   tail (tickers RH doesn't list).
4. **Fast-path overlap: skip-on-fast-path-active.** Autotrader
   skips Coinbase routing if fast-path is currently active for
   that ticker.

## Why this scope (Phase 2 only, audit-with-one-paper-test)

* **Vs. Phase 3 (broker selector) directly**: Phase 3 needs to
  know auth works. If we ship Phase 3 first and auth is broken,
  every Coinbase entry fails silently — exactly tonight's failure
  pattern.
* **Vs. read-only audit only (no paper-test)**: a read-only audit
  can verify `is_connected()` returns True but can't prove
  `place_buy_order` actually round-trips with the API correctly.
  A $5-far-below-market-immediately-canceled order is the cheapest
  way to prove the full chain.
* **Vs. multi-paper-test or paper-soak**: that's Phase 6's
  responsibility. Phase 2 is single-test prove-it-works.

## The change

Per the brief's 6 verification items:

1. Credentials configured (`CHILI_COINBASE_API_KEY` /
   `CHILI_COINBASE_API_SECRET` or equivalent in `.env`).
2. `is_connected()` returns True in chili, autotrader-worker,
   scheduler-worker, broker-sync-worker. **Multi-process auth
   liveness verified up front** (lesson from tonight's RH
   silent-empty incident).
3. `get_portfolio()` returns the funded $2.2k cash.
4. `get_positions()` returns Coinbase holdings (initially
   expected empty).
5. **Paper-test**: $5 BTC-USD limit-buy at 50% below spot,
   immediately cancel. `try/finally` guarantees zero residual
   orders. Hard 10s timeout on the cancel; if exceeded, CRITICAL
   surface.
6. CC report at
   `docs/STRATEGY/CC_REPORTS/2026-05-09_f-coinbase-autotrader-enablement-phase-2-auth-verification.md`.

## Acceptance criteria

See full brief for the 8-item list. Summary:

1. Items 1-4 (audit-only) pass before item 5 (paper-test) is
   attempted.
2. Multi-process auth check across all 4 worker processes.
3. Paper-test placement + cancellation within 5s (10s hard
   timeout); zero residual orders confirmed via
   `list_open_orders`.
4. Operator's $2.2k unchanged post-test (modulo trivial fees if
   accidental fill — which shouldn't happen but document if it
   does).
5. CC report covers: pass/fail per item, response shapes for
   `get_portfolio()` + `get_positions()`, full paper-test order
   payload, gotchas surfaced for Phase 3.
6. NO code changes to `coinbase_service.py` or
   `coinbase_spot.py`. If a bug surfaces, surface in the report;
   fix is Phase 2.5 or Phase 3.

## Brain integration (read-only except for the paper-test)

- `app/services/coinbase_service.py` — read; call public surface.
- `app/services/trading/venue/coinbase_spot.py` — read; optional
  parallel adapter check.
- `.env` — read variable presence (not values).

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Operator's directive: don't break what works.** RH path
  untouched.
- **Paper-test order ONLY.** $5 notional, far-below-market limit,
  immediately canceled. NO market orders, NO live entry.
- **Hard 10s timeout on cancel.** If exceeded, CRITICAL surface +
  stop.
- **`try/finally`** around order placement; cancel runs
  unconditionally on any exception.
- **DO NOT auto-promote Phase 3.** Operator approves Phase 3
  after reading Phase 2 report.
- **DO NOT touch the operator-decided design constraints.** They
  are documented here as binding for Phase 3+; Phase 2 doesn't
  implement them.
- **Edit-tool truncation discipline (HARD).**
- **No magic numbers** — paper-test prices/notional come from
  settings or documented constants.

## Out of scope (Phase 2 — covered by later phases)

- Broker selector logic (Phase 3).
- Bracket writer Coinbase paths (Phase 4).
- Cost-aware sizing (Phase 5).
- Paper-trade soak (Phase 6 — different from Phase 2's single
  paper-test).
- Live verification (Phase 7).
- Any code changes to `coinbase_service.py` or the adapter.
- Any RH path changes.

## Sequencing

1. Truncation scan on `coinbase_service.py` +
   `coinbase_spot.py`.
2. Item 1: credentials probe.
3. Item 2: multi-process `is_connected()` probe via
   `docker exec` into each container.
4. Item 3: `get_portfolio()` probe; verify cash = $2.2k.
5. Item 4: `get_positions()` probe.
6. **STOP and surface** if items 1-4 don't all pass. Operator
   decides whether to proceed to item 5.
7. Item 5: paper-test order. `try/finally`. Cancel within 5s.
8. Item 6: CC report.
9. Commit + push.

## Operator-side after Phase 2 ships

1. Read the report.
2. If auth + order-placement work cleanly, Phase 3 (broker
   selector) becomes next NEXT_TASK.
3. If auth gaps surface, Phase 2.5 fixes them BEFORE Phase 3.

## Rollback plan

Paper-test order canceled before this brief returns. If cancel
fails, rollback = operator-side manual cancel via Coinbase web UI;
report surfaces the order ID for traceability.

## What CC should do if it's unsure

1. **Credentials missing**: STOP, surface env var names. No auth
   probe without credentials.
2. **Multi-process auth divergence**: CRITICAL surface — same
   class as tonight's RH silent-empty bug.
3. **`get_portfolio()` cash mismatch with $2.2k**: STOP and
   surface for operator. Could be pending-deposit not settled,
   sub-account / wrong-currency view, or parsing bug.
4. **Cancel timeout**: CRITICAL. Operator manual cleanup
   required.
5. **Paper-test accidentally fills**: CRITICAL. Document
   resulting position; operator manual close required.
