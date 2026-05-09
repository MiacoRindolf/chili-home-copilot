# NEXT_TASK: f-coinbase-autotrader-enablement-phase-3-broker-selector

STATUS: PENDING

## Goal

Phase 3 of the Coinbase enablement initiative. Build the **broker
selector** that routes autotrader entries to RH or Coinbase based
on ticker + operator-locked design constraints. RH path stays
byte-identical; Coinbase path is gated behind a LIVE flag that
defaults OFF.

The full brief is at
`docs/STRATEGY/QUEUED/f-coinbase-autotrader-enablement-phase-3-broker-selector.md`
— **read it first.** ~2-3h CC scope. MEDIUM risk (touches
autotrader entry-routing).

## Why now

Phase 2 verified end-to-end (commit `6cce057`):
- Auth across all 4 worker containers ✅
- Portfolio + positions queryable ✅
- Paper-test redux: order placed (0.45s), cancelled (0.14s), zero
  residual, cash unchanged ($2200.01 → $2200.01) ✅
- `-USD` ticker convention works (operator converted USDC → USD)

Phase 3 is the gate to actually trading on Coinbase. Without it,
the $2.2k sits idle.

## Operator-locked design constraints (binding from Phase 1)

1. **Cross-venue position cap: SEPARATE per-venue caps** (no
   aggregation).
2. **Kill switch: GLOBAL** — one operator lever stops both
   venues (`CHILI_AUTOTRADER_KILL_SWITCH=1`).
3. **Selector preference for both-listed tickers: RH-first**
   (cost-cheaper; RH is fee-free, Coinbase is 60bps taker).
   Coinbase routes only the long tail (tickers RH doesn't list).
4. **Fast-path overlap: skip-on-fast-path-active**. Autotrader
   skips Coinbase routing if fast-path holds the ticker.

## Quote-currency convention (from Phase 2)

Coinbase tickers route as `-USD` pairs. Matches CHILI's existing
`coinbase_service.py` calls. If operator funds future deposits as
USDC, they must convert to USD in the Coinbase UI before
autotrader BUYs will succeed (Phase 2 G1 — operator runbook
responsibility).

## The change (3 components)

1. **`broker_selector.py`** — pure function that returns
   `{venue: 'rh'|'coinbase'|'skip', reason: str}` with a 5-branch
   decision tree (kill switch, fast-path overlap, RH whitelist,
   Coinbase whitelist, no match).
2. **Whitelist resolvers** — `resolve_rh_whitelist` and
   `resolve_coinbase_whitelist` (latter filters out the 31 dust
   positions and uses Coinbase product-list filtered to active
   USD-quoted spot products).
3. **`auto_trader.py` splice** — replace the existing direct
   broker call with a `select_venue` call; route by `decision.venue`.
   RH path BYTE-IDENTICAL. Coinbase path gated on
   `CHILI_COINBASE_AUTOTRADER_LIVE=1` (default OFF; OFF = shadow
   log only).

## Acceptance criteria (8-item list)

See full brief. Headline:

1. RH path BYTE-IDENTICAL post-Phase-3.
2. Selector returns correct venue for 5 ticker classes (RH-only,
   Coinbase-only, both-listed, fast-path-active, kill-switch-on).
3. `LIVE=0` (default) → Coinbase routes shadow-log only; no broker
   call.
4. `LIVE=1` + tiny limit-far-below-spot → places + cancels via
   existing autotrader bracket cancellation. Operator approval
   required for this step.
5. Multi-process kill-switch pickup verified across all 4
   workers.
6. Cost log preserved (writes to existing `trading_venue_truth_log`
   or new `trading_venue_routing_log`).
7. New tests in `tests/test_broker_selector.py` cover all 5
   decision branches + LIVE-flag gate.
8. CC report at
   `docs/STRATEGY/CC_REPORTS/<YYYY-MM-DD>_f-coinbase-autotrader-enablement-phase-3-broker-selector.md`.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged. Kill
  switch + drawdown breaker + ensemble promotion check PRECEDE
  selector in entry path.
- **RH path BYTE-IDENTICAL**. Verified by parity unit test that
  captures RH call args before+after.
- **No paper-soak in Phase 3** (Phase 6's job).
- **No cost-aware sizing in Phase 3** (Phase 5's job).
- **No bracket writer Coinbase paths in Phase 3** (Phase 4's
  job). Document any exit-path gaps in CC report.
- **No autotrader scope expansion**. Phase 3 only adds routing
  decision; it does NOT relax any existing entry-eligibility
  rules.
- **Edit-tool truncation discipline (HARD).** `auto_trader.py` is
  >2000 lines. After every edit: `wc -l` + `git diff --stat` and
  confirm no silent truncation.

## Out of scope (Phase 3 — later phases)

- Bracket writer Coinbase paths (Phase 4).
- Cost-aware sizing (Phase 5).
- Paper-trade soak (Phase 6).
- Live verification + capital ramp (Phase 7).
- Coinbase WebSocket order updates.
- USDC-quoted (`-USDC`) tickers.

## Sequencing

1. Truncation scan on `auto_trader.py` + `coinbase_service.py`.
2. Read `auto_trader.py` to find entry-placement callsite +
   capture RH-path call signature for parity test.
3. Write `broker_selector.py`.
4. Write `tests/test_broker_selector.py`.
5. Splice into `auto_trader.py` (RH unchanged; Coinbase gated).
6. Add 2 env vars to `app/config.py`.
7. Run pytest.
8. Force-recreate workers; verify multi-process kill-switch
   pickup.
9. Single live test (`LIVE=1` + tiny limit-far-below-spot +
   cancel). **Operator approval required for this step.**
10. CC report.
11. Commit + push.

## Rollback plan

- Selector misbehaves → `CHILI_AUTOTRADER_KILL_SWITCH=1`
  (30-second mitigation; blocks both venues).
- Coinbase routing unsafe → `CHILI_COINBASE_AUTOTRADER_LIVE=0`
  (30-second mitigation; RH unaffected).
- Selector wrong venue → git revert the `auto_trader.py`
  splice (selector module + tests stay).

## What CC should do if unsure

See full brief §"What CC should do if it's unsure". Key one:

> **Test parity violation** (RH path call args change for any
> reason): STOP. Surface for operator. RH path is byte-identical
> or nothing ships.
