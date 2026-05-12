# Quality-score handler & adaptive-gate composite axis

**Author:** Claude Code (executor) on 2026-05-11
**Brief:** `docs/STRATEGY/QUEUED/f-composite-quality-event-driven.md`
**Phase:** 3 of `f-adaptive-promotion-architecture`.

## 1. What the handler does

`app/services/trading/brain_work/handlers/quality_score.py` subscribes
to two event types via the dispatcher's per-event-type branches in
`app/services/trading/brain_work/dispatcher.py`:

| Event type             | Entry function                              |
|-----------------------|---------------------------------------------|
| `backtest_completed`  | `handle_backtest_completed_quality`         |
| `live_trade_closed`   | `handle_trade_closed_quality`               |
| `paper_trade_closed`  | `handle_trade_closed_quality`               |
| `broker_fill_closed`  | `handle_trade_closed_quality`               |

For each event, the handler:

1. Opens a fresh `SessionLocal()` (same isolation pattern as
   `pattern_stats.py` / `cpcv_gate.py`).
2. Loads the pattern row + the per-pattern slice from
   `pattern_directional_quality_v` + the per-pattern rolling-30 decay
   split.
3. Computes the composite via the existing pure function
   `pattern_quality_score.compute_quality_composite_score`. No new
   model; same math as the nightly cohort batch.
4. **Conditional write:** only writes `scan_patterns.quality_composite_score`
   when the recomputed value differs from the persisted one. This is
   the idempotency contract — a second call against the same DB state
   produces zero writes and zero outcome emits.
5. Emits a `pattern_quality_recomputed` **outcome** event with
   payload `{scan_pattern_id, old_score, new_score, source,
   recomputed_at, parent_work_event_id}` so downstream consumers (e.g.
   the adaptive gate's shadow log) can observe the recompute.

The handler runs **last** in the dispatcher's per-event chain so the
upstream handlers (`cpcv_gate`, `pattern_stats`, `regime_ledger`,
`live_drift`, `execution_robustness`, `demote`) have already committed
their writes before the composite is recalculated.

### Failure containment

- Inner exceptions are wrapped at the handler boundary: the session
  rolls back, a warning is logged with `exc_info=True`, and the handler
  returns normally. The upstream commits survive a broken composite
  recompute — this matches the precedent in `pattern_stats.py` and
  `live_drift.py`.
- The dispatcher's per-event-type try/except adds a second layer of
  containment (logs `quality_score (backtest_completed) handler
  failed ev_id=… : …`). A broken composite handler **cannot poison**
  the cpcv_gate or demote / regime chain.

### NULL handling

The handler intentionally produces `NULL` for any of these cases:

- `pat.cpcv_median_sharpe`, `pat.deflated_sharpe`, or `pat.pbo` is
  NULL.
- The pattern has no row in `pattern_directional_quality_v`
  (no labeled directional outcomes).
- `rolling_sample_n < 30` (decay is un-computable on partial windows).
- The decay split rolls back due to a query error.

NULL is **not** a magic-default fallback — it's NULL propagation per
advisor brief §2.6. Downstream consumers (the adaptive gate's 4th
axis) treat a NULL candidate composite as the pool mean (Q1 default).

### Retired patterns

`lifecycle_stage='retired'` short-circuits before any recompute work.
No DB reads beyond the pattern row, no writes, no emits.

## 2. How to read `quality_composite_score`

The score is in `[0, 1]` when weights sum to ≈1.0. Components and
contribution under default weights (declared in
`app/config.py` → `chili_cohort_score_weight_*`):

| Component         | Weight | Source field / view                           |
|------------------|-------:|----------------------------------------------|
| CPCV Sharpe (normalized to 2.0 = full credit) | 0.30 | `scan_patterns.cpcv_median_sharpe` |
| Deflated Sharpe (1.0 = full credit) | 0.20 | `scan_patterns.deflated_sharpe` |
| PBO-inverse (lower is better) | 0.15 | `scan_patterns.pbo` |
| Directional WR (rolling-30) | 0.25 | `pattern_directional_quality_v.rolling_directional_wr` |
| Decay-inverse (improving is full credit) | 0.10 | derived from `pattern_alert_directional_outcome` |

Calibration anchor: `cpcv_median_sharpe >= 1.0` lands at half-credit
on the CPCV component (the gate floor); `>= 2.0` saturates.

A score is **NULL** when any required component is missing — see
"NULL handling" above. As of 2026-05-11 the production scan_patterns
table has ~584 of 586 patterns NULL because the nightly cohort
refresh has not been firing on the new Phase 2 pool; the backfill
script below repopulates them.

## 3. Backfill operations

The one-shot backfill is `scripts/quality-score-backfill.ps1`.
It calls the streaming wrapper
`pattern_quality_score.compute_and_persist_scores_streaming` inside
the `chili` Docker container. The script's contract:

- **`-DryRun` defaults to `$true`.** Live runs require explicit
  `-DryRun:$false`. Dry-run mode loads, computes, and emits the
  would-write distribution; per-batch transactions roll back so no
  UPDATE commits.
- **Kill switch:** touch `scripts/quality-score-backfill-stop.flag`
  while the script is running. The Python streaming wrapper checks
  the flag between batches; the loop exits cleanly with
  `"stopped_by_flag": true` in the result JSON.
- **Per-pattern progress log:** pass `-VerboseProgress` to emit one
  JSON line per pattern (id, old_score, new_score, changed,
  directional_wr, sample_n, decay) to
  `scripts/quality-score-backfill-progress.log`.

### Standard procedure

```powershell
# 1. Dry-run first; read the distribution.
.\scripts\quality-score-backfill.ps1

# 2. Verbose dry-run if you want per-pattern detail.
.\scripts\quality-score-backfill.ps1 -VerboseProgress

# 3. Live run (commits per batch).
.\scripts\quality-score-backfill.ps1 -DryRun:$false

# 4. Kill switch — touch the flag from another terminal.
New-Item -ItemType File -Path .\scripts\quality-score-backfill-stop.flag
# To allow another live run later:
Remove-Item .\scripts\quality-score-backfill-stop.flag
```

The current pattern population is ~586. Per-pattern compute is
sub-second; a full backfill is roughly 30-60 seconds. No explicit
inter-batch rate limit is needed — the streaming wrapper commits per
`-BatchSize` (default 50) and polls the kill flag between batches.

## 4. Rollback

The handler is additive — there is no migration to revert.

**To stop the 4th Pareto axis from influencing promotion verdicts**
(useful when shadow-log shows divergence the operator wants to
investigate before acting):

```bash
# Setting via psql (settings table is the source-of-truth for runtime flags).
# The flag is named in app/config.py.
docker compose exec -T postgres psql -U chili -d chili -c \
  "UPDATE app_settings SET value='false' WHERE key='chili_cpcv_adaptive_gate_enabled';"
```

Flag-OFF semantics: the wrapper still computes the 4-axis adaptive
verdict and writes the shadow log (so the divergence remains
observable), but `maybe_apply_adaptive_gate` returns the **legacy**
3-D verdict. The composite axis is still computed and persisted but
unused for promotion decisions.

**To stop the handler from running entirely** — there is no handler-
level kill switch. The only blanket disable is Phase 1b's
`chili_brain_outcome_claimable_enabled=False`, which would stop the
broader outcome-claimable path. A future enhancement (out of scope
here) would add a per-handler `chili_brain_quality_score_handler_enabled`
flag; for now, treat the handler as always-on once the dispatcher
release ships.

**To clear the composite column entirely** (useful when investigating
a bad score pipeline):

```sql
UPDATE scan_patterns SET quality_composite_score = NULL WHERE active;
```

The handler will repopulate it on the next backtest_completed /
trade-close event for each pattern, or the operator can re-run the
backfill script.

## 5. Interaction with the adaptive gate's 4D Pareto axis

`app/services/trading/cpcv_adaptive_gate.py` was edited additively in
this phase to add composite as the 4th Pareto axis alongside DSR / PBO
/ median_sharpe. Key behaviors:

- **Pool composite array** is loaded alongside the other 3 metrics in
  `_load_pool_metrics`. NULL composites are excluded from the array
  (consistent with the other metrics' NULL handling).
- **Pool mean** of the composite array is the imputation value for
  NULL candidates. When the pool composite array is empty (pre-
  backfill state), the imputation falls back to 0.5 — the [0,1]
  midpoint — so the math stays well-defined.
- **Wrapper-reads-DB:** when the caller doesn't thread the candidate's
  composite via `eval_payload["quality_composite_score"]`, the wrapper
  reads `scan_patterns.quality_composite_score` for the candidate
  itself. One indexed lookup per gate call; no change to
  `promotion_gate.py`.
- **4-tuple Pareto:** `_pareto_dominated` is now generic over tuple
  width via `zip`. Both pool members and candidate are 4-tuples;
  pool members with NULL composite fill in with `comp_pool_mean` so
  the comparison is well-defined.
- **Threshold check:** when the candidate's composite is present, the
  wrapper compares it to the pool's empirical `q=0.95` percentile (the
  same quantile used for the other metrics). When the candidate's
  composite is NULL, the axis is treated as eligible by default — a
  missing composite must not block a candidate during the backfill
  window.

The shadow log now writes one extra row per evaluation (4 metric rows
+ 1 summary row), capturing the composite axis's raw / shrunken /
threshold values. No migration required — the
`cpcv_adaptive_eval_log` table accepts arbitrary `metric_name` values.

## 6. Known limitations

- **No handler-level kill switch.** Documented above. A future
  enhancement.
- **Per-event recompute is per-pattern.** The handler does not batch
  multiple events into one recompute; if 50 trade-close events arrive
  in the same dispatch round, the handler runs 50 times. Per-pattern
  compute is cheap (one indexed pattern read + two SQL queries +
  pure math), so this is acceptable. If the composite-recompute load
  becomes measurable, a future debounce (similar to
  `enqueue_or_refresh_debounced_work`) would be the right shape.
- **Composite emit dedupe.** Each recompute emits a
  `pattern_quality_recomputed` outcome with a dedupe key that
  incorporates the rounded new score. Identical-score re-emissions
  collapse to a single row; distinct scores produce distinct rows.
  This is consistent with the Phase 1b outcome-event idempotency
  contract.

## 7. Related runbooks

- `docs/runbooks/BRAIN_WORK_EVENT_KIND.md` — Phase 1b unified event
  queue.
- `docs/runbooks/BRAIN_EVENT_BACKFILL.md` — Phase 1c historical-orphan
  backfill mechanism.
- `docs/PHASE_ROLLBACK_RUNBOOK.md` — generic phase-flag rollback
  procedure.
