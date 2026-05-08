# NEXT_TASK: f-equity-broker-reconcile-wipeout-protection

STATUS: DONE

## Goal

Close the structural gap behind 2026-04-30's equity-book wipeout cascade.
R32 (commit `539e1c2`, 2026-04-30) added an empty-`rh_tickers` guard so
the reconciler refuses to mass-close when `get_positions()` returns 0
positions while local trades remain open. This brief verifies — through
data, not assumption — that R32 actually prevents NEW phantom rows from
forming under all wipeout-class failure modes, then ships always-on
observability + breaker burst-trip + the regression test that pins R32's
behaviour.

The full brief is at
`docs/STRATEGY/QUEUED/f-equity-broker-reconcile-wipeout-protection.md`
— read it first.

## Why now

Phase A (`f-pdt-count-broker-confirmed-only`, commit `60c26f8`) shipped
today and verified live: PDT count dropped from 14 to 2 immediately on
restart. That's the symptom fix — operator's stock entries are unblocked
right now.

Phase A excludes the 14 phantom rows from the PDT count but does NOT
prevent new phantoms from accreting. The next wipeout-class event would
re-create the same self-lock. Phase B is the cause fix.

## Audit-first protocol

The brief explicitly forbids pre-writing a partial-list extension. The
audit decides scope:

* **Case A** (zero post-R32 phantoms in equity book) → R32 is
  structurally complete. Brief becomes always-on observability only.
* **Case B** (post-R32 phantoms in empty-list mode) → R32 is bypassed
  somehow; find why and fix.
* **Case C** (post-R32 phantoms in partial-list mode) → R32 needs the
  partial-list extension; ship the option (A/B/C in the brief) the
  audit data supports.
* **Case D** (post-R32 phantoms via a different code path) → identify
  and patch.

CC's first deliverable is the audit categorization. Code follows from
that.

## Always-on deliverables (regardless of audit findings)

These ship in every case:

1. **Reconcile-close observability.** Today
   `broker_service.py:2202+` (the stale-close `for trade in stale:`
   loop) closes trades with only a `logger.debug`. Add a structured
   `[broker_sync] RECONCILE_CLOSE: ticker=X reason=Y` warning per
   close + a counter the operator can grep. Operator-facing.
2. **Bucket-coalesced wipeout-burst breaker trip.** When ≥3 rows
   close in the same 5-second bucket via
   `broker_reconcile_position_gone`, call `breaker.trip()` with
   reason `wipeout_burst_3_in_5s`. R31 already excludes synthetic
   reconcile losses from the consecutive-loss rule, so this trip
   fires on the row-burst pattern (a wipeout signature), not on
   aggregate PnL.
3. **Module-docstring update.** Extend the existing R32 docstring at
   `broker_service.py:2109+` with the Phase B linkage.
4. **R32 regression test.** Pin the empty-`rh_tickers` guard's
   return shape (`skipped_reason='empty_broker_positions_with_open_local_trades'`)
   and assert open trades stay open.

## Brain integration (reuse, don't rewrite)

- `app/services/broker_service.py:2109-2150` — R32 empty-list guard.
  EXTEND or DOCUMENT; don't rewrite.
- `app/services/broker_service.py:2171-2280` — stale-close path. ADD
  observability; don't change close logic.
- `app/services/trading/portfolio_risk.py:1016` — already excludes
  `broker_reconcile_position_gone` from R31's consecutive-loss rule.
  Don't touch.
- The breaker's existing `trip()` method (kill-switch infrastructure)
  is reused for the wipeout-burst signal.

## Acceptance criteria

1. **Audit deliverable**: CC report's "Audit" section lists every
   `broker_reconcile_position_gone` row in the equity book in the
   last 30 days, classified as pre-R32 (before
   `2026-05-01T04:08:57Z`) or post-R32. For each post-R32 row:
   `exit_date`, `last_broker_sync`, the size of `rh_tickers` at
   close-time (from logs if available), and which case (A/B/C/D).
2. **Always-on deliverables shipped** (observability + breaker
   burst-trip + R32 regression test + module docstring) regardless
   of audit findings.
3. **If audit Case C**: one of options A/B/C from the brief shipped
   with full test coverage; the chosen option is the one the data
   supports, NOT a default pick.
4. **If audit Case A**: brief closes at observability; no new code on
   the reconcile path.
5. CC report at
   `docs/STRATEGY/CC_REPORTS/2026-05-08_f-equity-broker-reconcile-wipeout-protection.md`.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Hard Rule 3**: data-first. If the audit surfaces Case D, fix
  the data path; don't paper over with a router/service filter.
- **Edit-tool truncation discipline (HARD).** `broker_service.py`
  is large (>2300 lines). Splice pattern only. `wc -l + ast.parse`
  post-edit verification mandatory. See memory
  `reference_2026_05_07_widespread_truncation.md`.
- **Tests use `_test`-suffixed DB.**
- **Audit-first.** Do NOT pre-write the partial-list extension. The
  audit data decides.
- **Don't touch `pdt_guard.py`** (Phase A's filter is the durable
  defence even after Phase B ships).

## Out of scope

- Crypto reconciler (separate brief if needed).
- Options reconciler (already MMM-filtered out of the stale-close
  path).
- The PDT count itself.
- Backfilling old phantom rows.
- Pattern-quality demotion / autotrader exit deferral / crypto
  bypass cleanup (separate briefs already queued).

## Sequencing

1. Truncation scan on `app/services/broker_service.py`.
2. Audit query: pull `broker_reconcile_position_gone` rows for the
   equity book grouped by 5-second buckets; cross-reference with
   2026-05-01T04:08:57Z (R32 deploy).
3. Categorize as Case A/B/C/D in the CC report.
4. Always-on deliverables (observability + burst-trip + regression
   test + docstring).
5. Conditional partial-list extension only if Case C surfaces.
6. Tests for everything shipped.
7. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate chili broker-sync-worker
   autotrader-worker`.
3. Verify `[broker_sync] RECONCILE_CLOSE` warnings appear (or
   correctly don't) in normal operation.
4. Wait 24h; confirm zero new phantom rows accumulate.

## Rollback plan

`git revert` the commit. Always-on deliverables are additive
(metrics + warnings + tests + docstring). Partial-list extensions
(if shipped) are gated by a settings flag
(`CHILI_RECONCILE_PARTIAL_LIST_GUARD_ENABLED`) defaulting to ON; flip
OFF to revert just the new guard while keeping the observability
changes.

## What CC should do if it's unsure

1. If the audit data is ambiguous (e.g., no logs available from the
   relevant window), ship the always-on deliverables and the
   regression test, write the audit limitations into the CC report,
   and skip the conditional extension. Phase B can be re-opened
   later with better instrumentation.
2. If the audit surfaces a third code path that emits
   `broker_reconcile_position_gone` outside `broker_service.py`,
   stop and surface — the brief expects the writer to be at
   `broker_service.py:2247` only.
