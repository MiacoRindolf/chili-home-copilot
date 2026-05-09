# f-phase-e-backlog-cleanup-fixes

STATUS: QUEUED
SLUG: phase-e-backlog-cleanup-fixes
PROPOSED: 2026-05-08
SEVERITY: low (Phase E first-sweep cleanup of accumulated backlog tripped the wipeout-burst breaker; 1 minor consequence + 1 nit surfaced)

## TL;DR

Phase E (`f-crypto-stale-trade-closer`, commit `c8aec21`) shipped
today and on its first run cancelled 14 phantom-open crypto trades
(the accumulated backlog of `entry_never_filled` rows from before
Phase E was protecting them). Two small surfacing items:

1. **Burst-breaker tripped on the cleanup**, not on a real wipeout.
   Phase B's `_record_reconcile_close_burst` correctly fired (3
   closes within 5s), persisted breaker_tripped=true, then operator
   manually reset via `scripts/d-breaker-reset.ps1`. Steady-state
   (1-2 phantoms per week) won't trip; only first-run backlogs.
2. **`trading_bracket_intents` rows not updated** when the parent
   trade is cancelled by the sweep. The bracket reconciler will
   keep emitting `missing_stop:warn` for the now-cancelled trades
   until something flips `intent_state` to `abandoned`.

This brief proposes the small tweaks to make Phase E's first-run
behaviour cleaner without weakening its safety properties.

## Why now

Both items surfaced on Phase E's first live run (2026-05-08
17:30 PDT). Output from `run_crypto_stale_trade_close(db)`:
```
result: {'layer1_cancelled': 14, 'layer2_closed': 0, 'layer2_streak_incremented': 0, 'layer2_streak_reset': 0,
         'trade_ids': [1826, 1810, 1807, 1809, 1827, 1808, 1828, 1824, 1835, 1823, 1832, 1831, 1837, 1836]}
```

Burst log:
```
[broker_sync] WIPEOUT BURST DETECTED -- 3 reconcile-closes in <=5s (latest: ticker=HBAR-USD trade_id=1807);
              TRIPPING DRAWDOWN BREAKER with reason='wipeout_burst_3_in_5s'
```

Bracket reconciler still emits, post-sweep:
```
trade_id=1810 (now cancelled) intent_state='intent' last_diff_reason='missing_stop:warn'
```

## Goal

### Item 1: Backlog-aware burst-breaker exemption

`_record_reconcile_close_burst` should distinguish between:
- A wipeout cascade (real broker failure → many simultaneous
  closes) — DO trip the breaker.
- A backlog cleanup (operator's first deploy of a new sweep that
  catches accumulated phantoms) — DO NOT trip the breaker.

The discriminator is: how OLD are the trades being closed in the
burst? Real wipeouts close TODAY's positions. Backlog cleanups
close trades that have been open for hours/days.

Proposed: add a parameter to `_record_reconcile_close_burst(ticker,
trade_id, *, _now=None, _breaker_persister=None,
trade_age_seconds=None)`. If all trades in the bucket are older
than `CHILI_BURST_BREAKER_MIN_TRADE_AGE_SECONDS` (default 3600 = 1h),
log the burst as INFO (not WARN), do NOT call `_persist_breaker_state(True)`.

Why 1h: a real wipeout closes positions opened today (within
minutes-to-hours). A backlog of `entry_never_filled` rows is by
definition >2h old (Layer 1's window). The 1h threshold gives
30-60min headroom for legitimate same-cycle multi-close cascades.

Phase E's call sites pass `trade_age_seconds = (now - trade.entry_date).total_seconds()`.
Callers that don't know the age (existing equity reconcile path)
omit the kwarg and the default behaviour (always trip on burst)
is unchanged.

### Item 2: bracket_intents update on cancel/close

When the sweep cancels a trade in Layer 1 OR Layer 2, also UPDATE
`trading_bracket_intents` for that trade_id:
- `intent_state = 'abandoned'` (Layer 1) or `'resolved'` (Layer 2)
- `last_diff_reason = 'trade_cancelled_entry_never_filled'` or
  `'broker_position_reconciled_to_zero'`
- `updated_at = NOW()`

This stops the bracket reconciler from continuing to emit
`missing_stop:warn` for the now-closed trade.

### Item 3: PHASE_A_RECONCILE_ARTIFACT_EXIT_REASONS namespacing

Phase E added two reasons to `pdt_guard._RECONCILE_ARTIFACT_EXIT_REASONS`.
The frozenset is starting to accumulate; for grep-ability and so
future phases (F, G, ...) don't have to touch the constant
directly, lift to a registry pattern: `_RECONCILE_ARTIFACT_EXIT_REASONS`
becomes a module-level frozenset that's the union of named
sub-sets (`_EQUITY_RECONCILE_ARTIFACT_REASONS`,
`_CRYPTO_RECONCILE_ARTIFACT_REASONS`). Pure refactor, no behaviour
change.

## Acceptance criteria

1. Item 1: `_record_reconcile_close_burst` accepts an optional
   `trade_age_seconds` kwarg. When all trades in a bucket exceed
   the threshold, the breaker is NOT tripped. Tests pin both
   paths.
2. Item 2: Phase E's sweep updates the matching bracket_intents
   row when a trade is cancelled/closed. Test verifies the intent
   flips to `abandoned`/`resolved` and the reconciler's next
   sweep emits no further `missing_stop:warn` for that trade.
3. Item 3: refactor only. Existing tests pass (especially Phase
   A's frozenset-shape test). `_RECONCILE_ARTIFACT_EXIT_REASONS`
   exposes the same set; the union is computed at module load.
4. Live verification: re-run `run_crypto_stale_trade_close(db)` in
   chili_test with a seeded backlog of 5+ phantoms; the burst
   breaker does NOT trip.
5. Existing 9 Phase E tests still pass.
6. CC report at
   `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-phase-e-backlog-cleanup-fixes.md`.

## Brain integration (reuse, don't rewrite)

- `app/services/broker_service.py:_record_reconcile_close_burst`
  (Phase B). Add the optional kwarg; preserve default behaviour.
- `app/services/trading/bracket_reconciliation_service.py:run_crypto_stale_trade_close`
  (Phase E). Pass `trade_age_seconds` and update bracket_intents
  in the same transaction.
- `app/services/trading/pdt_guard.py:_RECONCILE_ARTIFACT_EXIT_REASONS`.
  Refactor to a union of named subsets.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged. The
  burst-breaker still trips on REAL wipeouts (today's positions
  closed in <5s).
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Don't touch the layer-1 / layer-2 thresholds.** They're
  correct; only the burst-tripping behaviour gets the age guard.
- **Don't touch Phase A's SQL filter.** Item 3 is internal
  refactor only.
- **No magic numbers**: the 1h threshold lifts to settings.

## Out of scope

- New stale-detection criteria.
- Equity-side burst-breaker behaviour (already correct: equity
  reconciler only fires on today's positions).
- Manual breaker reset operator workflow (already documented in
  scripts/d-breaker-reset.ps1).
- The pattern-demote sweep wiring fix
  (`f-pattern-demote-sweep-wiring-fix`) — separate brief.

## Sequencing

1. Truncation scan.
2. Item 1: add the kwarg + threshold setting + tests.
3. Item 2: bracket_intents UPDATE + tests.
4. Item 3: refactor frozenset to union of subsets + verify
   imports don't break.
5. Live re-run in chili_test with seeded backlog; verify burst
   does not trip.
6. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker scheduler-worker`.
3. Verify the breaker stays clean on the next sweep cycle.
4. Optional: re-run the audit query to confirm zero phantom-opens
   accumulated since the last sweep.

## Rollback plan

`git revert` the commit. Item 1 default behaviour matches
pre-change (always trip on burst). Item 2 update is gated on a
new try/except so a failure doesn't poison the sweep. Item 3 is
pure refactor.

## Open questions

1. **Should the age threshold be configurable per-burst-event?**
   Probably not — global setting is simpler. Surface only if
   operator hits a case where they want different thresholds for
   different reconcilers.
2. **Should bracket_intents have its own status-change log?**
   Out of scope here; if the operator wants forensics on intent
   state transitions, queue a separate observability brief.
