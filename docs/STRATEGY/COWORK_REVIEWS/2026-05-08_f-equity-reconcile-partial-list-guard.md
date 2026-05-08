# COWORK_REVIEW: f-equity-reconcile-partial-list-guard

**Verdict:** Ship it. The three-layer reliability stack
(Phase A + B + C, all shipped today) closes the wipeout-cascade
loop end-to-end. Live verification confirms the migration,
constants, and clean-baseline streak distribution are all in
place. Next post-R32 phantom — if any — must survive 2 consecutive
broker_sync cycles AND the existing time window before the
stale-close path can fire.

## Algo-trader lens

Today's chain transformed a self-locking failure mode (operator's
account is PDT-blocked from synthesized closes) into three
independent defences with different failure-mode coverage:

* **Cardinality (Phase B's wipeout-burst breaker)**: ≥3 closes in
  any 5-second bucket trips the kill switch. Catches the
  2026-04-30 cascade pattern (9 trades at 00:56:01).
* **Persistence (Phase C's streak counter)**: a position must be
  missing for 2 consecutive cycles before close fires. Catches
  the JOB / PED single-row pattern (one cycle missing, then
  closed).
* **Symptom isolation (Phase A's PDT filter)**: even if a phantom
  row makes it through, it's excluded from the PDT count by
  `broker_order_id IS NOT NULL` + `last_fill_at IS NOT NULL` +
  `exit_reason NOT IN reconcile-set`. Account never self-locks
  from a synthesized close again.

Phase C's guard kicks in only after a position has been observed
present at least once. Fresh trades from the autotrader (where
`last_broker_sync IS NULL`) still go through the
`_RECONCILE_CONFIRM_WINDOW` time guard via `entry_date` fallback —
the same logic that prevented "close-within-30s-of-place" when
fractional fills were slow to reflect.

## Dev-architect lens

CC's three notable choices, in order of how much I care:

1. **Bulk-UPDATE gated on `if rh_tickers:`.** This is the call
   that matters most. A naive implementation would unconditionally
   increment every open trade's streak when the position list is
   missing. R32 already refuses to mass-close in that case — so
   nothing closes — but the streak counters would all advance.
   On the next non-empty cycle, two consecutive auth-flaps would
   have left every trade with streak ≥ 2; a single-ticker drop
   would then false-close. CC's gate is correct AND the surface
   reasoning ("R32 + Phase C should be compositional, not
   overlapping") is the right architectural framing. Surfaced
   prominently in the CC report's Surprises section.

2. **ORM column added to `Trade` model** even though the brief's
   "Brain integration" section didn't list it. CC made the call:
   the bulk UPDATE uses `Trade.broker_sync_missing_streak` as a
   column reference (cleaner than raw SQL), so the ORM needs to
   know about it. `server_default="0"` matches the migration's
   `DEFAULT 0` so schema and ORM agree at INSERT. Standard
   practice; documented as a deviation.

3. **Tests assert on Phase B's `[broker_sync] RECONCILE_CLOSE`
   warning, not on `closed=N` count.** This is subtle and right:
   the return-dict count doesn't distinguish "closed for the right
   reason." Phase B's structured warning IS the ground-truth
   signal that the close path executed for a `broker_reconcile_*`
   reason. Phase C tests building on Phase B observability is
   exactly what we wanted from the layered design.

## Live verification (operator-side, dispatched from sandbox)

```
broker_sync_missing_streak | integer | NOT NULL | DEFAULT 0  -- migration 233
_RECONCILE_PARTIAL_LIST_STREAK_MIN: 2
_RECONCILE_CONFIRM_WINDOW: 300        (existing time-window stays)
_WIPEOUT_BURST_THRESHOLD: 3           (Phase B unchanged)

streak distribution (all trades, 14d window):
  status=cancelled streak=0 count=14
  status=closed    streak=0 count=68
  status=open      streak=0 count=20
  status=rejected  streak=0 count=1

Phase A still working: pdt_count user_id=1 = 0
```

Clean baseline. Counter starts fresh at 0 for every existing row
(default value); future increments will only happen when a
broker_sync cycle observes a non-empty `rh_tickers` that's
missing one of the open trades.

## What surprised me

Nothing. CC followed the brief tightly; the two deviations
(bulk-UPDATE gate + ORM column) were the architecturally-correct
calls, surfaced in writing. No scope creep, no edits outside the
declared surface (`broker_service.py`, `migrations.py`,
`config.py`, `models/trading.py`, the new test file). Splice
discipline + AST verification per discipline.

## What's left

### 7-day soak

Re-run the Phase B audit query after 7 days. Target:
post-R32 phantom count = 0 in the trailing 7d. If any new
phantoms surface, audit:
- `broker_sync_missing_streak` value at exit_date — if 0 or 1, the
  guard didn't fire (bug); if 2+, the guard authorized the close
  (legitimate prolonged absence).
- `last_broker_sync` to `exit_date` gap — should be ≥ 10 minutes
  (2 cycles) for any close.
- `[broker_sync] RECONCILE_CLOSE` warning timing — should bracket
  the close.

### Three queued briefs remain parked

- **`f-pdt-crypto-bypass-cleanup`** — hygiene; explicit asset_kind
  + equity-tier policy. Ship anytime; not blocking anything.
- **`f-autotrader-pdt-aware-exit-deferral`** — premise was flawed
  (the 14 PDT-counted "round-trips" weren't real). Recommend
  rewriting the brief to address whatever the *actual* concern is
  for real autotrader same-day closes (which we now know are
  rare), or de-prioritizing entirely.
- **`f-pattern-demote-on-thin-evidence`** — kills the pattern 585
  alert noise (~157 alerts/24h, 25% WR, 4 trades, no OOS
  validation). Pure observability/learning-loop hygiene; not
  blocking trading but pollutes the signal pipeline.

### Suggested next direction

`f-pattern-demote-on-thin-evidence`. The reconciler chain is now
done; the pattern lifecycle gap is the next-largest source of
noise the operator has to filter through manually. Small scope
(one handler addition + 5 tests). After that, the
`f-pdt-crypto-bypass-cleanup` cleans up the asset-class boundary
in `pdt_guard.py` for hygiene.

## Final note

The `audit-first` protocol earned its keep this session.
Phase B explicitly forbade pre-writing a partial-list extension
without audit data; CC deferred; the operator-run audit returned
Case C with two specific phantom rows; Phase C fit those rows
exactly (per-position cycle counter, not 50%-drop or
snapshot-history-table). Pattern: when a brief's deliverable
depends on data that lives outside the sandbox, write the brief
to defer, surface the audit command, and let the operator's
return data choose the option. Saves at least one round of
"shipped the wrong shape" rework.
