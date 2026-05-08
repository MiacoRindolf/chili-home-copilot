# CC_REPORT: f-pattern-demote-on-thin-evidence

## Outcome

Pattern lifecycle now demotes promoted patterns matching all 5
thin-evidence criteria from the brief. Sweep runs every
`execution_feedback_digest` cycle (same trigger as the existing
`run_live_pattern_depromotion` — they're complementary, not
overlapping). Pattern 585's audit fingerprint (4 trades / 25% WR /
no OOS / `provisional_small_paths`) is exactly matched by the
predicate; 1011/1016 (large samples) are unaffected; 1047 (already
`challenged`) is skipped by the lifecycle-stage filter.

## Per-step status

### Step 1 — Survey + truncation scan — COMPLETE
* `learning.py`: 10102 lines, AST clean. Existing
  `run_live_pattern_depromotion` at line 662 maps live-vs-OOS gap
  on accumulated samples (different concern from this brief's
  thin-evidence-from-promotion-time).
* `brain_work/dispatcher.py`: identified
  `_handle_execution_feedback_digest` as the per-cycle hook;
  trade-close fanout in `run_brain_work_dispatch_round` invokes
  the existing `handlers/demote.py:handle_trade_closed` (EV-gate
  driven) — distinct from this brief's criteria-driven sweep.
* `ScanPattern` ORM has the read columns (`lifecycle_stage`,
  `trade_count`, `win_rate`, `oos_win_rate`,
  `promotion_gate_reasons`); the write columns `demoted_at` +
  `promotion_demote_reason` exist in the schema (mig 12559+) but
  are NOT on the ORM class. Use raw SQL UPDATE for those.

### Step 2 — Implement `run_thin_evidence_demote` — SHIPPED
`learning.py` splice (+~155 lines, AST clean):

* Module-level constants (per "no magic numbers"):
  * `THIN_EVIDENCE_MIN_TRADES = 10`
  * `THIN_EVIDENCE_WIN_RATE_FLOOR = 0.33`
  * `THIN_EVIDENCE_PROVISIONAL_GATE_REASON = "provisional_small_paths"`
  * `THIN_EVIDENCE_DEMOTE_REASON = "thin_evidence_low_realized_wr"`
* `_matches_thin_evidence_criteria(p)` — pure predicate accepting
  any object with the right attributes (testable without DB; takes
  `SimpleNamespace` in unit tests). Handles JSONB list AND JSON-
  string-encoded promotion_gate_reasons surfaces.
* `run_thin_evidence_demote(db)` — sweep + raw-SQL UPDATE writing
  `lifecycle_stage='challenged' + lifecycle_changed_at=NOW() +
  demoted_at=NOW() + promotion_demote_reason=…`. Idempotent (the
  lifecycle != 'promoted' filter short-circuits a second sweep).

### Step 3 — Wire into dispatcher — SHIPPED
`brain_work/dispatcher.py:_handle_execution_feedback_digest`:

* Imports `run_thin_evidence_demote` from `..learning`.
* Calls it inside a try/except after the existing
  `run_live_pattern_depromotion(db)` so a sweep failure doesn't
  poison the digest.
* Folds the sweep's counts into the existing depromotion outcome
  payload via two new keys (`thin_evidence_demoted` +
  `thin_evidence_demoted_ids`) so the
  `execution_quality_updated` outcome carries both signals
  without breaking consumers.

### Step 4 — Tests — SHIPPED (15 tests)
`tests/test_pattern_demote_on_thin_evidence.py`:

**Helper-level (10 tests):**

1. `test_all_criteria_matched_returns_true`.
2. `test_lifecycle_not_promoted_excludes`.
3. `test_trade_count_at_or_above_min_excludes` (boundary at 10).
4. `test_win_rate_at_or_above_floor_excludes` (boundary at 0.33).
5. `test_win_rate_none_excludes`.
6. `test_oos_win_rate_present_excludes`.
7. `test_provisional_gate_reason_absent_excludes`.
8. `test_promotion_gate_reasons_str_json_decoded` — JSONB-as-
   string surface compatibility.
9. `test_pattern_585_audit_fingerprint_matches`.
10. `test_pattern_1011_audit_fingerprint_kept`.

**DB-bound (5 tests):**

11. `test_sweep_demotes_audit_585_fingerprint` — pattern 585 →
    challenged with the expected demote_reason.
12. `test_sweep_keeps_healthy_promoted_patterns` — 1011/1016 stay
    promoted.
13. `test_sweep_does_not_touch_already_challenged` — pattern 1047
    skipped.
14. `test_sweep_idempotent_on_second_run` — no double-touch.
15. `test_provisional_gate_reason_constant_value` — pin the four
    threshold constants so a typo flips red.

### Step 5 — CC report + commit + NEXT_TASK DONE — IN PROGRESS

## Surprises / deviations

1. **Two existing demote paths, both inappropriate.** I considered
   wiring into `brain_work/handlers/demote.py:handle_trade_closed`
   (event-driven on trade close) but its EV-gate has a min-trades
   floor that pattern 585 — at 4 trades — wouldn't even cross to
   evaluate. I considered extending `run_live_pattern_depromotion`
   but its target lifecycle is `decayed` (not `challenged`) and its
   trigger is the live-vs-OOS gap (which can't fire when
   `oos_win_rate IS NULL`). The new sweep is genuinely a third
   demote concern and fits as a sibling.

2. **Raw SQL for the UPDATE.** `demoted_at` and
   `promotion_demote_reason` are present in the schema but not on
   the ORM class. Adding them to `app/models/trading.py` would
   require touching the ORM model AND would cascade into other
   handlers that read it; raw SQL is the surgical choice. The brief
   said "scan_patterns schema columns already exist — no migration"
   so the operator's mental model is already "use the columns
   directly".

3. **`promotion_status` left untouched.** The brief specifies
   `lifecycle_stage='challenged'`, `demoted_at`, and
   `promotion_demote_reason`. It does NOT say to flip
   `promotion_status` (which the existing
   `handle_trade_closed` does set to e.g. `challenged_ev_…`).
   Held the line: this sweep only writes the three columns the
   brief lists.

## Open questions (carried from brief)

1. **Cadence**. Verified: the sweep rides on the
   `execution_feedback_digest` work event, which fires whenever
   the existing `run_live_pattern_depromotion` runs. Per brief,
   that's per-cycle today; pattern 585 should demote within 1–2
   brain cycles after deploy.

2. **Should `evidence_count = 0` also trigger?** Surfaced in the
   brief's open questions; NOT implemented in this sweep. The four
   criteria here are conservative; if pattern 585 demotion proves
   too narrow (other unhealthy patterns slip through), tighten in
   a follow-up.

3. **Pattern 1047 history.** The audit says 1047 is already
   `challenged`; the test
   `test_sweep_does_not_touch_already_challenged` confirms the
   sweep's lifecycle filter skips it, so the operator's prior
   intervention to demote 1047 is preserved.

4. **`projected_profit_below_min`** retuning. Out of scope per
   brief — the threshold reflects economic reality, not pattern
   quality.

## Verification

* `learning.py`: `wc -l` 10102 → 10277 (+175); AST clean.
* `dispatcher.py`: AST clean; `_handle_execution_feedback_digest`
  importable.
* `_matches_thin_evidence_criteria`,
  `run_thin_evidence_demote`, and the four threshold constants
  importable.
* 15/15 tests PASS.
* Splice pattern used (NOT Edit tool) for `learning.py`. Edit tool
  used for the small 20-line dispatcher additions (well under the
  100-line splice threshold for the surface being touched).

## Operator-side after CC ships

Per brief:

1. `git pull` + truncation scan.
2. `docker compose up -d --force-recreate brain-worker scheduler-worker`.
3. **Verify pattern 585 demotes within 1–2 brain cycles** (≤ 10
   min):
   ```sql
   SELECT id, name, lifecycle_stage, promotion_demote_reason, demoted_at
   FROM scan_patterns WHERE id = 585;
   ```
   Expected: `lifecycle_stage='challenged'`,
   `promotion_demote_reason='thin_evidence_low_realized_wr'`,
   `demoted_at` not NULL.
4. **Verify alert flow stops for pattern 585**:
   ```sql
   SELECT COUNT(*) FROM trading_alerts
   WHERE alert_type='pattern_breakout_imminent' AND scan_pattern_id=585
     AND created_at > NOW() - interval '15 minutes';
   ```
   Expected: 0 (alert generation halted because
   `pattern_imminent_alerts` filters on `lifecycle_stage='promoted'`).
5. **Verify 1011/1016 stay promoted**:
   ```sql
   SELECT id, lifecycle_stage, trade_count, win_rate, oos_win_rate
   FROM scan_patterns WHERE id IN (1011, 1016);
   ```
   Expected: both `promoted` (trade_count is well above 10; win_rate
   is well above 0.33).

## Rollback plan

`git revert` the feature commit. The handler is purely additive;
revert simply stops auto-demoting. Pattern 585 will stay
`challenged` in the DB (its lifecycle doesn't auto-revert), which
is the desired state regardless. Manual re-promotion is a SQL
UPDATE the operator can run if needed.

## What's NEXT after this ships

* If the sweep's first run demotes additional patterns beyond 585,
  surface their IDs to the operator for review (the
  `thin_evidence_demoted_ids` field in the
  `execution_quality_updated` outcome payload makes this grep-able).
* If `evidence_count = 0` becomes a stronger signal in operator
  practice, queue a follow-up brief to add it as a 6th criterion.
* The complementary brief
  `f-pattern-oos-revalidation` (re-promote on fresh OOS pass) is
  the natural next move once challenged patterns accumulate;
  intentionally NOT in scope here.
