# CC_REPORT: f-position-identity-phase-5b-soak-and-reader-parity (day 1)

Date: 2026-05-22

## What shipped

- Commit (next): adds `scripts/d-phase5b-soak-probe.py` — the daily soak
  probe for Phase 5B. Read-only, idempotent. Connects to
  `localhost:5433/chili`, hits `trading_phase5a_envelope_parity`,
  `trading_phase5b_decision_envelope_position`, and
  `trading_phase5b_pattern_decision_performance`, plus a reader-parity
  comparison against legacy `trading_trades`-grouped PnL.
- Same commit: removes the throwaway `scripts/_phase5b_discover_columns.py`
  used to verify view schemas before writing the probe.
- Earlier commit (`fc073f2`): committed the two DD-breaker arming-watch
  reports for 2026-05-21 and 2026-05-22 that were sitting untracked.
  Watch is at n=27/30 close-days; projected arm 2026-05-25→26; flag
  stays OFF.

Files touched: 3 (probe added, discover-columns deleted, watch reports
committed). Migrations added: 0.

## Verification

Live probe run, 2026-05-23 05:13 UTC (operator's host clock):

```
=== Phase 5A envelope parity ===
  checked_at: 2026-05-23 05:13:34.818120
  trade_rows: 685
  trades_with_decision: 618
  trades_missing_decision: 67   (corrupt legacy dust, intentional)
  broker_trades_with_position: 509
  broker_trades_missing_position: 176
  open_broker_trades_missing_position: 3   <-- delta vs yesterday
  orphan_decisions: 0

=== Phase 5B linkage status distribution ===
  linked                                                          509 (green)
  historical_broker_envelope_missing_position                     106 (debt-ok)
  broker_envelope_missing_position                                  3 (HARD)

=== Pattern decision performance (top 4 by total_pnl) ===
  585  Intraday Squeeze + Declining Volume  87/87/87/0   +$521.11
  537  Falling Wedge Breakout + Trend Recl  23/23/12/1   +$81.28
  586  Intraday Squeeze + Declining Volume  30/30/30/0   +$57.96
 1052  rsi_bullish_divergence_reversal_bre  32/32/26/0   +$57.42

=== Reader parity: old trading_trades vs Phase 5B view ===
  patterns in old: 30   patterns in new: 30   mismatches: 3
   pid 1072: old 3 envelopes +$12.60 vs new 1 envelope -$7.21 (delta -$19.81)
   pid 1037: old 6 envelopes -$4.77  vs new 5 envelopes -$4.78  (delta -$0.00)
   pid 1052: old 39 envelopes +$57.42 vs new 32 envelopes +$57.42 (delta 0.00)
```

Migration verifier: not re-run; no migrations in this change.
Test suite: no automated tests added — the probe is a read-only operational
script, not a code-path that touches the brain.

## Surprises / deviations

1. **3 new hard linkage issues today (was 0 yesterday).** The Phase 5B
   ship CC_REPORT from 2026-05-22 said "0 hard live linkage issues" against
   506 linked. Today: 509 linked (+3) AND 3 hard
   `broker_envelope_missing_position`. The hard issues show up as 3
   `open_broker_trades_missing_position` rows in the parity counter, which
   was 0 yesterday. This is a real soak signal — the new envelope-insert
   path landed 3 open broker trades today without a position_id link.
   First soak finding. **NOT a release blocker** — Phase 5B is read-only —
   but it explains why the future-insert trigger or position-resolver isn't
   firing on these. Worth a brief follow-up.
2. **NEXT_TASK.md uses slightly stale field names.** The brief references
   `valid_trades_missing_decision`, but the live view's column is
   `trades_missing_decision` (no `valid_` prefix). The probe code adapts;
   NEXT_TASK can be edited later to match.
3. **Reader-parity mismatches on 3 patterns.** Pattern 1072 has a real
   PnL gap (-$19.81); patterns 1037 and 1052 have envelope-count drift
   only, with PnL matching to within $0.01. The likely cause is the 67
   corrupt-legacy-dust trades sitting in `trades_missing_decision`: those
   envelopes count in the old `trading_trades`-grouped query but not in
   the Phase 5B view that joins through decisions. Pattern 1072's
   non-zero delta means at least 2 of those dust rows had closed-trade
   PnL recorded even though they got skipped from the decision bridge.
4. **Pre-work cleanup needed.** Before I could ship the probe, four
   files were sitting in the working tree truncated mid-statement from
   an earlier parallel agent's run: `auto_trader_rules.py` (truncated
   at line 1544 mid-function-body), `tests/test_auto_trader_rules.py`
   (mid-decorator), `learning.py` (mid-log-statement), and
   `tests/test_evidence_canonical_writer.py` (mid-assert). The
   parallel agent's commit `49342c6` had the complete versions in
   HEAD; I restored all four from HEAD via `git show HEAD:... > ...`.
   Same widespread-truncation pattern as memory-noted
   `reference_2026_05_07_widespread_truncation.md` — operator may want
   to check whether the Edit tool truncation bug is more common than
   "files >2000 lines" suggests.

## Deferred

- **Production reporting-reader migration.** The probe's reader-parity
  section is the lightweight first step; an actual reporting reader
  (e.g., a brain summary that today queries `trading_trades`) still
  needs to be pointed at `management_envelopes.pattern_decision_performance`.
  Worth queueing as the next NEXT_TASK once the 3 hard linkage issues
  are investigated.
- **The 3 hard linkage issues themselves.** Need a follow-up to
  identify the 3 open Trade rows that came in today without a position
  link and either (a) backfill position_id retroactively or (b) fix the
  insert-time hook so it can't recur.
- **NEXT_TASK.md field-name nit.** Replace `valid_trades_missing_decision`
  with `trades_missing_decision` on its next edit pass.

## Open questions for Cowork

1. **Is the soak window 7 days like Phase 1, or shorter / longer?** The
   PROTOCOL says Phase 5 soak is "2 weeks" but Phase 5B is a sub-phase
   and the brief says "until the read model stays boring through multiple
   fresh entries and at least one close." Worth pinning a concrete date
   so the soak doesn't drift.
2. **Do we want to fix the 3 hard linkage issues mid-soak or wait?**
   Treating them as soak signal (good: confirms the probe works; bad:
   real positions are mis-linked). My read: fix mid-soak — a soak that
   tolerates new drift teaches the brain that the new write path is
   unreliable.
3. **NEXT_TASK is still marked PENDING.** The soak is multi-day so I'm
   leaving it PENDING; the daily probe is what advances the soak each
   day. Should the protocol have a `STATUS: IN_PROGRESS` state for
   multi-day tasks like this, or is leaving `PENDING` with daily reports
   the right pattern?
