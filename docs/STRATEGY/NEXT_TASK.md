# NEXT_TASK: f-hygiene-4

STATUS: DONE

## Goal

Investigate and explain the systematic 13–40 bps disagreement between decay-miner mean and realized validation residual at horizon=1800. Apply a surgical fix at the root cause if scope permits in one session, OR diagnose-and-document if it requires structural change. The calibration helpers (`is_score_tradeable`, `is_negative_edge_excluded`, `compute_calibrated_bracket`) all use miner-mean as their gate input — if the predictor is off by ~30 bps from realized truth, every gate decision is built on wrong guidance.

After this task:

1. **The cause of the miner-mean vs validation-residual disagreement is identified** with evidence.
2. **A surgical fix lands at the root** if the diagnosis points to a localized issue, OR a structural-change brief is queued for f-hygiene-5 if it doesn't.
3. **F8b/F9 sequencing has a clean calibration baseline.** F8b on {BTC, SOL} is meaningful only if the predictor it tunes against is accurate.

Up to 2 commits: investigation/diagnosis (may be 0 commits), surgical fix (1 commit if scope permits).

## Why now

- f8a-evaluation-rerun-2 surfaced the disagreement: 13–40 bps systematic positive offset (realized return > miner mean) across 6 cells with `realized_validation_count > 0` at horizon=1800. ETH med h=1800: residual=+31 bps, miner_mean=−16 bps → 47 bps disagreement. SOL high h=1800: residual=+32 bps, miner_mean=−8 bps → 40 bps disagreement. These aren't sampling noise at val_n=1-2; the direction is consistent.
- The systematic positive direction means **the miner is underpredicting realized return**. Negative-edge gate over-blocks signals; tradeability gate is conservative-by-mistake.
- F8b would calibrate `VOL_BREAKOUT_PULLBACK_DELAY_S` against miner-mean. If miner-mean is 30 bps off truth, calibration moves DELAY_S to a wrong value. **Fix the predictor before tuning the parameter.**

## Architectural commitments

- **Investigation first, fix only if warranted.** Same shape as F-hygiene-2's `db_errors` investigation. Five hypotheses; let evidence drive the diagnosis.
- **Surgical fix at the root, not patches at consumers.** If the miner is sampling wrong, fix the miner. Don't paper over by changing the gates' input source (that's a workaround, not a fix).
- **No threshold tuning.** This task identifies what's broken; it doesn't tune anything.
- **No producer-side change to `fast_alerts`.** Same constraint as f-hygiene-3: the catchup-batch dup pattern is intentional.
- **No live-placement changes. Default mode stays paper.**
- **No fast-data-worker restart unnecessarily.** If a fix is loadable without restart (config / data-only), prefer that. If the fix requires restart, document the ~30s soak interruption in the report (same convention as f-hygiene-3.1).

## Scope — investigation, not a-priori fix

### Subtask 1: Diagnose the disagreement source

Five hypotheses, each falsifiable with a specific check. Run all five; let evidence narrow.

#### Hypothesis A — Entry-time bias

The miner observes forward-return at the *alert-fire* moment. Actual paper entry happens after gate decisions (executor poll cycle, gate stack evaluation, broker simulation latency). If the gap is meaningful (>= 1s), the miner's "forward return at horizon=N from fire" doesn't match the executor's "forward return at horizon=N from entry."

**Check:**

```sql
-- Compare alert fired_at to execution executed_at for pullback alerts
SELECT
  AVG(EXTRACT(EPOCH FROM (e.executed_at - a.fired_at))) AS avg_fire_to_entry_s,
  PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (e.executed_at - a.fired_at))) AS median_s,
  PERCENTILE_DISC(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (e.executed_at - a.fired_at))) AS p90_s,
  COUNT(*) AS n
FROM fast_executions e
JOIN fast_alerts a ON a.ticker=e.ticker
                 AND a.alert_type=e.alert_type
                 AND a.fired_at=e.alert_fired_at
WHERE a.alert_type='volume_breakout_pullback_long';
```

**Falsification test:** if median fire-to-entry gap is < 1 second, this hypothesis is rejected (the miner-vs-executor measurement points are too close to explain 30 bps).

#### Hypothesis B — Horizon mismatch

The miner records forward-return at exactly `horizon_s` seconds after fire. Realized exit happens at *variable* time within the closest-horizon bucket. If exits systematically cluster before or after the miner's measurement point, residuals skew.

**Check:**

```sql
-- For each closed exit, compare realized hold time to its mapped horizon
SELECT
  CASE
    WHEN x.holding_period_s < 100 THEN '<100s'
    WHEN x.holding_period_s < 600 THEN '100-600s'
    WHEN x.holding_period_s < 2700 THEN '600-2700s'
    WHEN x.holding_period_s < 5400 THEN '2700-5400s'
    ELSE '>5400s'
  END AS hold_bucket,
  COUNT(*) AS exits,
  AVG(x.realized_return_pct * 100) AS avg_bps
FROM fast_exits x
WHERE x.entry_execution_id IN (
  SELECT e.id FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker
                    AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long'
)
GROUP BY hold_bucket;
```

**Falsification test:** if hold-time within the 1800s mapped bucket varies >> 30% (e.g., some at 1200s, some at 2400s within the "1800s closest-horizon" group), the miner's "exactly 1800s after fire" measurement isn't comparable.

#### Hypothesis C — Price-column mismatch

Miner forward-return uses one price column; realized return uses another. If `close` (or mid) is used by miner, but executor entry/exit uses `best_bid`/`best_ask` (with 1-2 bps spread offset), the systematic difference appears.

**Check:**

```bash
# Read decay_miner.py to find what column it uses for forward-return computation
grep -n "forward_return\|forward_price\|future_close\|future_book\|best_bid\|best_ask\|close" \
  app/services/trading/fast_path/decay_miner.py

# Read fast_exits realized-return computation
grep -rn "realized_return\|realized_pnl" app/services/trading/fast_path/ \
  | grep -v test
```

**Falsification test:** if miner uses `close` and exits use `best_bid`/`best_ask`, the systematic offset is exactly the half-spread. Coinbase BTC-USD spread is ~0.001 bps (negligible); ETH ~4 bps; SOL ~10 bps; DOGE ~90 bps. The 30+ bps disagreement isn't half-spread, but per-ticker disagreements should correlate with spread if this hypothesis holds.

#### Hypothesis D — Catchup-batch dup contamination

f-hygiene-3 surfaced that dup alerts produce identical observations during cold-start backfill. If the cold-start ran during a market regime that's no longer representative, the miner mean is biased toward that regime, and recent realized exits diverge.

**Check:**

```sql
-- How many decay observations come from cold-start backfill vs live observations?
-- Cold-start backfill happens at container startup; live obs happen continuously.
-- The miner's `last_updated` column isn't sufficient because it updates on every change.
-- Best proxy: check fast_alerts that landed with the original snapshot-replay catchup
-- (id <= some cutoff) vs current.
SELECT
  CASE WHEN a.id <= 2300 THEN 'pre-fix-catchup' ELSE 'post-fix-live' END AS era,
  COUNT(*) AS alerts,
  AVG(EXTRACT(EPOCH FROM (NOW() - a.fired_at))) AS avg_age_s
FROM fast_alerts a
WHERE a.alert_type='volume_breakout_pullback_long'
GROUP BY era;
```

**Falsification test:** if pre-fix-catchup era is < 20% of total alerts, the cold-start contamination can't account for 30 bps systematic drift in current measurements.

#### Hypothesis E — Score-bucket mismatch

The miner aggregates by `score_bucket` computed at fire time (in `decay_miner._handle_alert_inserted`). The exit references the SAME bucket via the residual computation. But if the residual computation re-derives the bucket from a slightly different score (e.g., recomputed from features), they won't match.

**Check:**

```bash
grep -n "score_bucket\|bucket_for_score\|score_to_bucket" \
  app/services/trading/fast_path/decay_miner.py \
  app/services/trading/fast_path/calibration.py \
  app/services/trading/fast_path/exit_manager.py
```

**Falsification test:** if alert-time and exit-time both call the same `score_to_bucket(score)` helper with the same `score`, they're identical. If they reference different score columns or recompute, that's the bug.

### Subtask 2: Surgical fix at the identified source

Once Subtask 1 narrows the hypothesis, apply a single targeted fix.

**Decision branches:**

- **Branch A — Hypothesis A (entry-time bias) confirmed.** Fix: shift the miner's forward-return reference point from `fired_at` to `executed_at` for paper-fill cases. Touch `decay_miner` to use the execution timestamp when an execution exists. Surgical, ~10 LOC.

- **Branch B — Hypothesis B (horizon mismatch) confirmed.** Fix: instead of measuring forward-return at exactly `horizon_s`, average the actual hold-time bucket distribution into the residual computation, OR add a hold-time-weighted observation alongside the fixed-horizon one. Larger scope; possibly defer to f-hygiene-5.

- **Branch C — Hypothesis C (price-column mismatch) confirmed.** Fix: align miner's price column with executor's. If executor uses `(best_bid + best_ask) / 2` (mid), miner should too. If executor uses fill prices, miner needs a different reference (deferred to per-trade lookup if computationally feasible).

- **Branch D — Hypothesis D (catchup-batch contamination) confirmed.** Fix: don't include cold-start-replayed observations in the running mean. Either dedupe at observation-write time (touches `decay_miner._handle_alert_inserted`) or filter at read time. Surgical-ish, but the cold-start-backfill section is load-bearing for early-soak data; needs careful handling.

- **Branch E — Hypothesis E (score-bucket mismatch) confirmed.** Fix: ensure both alert-time and exit-time go through the same `score_to_bucket()` helper. Likely a one-liner.

- **Branch F — Multiple hypotheses contribute.** Fix the largest one this session; document the others for f-hygiene-5.

- **Branch G — None of the 5 hypotheses fits.** Document findings; queue f-hygiene-5 with new hypotheses derived from the data.

**Constraint:** if Branch B (horizon mismatch) requires a structural rework of the miner's forward-return logic, defer to its own brief. f-hygiene-4 should ship Subtask 1's diagnosis even if Subtask 2 doesn't fit the session.

**Verification:**

After the fix, restart fast-data-worker (acceptable ~30s soak interruption per f-hygiene-3 precedent). Wait 30+ minutes for new observations and exits to land. Re-run the validation-residual query from f8a-evaluation-rerun-2's report:

```sql
SELECT ticker, score_bucket, horizon_s, sample_count,
       realized_validation_count AS val_n,
       ROUND(realized_validation_residual::numeric * 10000, 2) AS resid_bps,
       ROUND(mean_return::numeric * 10000, 3) AS miner_mean_bps,
       ROUND((realized_validation_residual - mean_return)::numeric * 10000, 2) AS disagreement_bps
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long' AND realized_validation_count>0
ORDER BY horizon_s, ticker;
```

Target: per-cell `disagreement_bps` should drop below the pre-fix range (13–40 bps) on the new validations. Pre-fix-era cells will retain their original residuals (already accumulated); the fix matters for new validations going forward.

## Brain integration (reuse, don't rewrite)

- `decay_miner._handle_alert_inserted` and `_handle_exit_inserted` — extend in place; don't restructure.
- `fast_signal_decay` table — read-only for diagnosis; UPSERT pattern from F-hygiene-3.1 is unchanged.
- `score_to_bucket()` (or equivalent) — verify both call sites use the same helper.
- f-leak-1.5's integrity probe pattern — adapt for the diagnostic queries.
- F-hygiene-3's runbook (`docs/RUNBOOKS/fast_alerts-microsecond-dup.md`) — referenced for any dup-related discussion.

## Constraints / do not touch

- **All 8 live-placement safety belts.** Untouched.
- **Default mode stays paper.**
- **No threshold tuning.** Don't change `VOL_BREAKOUT_MULT`, `VOL_BREAKOUT_PULLBACK_DELAY_S`, MIN_SAMPLES, score-bucket cutoffs, or any gate threshold.
- **No producer-side change to `fast_alerts`.** Catchup-batch dups stay.
- **No miner refactor** beyond the surgical fix at the identified hypothesis site.
- **No migrations.** No schema work.
- **`models/trading.py`, `.env.example`, executor, exit_manager, gate stack, calibration helpers.** Don't touch beyond what the fix-site requires.
- **No new gates.**
- **F8a soak protection:** if a restart is needed, document the ~30s interruption; don't restart unnecessarily.

## Out of scope

- F8b: per-ticker calibration on {BTC, SOL}. Comes after this task.
- F9: signal redesign. Comes after F8b's outcome.
- f-leak-3: still conditional on next OOM event.
- Refactor of decay_miner's flush logic.
- Change to the closest-horizon mapping (separate signal-design decision).
- Cross-pair correlation analysis.
- The "write validations to nearby horizons with weights" idea (separate signal-design decision).

## Success criteria

1. `git log --oneline -5` shows 0–2 new commits, pushed to origin. Diagnosis-only run produces 0 code commits + 1 doc commit; diagnosis + fix produces 1 code commit + 1 doc commit.
2. `docs/STRATEGY/CC_REPORTS/<date>_f-hygiene-4.md` written with:
   - Per-hypothesis check results, with the falsification verdict for each (rejected / supported / inconclusive).
   - The named root cause (or "no single hypothesis explains it" with branch-G framing).
   - The applied fix (or deferred-to-f-hygiene-5 with a brief one-liner).
   - Pre-fix vs post-fix disagreement table for the cells with `realized_validation_count > 0`.
3. F8a soak continues uninterrupted (or the ~30s restart is documented).
4. The recommendation for next NEXT_TASK is named explicitly: F8b on {BTC, SOL}, F9, f-hygiene-5 (deferred fix), or "soak more then re-evaluate."

## Open questions for Cowork (surface in your report only if relevant)

1. **If multiple hypotheses contribute** (Branch F), surface which is the LARGEST contributor and the magnitude of its share. The fix should target that one; the others can wait if they're smaller.

2. **If the fix is in shared code** (`decay_miner.py`, which scheduler-worker also runs), the fix benefits both processes. Confirm via mem_watcher logs.

3. **If the cold-start backfill (Hypothesis D) is the cause**, the fix has a secondary effect: it'd reduce the number of "early-soak observations" the miner sees, which means F-hygiene-3's UPSERT would land more cold-cell rows. This compounds positively.

4. **If Hypothesis B (horizon mismatch) is confirmed**, the fix likely touches signal design, not just code. Defer to f-hygiene-5 with a clear scope. f-hygiene-4 can still ship Subtask 1's diagnosis.

5. **If validation residuals are CONSISTENTLY positive** (which they are at the cells we have), that's a more specific signal than "off by 30 bps" — it means the miner systematically *underpredicts*. That points more strongly to Hypothesis A (entry-time bias: the alert fires, the price moves further before entry, miner records the smaller move; realized return captures the bigger move) or Hypothesis B (horizon mismatch: miner records at h=1800 exactly; realized exits land at variable times when the move has continued).

## Rollback plan

- Subtask 1 (diagnosis): no code changes.
- Subtask 2 (fix): one targeted change to `decay_miner.py` or `calibration.py`. Revert restores prior behavior; miner-mean accuracy returns to pre-fix state. Existing `fast_signal_decay` rows are untouched (the fix affects new observations going forward).
- No migrations. No data migrations. No schema changes.
- No live-placement risk: none of these touch the executor, gates, broker code, or strategy thresholds.
