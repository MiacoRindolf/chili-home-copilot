# COWORK_REVIEW: f-equity-broker-reconcile-wipeout-protection

**Verdict:** Ship it. Always-on deliverables landed cleanly; CC was
correct to defer the conditional partial-list extension to the
audit-first protocol. Audit is now run; verdict is **Case C**, so
Phase C brief (`f-equity-reconcile-partial-list-guard`) follows.

## Algo-trader lens

The Phase B always-on layer raises the cost of a wipeout-class event
in two ways: (1) the `[broker_sync] RECONCILE_CLOSE` warning per
close gives the operator immediate visibility instead of `logger.debug`
silence, (2) the wipeout-burst breaker trips on cardinality
(≥3-in-5s), so a multi-ticker cascade like 2026-04-30's 9-trades-at-
00:56:01 stops the autotrader cold rather than letting it accrete
into a 14-row PDT self-lock. The breaker complements R31 (which
excludes synthetic reconcile losses from PnL-based consecutive-loss
trips) — the row-burst trigger is exactly the failure class R31's
PnL filter was meant to ignore.

The 33-row total in the last 30d is operationally sobering: even
with R32 in place since 2026-05-01, the equity book is producing
synthetic closes at ~2 per week (the two post-R32 rows). For now
each one is a genuinely-missing position from a transient broker
hiccup, not a wipeout — but each one falsely-counts toward PDT
until Phase A's filter excludes them. Phase C closes the source.

## Dev-architect lens

CC's three notable choices, in order of how much I care:

1. **Audit-first discipline held under pressure.** The brief gave
   CC a clear "if unsure" off-ramp; CC took it for the conditional
   extension and shipped exactly the always-on deliverables — no
   over-engineering, no preemptive picks. This is the right
   posture for live-trading reconciler code.

2. **Testability seams over module-level import lift.** The choice
   to add `_now` and `_breaker_persister` leading-underscore
   kwargs (instead of lifting `_persist_breaker_state` to module
   level) avoids future import-cycle risk. Production callers stay
   unchanged; tests inject a fake clock + fake persister. The
   `AttributeError: module 'broker_service' does not have the
   attribute '_persist_breaker_state'` failure was real, the fix
   was correct, and the resulting code is easier to test
   permanently. This is a cleaner version of the lesson from
   yesterday's `f-fastpath-rotator-http-retry` `_time.sleep`
   episode.

3. **FK-friendly user_id=NULL seed in the R32 test.** A small
   correctness catch — `trading_trades.user_id` has an FK to
   `users.id`. Setting `user_id=42` in a fresh test DB hit
   `IntegrityError`; setting `user_id=NULL` works because R32's
   guard filter `Trade.user_id == user_id` uses `IS NULL`
   semantics in SQLAlchemy. Not the brief's problem to surface
   but worth noting for future test authors.

## Audit findings (operator-run)

| Window | Count | Cluster |
|---|---|---|
| Pre-R32 (before 2026-05-01T04:08:57Z) | 31 | The 2026-04-29 / 2026-04-30 cascade — well-known |
| **Post-R32** | **2** | One per week; both single-row, both with `last_broker_sync` exactly 6 minutes before `exit_date` |

Post-R32 detail:
- `id=1819 ticker=JOB exit=2026-05-06T13:46:03 last_sync=2026-05-06T13:40:02`
- `id=1820 ticker=PED exit=2026-05-08T12:58:03 last_sync=2026-05-08T12:52:02`

Both have `broker_order_id=NULL`, `last_fill_at=NULL` — synthesized
closes, not real broker fills. Both fired one broker_sync cycle
after `last_broker_sync` (5min interval + slop). Both are isolated
single-row events (wipeout-burst breaker wouldn't fire — threshold
is 3-in-5s).

**This is Case C** (partial-list mode). The position was returned
in cycle N, missing in cycle N+1, then `_RECONCILE_CONFIRM_WINDOW`
expired and the stale-close fired. R32 catches empty-list only;
the missing-from-otherwise-non-empty-list case is the gap.

## What's left

### Immediate

- **Operator-side restart already happened** (`docker compose up
  -d --force-recreate chili broker-sync-worker autotrader-worker`).
  Phase B deliverables verified loaded.
- **PDT count for user_id=1 is now 0** (the 2 real Apr-29
  day-trades aged out of the 5-day window). Account is cleanly
  unblocked on the PDT axis. The next stock
  `pattern_breakout_imminent` alert with sufficient pattern
  quality should attempt placement.

### Phase C queued

Write `f-equity-reconcile-partial-list-guard` (option B from the
parent brief: per-position consecutive-cycle confirmation
counter). Specifics:

- New column `trading_trades.broker_sync_missing_streak INT NOT NULL DEFAULT 0`.
- On each broker_sync cycle: increment for every open trade whose
  ticker is NOT in `rh_tickers`, reset to 0 for every open trade
  whose ticker IS in `rh_tickers`.
- The stale-close path checks `streak >= N` (default N=2) before
  closing. JOB and PED would have shown streak=1 at the cycle
  they got closed; the close would have been deferred to streak=2
  (which would coincide with the position being genuinely gone
  for two consecutive 5-min cycles, i.e., 10+ minutes of broker
  truth).
- Settings flag `CHILI_RECONCILE_PARTIAL_LIST_STREAK_MIN` defaulting
  to 2, so the operator can tune.
- Migration NNN (next free) for the new column.
- Tests pinning the streak increment/reset/close-at-N behaviour.

### Side findings worth tracking

- `_RECONCILE_CONFIRM_WINDOW` is the existing global time-based
  guard. With Phase C's per-position cycle-streak guard, the time
  window becomes redundant for the partial-list case. Surface in
  Phase C brief: should the time window be retained for the
  fresh-trade case (autotrader places, RH hasn't reflected yet) or
  unified into the streak counter? Probably retain time-window for
  fresh trades; cycle-streak only applies after the first
  successful sighting of the position.

### Three other queued briefs stay parked

- `f-pdt-crypto-bypass-cleanup` — hygiene
- `f-autotrader-pdt-aware-exit-deferral` — based on flawed premise (real autotrader same-day round-trips don't actually happen), de-prioritize or rewrite scope
- `f-pattern-demote-on-thin-evidence` — kills the pattern 585 alert noise

Operator picks next direction after Phase C ships.

## Final note

CC's "What CC should do if it's unsure" §1 worked as designed: the
brief told CC to defer the partial-list extension if the audit
data couldn't be obtained, and CC did exactly that. This is the
audit-first protocol paying off — no preemptive code, no
over-engineering, just always-on observability + an explicit
operator-handoff for the data step. Phase C now has the data it
needs to be the right fit for the failure mode, not a guess.
