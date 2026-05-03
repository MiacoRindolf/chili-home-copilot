# NEXT_TASK: f-hygiene-3

STATUS: DONE

## Goal

Two structural-correctness items, both surfaced by prior CC reports as silent measurement bugs that affect F8a evaluation quality. Same shape as F-hygiene-1 and F-hygiene-2: small, soak-safe, no strategy impact, no calibration retuning. Queued first in a three-task sequence (f-hygiene-3 → f8a-evaluation-rerun-2 → F9 / further) per operator's "yes do all of them."

After this task:

1. **Decay-miner's validation-count is no longer silently undercounted by ~95%.** `_handle_exit_inserted`'s UPDATE-only path becomes UPSERT so validation residuals land even when the bucket cell doesn't yet have a forward-return observation.
2. **Downstream queries that join on `(ticker, alert_type, fired_at)` are duplicate-tolerant.** The microsecond-dup pattern (catchup-batch fires multiple alerts with identical `fired_at`) has already bit `decay_miner._handle_exit_inserted` (F-hygiene-2.1) and `f8a-evaluation-rerun`'s report query (n=142 → 37 inflation). Audit the codebase for the same pattern; add `LIMIT 1` / `DISTINCT` / canonical-id reference at remaining sites.
3. **F8a-evaluation-rerun-2 (next task) starts with cleaner data:** validation-count is no longer silently dropped, and any join-based count won't be inflated.

Up to 3 commits. Subtask 2 may be 0 or N commits depending on how many sites need the fix.

## Why now

- f8a-evaluation-rerun's Open Question 2 named the validation-count gap explicitly: ~95% silent drop rate (7 validations vs 142 actual exits). Fix at the source: switch UPDATE → INSERT...ON CONFLICT DO UPDATE for the validation columns.
- f-leak-1.5's postgres integrity probe found the n=142 was JOIN-cardinality inflation (actual = 37 distinct exits) caused by the same microsecond-dup pattern. F-hygiene-2.1 fixed it for `decay_miner._handle_exit_inserted` but not retroactively for analysis queries. Need a codebase audit for remaining sites.
- F8a-evaluation-rerun-2 will run with the same SQL the operator types `claude` to execute. If the underlying data is silently dropping validations or the queries are silently inflating counts, the verdict is wrong by construction. Fix the measurement before re-running the analysis.

## Architectural commitments

- **Surgical fixes at error sites, not refactors.** UPSERT is one site (`decay_miner._handle_exit_inserted`). Microsecond-dup audit is grep-based discovery + per-site `LIMIT 1` / `ORDER BY id DESC LIMIT 1` / `DISTINCT` patches.
- **Don't change insertion semantics.** The `fast_alerts` table has duplicates by design (catchup batch). Adding a UNIQUE constraint would silently drop deferred emits. Fix downstream consumers to be duplicate-tolerant; don't change the producer.
- **No new magic numbers.** Any constants are diagnostic-cadence, not strategy.
- **Idempotent, additive changes.** Subtask 1's UPSERT changes update semantics in a strictly-more-permissive direction (UPDATE-zero-rows → INSERT new row). Subtask 2's audit patches make queries strictly-more-correct.
- **No miner / scanner / executor / gate code changes** beyond Subtask 1's UPSERT and Subtask 2's per-site patches.
- **No migrations.** No schema work — UPSERT is application-level SQL.

## Scope

### Commit 1: Validation-count UPSERT in `_handle_exit_inserted`

**File:** `app/services/trading/fast_path/decay_miner.py`

**What:**

Currently `_handle_exit_inserted` does:

```python
UPDATE fast_signal_decay
SET realized_validation_count = realized_validation_count + 1,
    realized_validation_residual = realized_validation_residual + :residual
WHERE ticker = :ticker AND alert_type = :alert_type
  AND score_bucket = :bucket AND horizon_s = :horizon
```

If the row doesn't exist (the bucket cell hasn't received any forward-return observation yet), this affects 0 rows and the validation event is silently lost. Per f8a-evaluation-rerun: 95% silent drop rate.

**Fix:** Switch to INSERT...ON CONFLICT DO UPDATE:

```python
INSERT INTO fast_signal_decay (
    ticker, alert_type, score_bucket, horizon_s,
    sample_count, mean_return, m2_return,
    realized_validation_count, realized_validation_residual,
    last_updated
)
VALUES (
    :ticker, :alert_type, :bucket, :horizon,
    0, 0, 0,                                            -- no forward-return obs yet
    1, :residual,
    NOW()
)
ON CONFLICT (ticker, alert_type, score_bucket, horizon_s)
DO UPDATE SET
    realized_validation_count = fast_signal_decay.realized_validation_count + 1,
    realized_validation_residual = fast_signal_decay.realized_validation_residual + EXCLUDED.realized_validation_residual,
    last_updated = NOW()
```

**Caveat:** This creates rows where `sample_count = 0`, `mean_return = 0`, `m2_return = 0`, but `realized_validation_count > 0`. Downstream consumers that read mean_return must already handle `sample_count = 0` correctly (it means "no observation"). Verify by grep before merging:

```bash
grep -rn "mean_return\|fast_signal_decay" app/ | grep -v test
```

If any consumer reads `mean_return` without first checking `sample_count > 0`, that's a separate bug (and not introduced by this fix; just exposed). Flag in the report; don't fix in this commit.

**Verification:**

Before fix:

```sql
SELECT realized_validation_count, COUNT(*) AS cells
FROM fast_signal_decay
WHERE alert_type = 'volume_breakout_pullback_long'
GROUP BY realized_validation_count;
```

After fix + 30 min of new exits:

Same query; expect the `realized_validation_count > 0` row count to grow proportionally to actual `fast_exits` rows referencing pullback alerts. Target: validation-count sum approaching the distinct-exit count (37 currently for pullback) instead of stuck at 7.

### Commit 2 (or N): Microsecond-dup query audit + per-site fix

**Files:** TBD via grep — anywhere a JOIN-on-`(ticker, alert_type, fired_at)` is used.

**What:**

The `fast_alerts` table has rows with identical `(ticker, alert_type, fired_at)` to the microsecond, due to the catchup-batch pattern (F8a's snapshot-replay fires multiple deferred emits stamped with the same `now()`). F-hygiene-2.1 fixed this for `decay_miner._handle_exit_inserted`'s `.one_or_none()` → `.first()` with `ORDER BY id DESC LIMIT 1`. Other sites probably have the same issue.

**Steps:**

1. Find all JOINs / lookups against `fast_alerts` on the composite key:

   ```bash
   grep -rnE "FROM fast_alerts|JOIN fast_alerts|fast_alerts\." app/ | grep -v test
   grep -rnE "\.alert_type\s*==.*\.fired_at\s*==|alert_type\s*=.*fired_at\s*=" app/
   ```

2. For each match, classify:
   - **Aggregate (COUNT, SUM, AVG, etc.):** Likely affected by JOIN inflation. Add `DISTINCT ON (...)` or rewrite as `IN (subquery)` returning canonical ids. Same shape as f-leak-1.5's integrity probe.
   - **Single-row lookup:** Add `ORDER BY id DESC LIMIT 1` (same convention as F-hygiene-2.1).
   - **Already canonical-id-keyed (joins on `fast_alerts.id`):** No change needed.

3. Apply the per-site patch.

**Decision branches:**

- **A. Few sites (≤ 3) need fix.** One commit per site, plus optionally a final commit consolidating any shared utility.
- **B. Many sites (> 3) need fix.** Group by file. Multiple commits OK; each one says explicitly which site/sites and what classification.
- **C. Zero sites need fix.** F-hygiene-2.1 + the canonical-id-keyed joins already cover everything. Document in CC report; no commit.

**Constraint:** Don't add a UNIQUE constraint on `(ticker, alert_type, fired_at)`. The producer relies on duplicates landing. If we ever add nanosecond-resolution fired_at or a sub-microsecond serial column, that's a separate structural decision — not in this hygiene pass.

**Verification:**

Re-run the integrity probe (`scripts/dispatch-postgres-integrity.ps1`) and the f8a-evaluation-rerun report query. Specifically:

```sql
-- Distinct pullback exit count (should be ~37, NOT ~142):
SELECT COUNT(*) FILTER (WHERE entry_execution_id IN (
  SELECT e.id FROM fast_executions e
  JOIN fast_alerts a ON a.ticker = e.ticker
                    AND a.alert_type = e.alert_type
                    AND a.fired_at = e.alert_fired_at
  WHERE a.alert_type = 'volume_breakout_pullback_long'
)) AS pullback_exits FROM fast_exits;
```

If the counts now match across all canonical/inflated query forms, Subtask 2 is verified.

### Commit 3 (optional): Document the convention

**File:** `docs/RUNBOOKS/fast_alerts-microsecond-dup.md` (new) OR a comment block at the top of `decay_miner.py`.

**What:**

Brief runbook explaining the duplicate-microsecond pattern, why it's accepted (catchup batch is intentional), and the canonical query patterns (`ORDER BY id DESC LIMIT 1` for single-row lookups, `IN (SELECT id ...)` for aggregates, `JOIN ON fast_alerts.id` when an upstream table has the canonical id).

This makes the convention discoverable for future contributors and prevents the same bug from re-emerging.

**Decision:** Optional. If Subtask 2 finds 0 sites needing fix, skip this; the convention is already encoded in the existing fix sites. If it finds many, the runbook is worth writing.

## Brain integration (reuse, don't rewrite)

- `_handle_exit_inserted` in `decay_miner.py` — extend the existing UPDATE in place, don't restructure.
- F-hygiene-2.1's `ORDER BY id DESC LIMIT 1` convention — reuse exactly.
- f-leak-1.5's integrity probe — used for verification.
- F8a-fix's `id > 2300` convention — used in the verification SQL.

## Constraints / do not touch

- **All 8 live-placement safety belts.** Untouched.
- **Default mode stays paper.**
- **No strategy threshold tuning.** Don't change `VOL_BREAKOUT_MULT`, `VOL_BREAKOUT_PULLBACK_DELAY_S`, MIN_SAMPLES, or any gate threshold.
- **No miner Welford-update path changes** beyond Subtask 1's UPSERT. The mean/m2 computation is unchanged; only the validation-count side becomes UPSERT.
- **No migrations.**
- **No new gates.**
- **No producer-side change to `fast_alerts`.** Don't add a UNIQUE constraint, don't add a sub-microsecond serial, don't change `fired_at` resolution.
- **No fast-data-worker restart** (would interrupt F8a soak that's accumulating data for f8a-evaluation-rerun-2).
- **`models/trading.py`, `.env.example`, executor, exit_manager, gate stack, calibration helpers.** Continue to leave alone.

## Out of scope

- f8a-evaluation-rerun-2 (next-after-this task).
- F9 (queued after that).
- f-leak-3 (still conditional on next OOM event).
- scheduler-worker per-Thread closure leak (separate, future task per f-leak-2 review).
- Any tuning of any threshold.
- Producer-side change to make `fast_alerts` canonical-id-keyed at insertion (would require migration; structural decision).
- Refactor of decay_miner's flush logic.

## Success criteria

1. `git log --oneline -5` shows 1–3 new commits, pushed to origin. Each clearly identifies subtask and site.
2. Subtask 1 verified: `realized_validation_count` for `volume_breakout_pullback_long` no longer stuck at 7. Specific target depends on actual exit-count growth over the verification window — but the *ratio* of validations to exits should approach 1:1 instead of ~1:20.
3. Subtask 2 verified: any aggregate query against `fast_alerts` joined on the composite key returns the distinct count, not the inflated count. Re-run f-leak-1.5's integrity probe to confirm.
4. F8a soak continues uninterrupted on fast-data-worker. No behavioral changes to any strategy code path.
5. `docs/STRATEGY/CC_REPORTS/<date>_f-hygiene-3.md` written following PROTOCOL.md format. Include:
   - Subtask 1: pre-fix and post-fix validation-count distributions.
   - Subtask 2: list of sites found, per-site classification, per-site patch.
   - Verbatim verification SQL for next review.

## Open questions for Cowork (surface in your report only if relevant)

1. **If Subtask 2 finds zero sites needing fix**, that means F-hygiene-2.1 already covered everything. Confirm by re-running the f-leak-1.5 integrity probe AND the f8a-evaluation-rerun report query. If both return canonical counts, Subtask 2 is closed cleanly.

2. **The UPSERT in Subtask 1 creates `sample_count = 0, realized_validation_count > 0` rows.** Downstream consumers that read `mean_return` should already gate on `sample_count > 0`. If grep finds a consumer that doesn't, that's a pre-existing bug surfaced by this fix — not caused by it. Flag separately; don't fold the fix into f-hygiene-3.

3. **If many query sites need patching (Subtask 2 branch B)**, consider whether a shared helper would prevent re-emergence. Same pattern as F-hygiene-2's "code-twin between exit_manager and decay_miner" observation. Not in scope for this task; surface as a future structural-pass candidate.

4. **The "F-hygiene-3 validation cleanup" makes f8a-evaluation-rerun-2 measurement-grade-cleaner**, but doesn't change any strategy code. If this hygiene pass surfaces a strategy-relevant gap (e.g., the validation residuals start showing meaningful per-bucket signal once they accumulate), surface for f8a-evaluation-rerun-2's brief — but don't act on it here.

## Rollback plan

- Subtask 1 (UPSERT): targeted SQL change in one method. Revert restores prior UPDATE-only behavior; validation-count goes back to ~95% silent drop. Existing rows are untouched.
- Subtask 2 (per-site patches): each is a localized change. Per-site revert restores prior query; would re-introduce inflation for that site only.
- Subtask 3 (optional runbook): purely additive doc.
- No migrations. No data migrations. No schema changes.
- No live-placement risk: none of these touch the executor, gates, broker code, or strategy thresholds.
