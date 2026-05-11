# CPCV gate coverage audit (Phase 0 of f-adaptive-promotion-architecture)

Date: 2026-05-11
Author: Claude Code (executor)
Brief: `docs/STRATEGY/QUEUED/f-cpcv-gate-coverage-audit.md`
Parent: `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`
Audit scripts: `scripts/audit-cpcv-gate-coverage.ps1`,
`scripts/audit-cpcv-gate-force-eval.ps1`
Raw output: `scripts/audit-cpcv-gate-coverage-out.txt`,
`scripts/audit-cpcv-gate-force-eval-731-out.txt`,
`scripts/audit-cpcv-gate-force-eval-1212-out.txt`

## TL;DR

The funnel breaks **before** the CPCV gate handler can produce a verdict.
Of 50 audited candidate patterns (PTR>=30, `cpcv_n_paths IS NULL`,
lifecycle not promoted/retired), 26 (**52.0%**) have NO
`backtest_completed` event ever fired; the other 24 (**48.0%**) DO have
a `backtest_completed` event marked `done`, **yet not a single
`[brain_work:cpcv_gate]` log line exists in the entire brain-worker
history** (or any other container). The handler is not running on
either bucket.

Force-eval surfaced a second, separable blocker: even when the handler
IS reached, patterns with >=30 PTR rows can fail an **ensemble pre-gate
inside `check_promotion_ready`** (line 341 of `mining_validation.py`)
before CPCV runs at all. Both probed patterns (731, 13,696 rows; 1212,
7,095 rows) returned `detail_blocked = "ensemble_failed"` with an empty
`cpcv_promotion_gate` payload. In that path, `cpcv_n_paths` remains
NULL **by design** because `cpcv_eval_to_scan_pattern_fields({})`
returns an empty patch (`promotion_gate.py:1042-1060`).

These two findings combine: **Phase 1 backfill of synthetic
`backtest_completed` events will not, on its own, resolve the drought.**
The dispatcher's outcome-handler path is silent, AND the gate would
short-circuit at the ensemble pre-gate for at least some of the 275.

## Methodology

- Sample: 50 of the 275 candidate patterns surfaced by the parent
  brief (cap per the task brief, ordered by `ptr_rows DESC`).
- Selection mirrors `cpcv_gate.handle_backtest_completed`'s own
  preconditions (lifecycle not in {promoted,retired}, PTR >= 30,
  `outcome_return_pct IS NOT NULL`).
- For each candidate, the most recent `brain_work_events` row with
  `event_type='backtest_completed'` AND
  `payload->>'scan_pattern_id' = pid` is joined via LATERAL.
- Handler-log evidence: `docker logs --since 24h
  chili-home-copilot-brain-worker-1` greped for
  `[brain_work:cpcv_gate]` AND either `ev_id=<event_id>` or
  `pattern_id=<pid>`.
- Force-eval (`audit-cpcv-gate-force-eval.ps1`): runs
  `check_promotion_ready` against one pattern inside the brain-worker
  container, then `sess.rollback()` in `finally` — no DB writes.

All `psql` calls were SELECT/COPY-only. No `app/` edits. No restarts.
No new migrations / tables / columns. No env edits.

## Classification breakdown (exact percentages)

| classification                  | count | pct    |
|---------------------------------|-------|--------|
| event_missing                   |    26 |  52.0% |
| event_pending_or_retry          |     0 |   0.0% |
| event_dead                      |     0 |   0.0% |
| event_done_but_no_handler_log   |    24 |  48.0% |
| handler_logged_but_no_persist   |     0 |   0.0% |
| unknown                         |     0 |   0.0% |
| **TOTAL**                       |  **50** | **100.0%** |

(Source: `scripts/audit-cpcv-gate-coverage-out.txt` line 12–22.)

## Cross-check: full-history log counts

Run from the host at 2026-05-11T15:18Z:

```
docker logs chili-home-copilot-brain-worker-1 | grep -c "brain_work:cpcv_gate"  ->  0
docker logs chili-home-copilot-brain-worker-1 | grep -c "brain_work:dispatch"   ->  0
docker logs chili-home-copilot-brain-worker-1 | grep -c "handler_verify"        ->  1   (startup OK)
```

Same `grep -c` returns 0 across `scheduler-worker-1`,
`autotrader-worker-1`, `broker-sync-worker-1`, `fast-data-worker-1`,
and `chili-1` for both prefixes.

Yet `brain_work_events` shows **205 `backtest_completed` rows with
`status='done'` in the last 24h** (first `2026-05-10 15:28:15Z`, last
`2026-05-11 15:08:43Z`). Something is marking events done outside
`run_brain_work_dispatch_round`. That writer is not yet identified.

## Force-eval results

`scripts/audit-cpcv-gate-force-eval-731-out.txt`:

| field                  | value                                            |
|------------------------|--------------------------------------------------|
| pattern_id             | 731                                              |
| pattern_name           | Intraday Squeeze + Declining Volume [1m] [BOS-tight] |
| lifecycle_stage        | candidate                                        |
| pattern_evidence_kind  | realized_pnl                                     |
| ptr_rows_loaded        | 13,696                                           |
| `ok` (ready=)          | **False**                                        |
| `detail.blocked`       | **`ensemble_failed`**                            |
| `detail` keys          | `['blocked', 'ensemble']` (no `cpcv_promotion_gate`) |
| `scan_pattern_patch`   | `{}` (nothing to write)                          |

`scripts/audit-cpcv-gate-force-eval-1212-out.txt`:

| field                  | value                                            |
|------------------------|--------------------------------------------------|
| pattern_id             | 1212                                             |
| pattern_name           | Above SMA20 + RSI>50 + ADX>20 (healthy uptrend) [tf-4h] |
| lifecycle_stage        | candidate                                        |
| ptr_rows_loaded        | 7,095                                            |
| `detail.blocked`       | **`ensemble_failed`**                            |
| `scan_pattern_patch`   | `{}`                                             |

Both patterns short-circuit before `finalize_promotion_with_cpcv` runs,
so no CPCV metrics are produced even though >>30 PTR rows are available.
This is **separate from the dispatcher silence** above; even with a
healthy dispatcher, these two patterns' `cpcv_n_paths` would still stay
NULL.

## Where the funnel breaks (the verdict)

Two stacked breaks. Both are real; neither alone explains 275.

**Break #1 — dispatcher silence (100% of audited sample).**
The 24 `event_done_but_no_handler_log` rows are not handler-import
failures (`[handler_verify] OK 6/6` fired at startup) — they're a
broader dispatch outage. `run_brain_work_dispatch_round` has emitted
**zero** `[brain_work:dispatch]` log lines in the brain-worker
container's full history. With the dispatcher silent, the 26
`event_missing` patterns are not waiting on a misrouted event — they
are waiting on a producer (an emitter call from `backtest_queue_worker`
or equivalent) that hasn't fired for them. **Phase 1 backfill of
synthetic `backtest_completed` events is fine on its own, but the
events will not be drained** until the dispatcher path is restored.

**Break #2 — ensemble pre-gate inside `check_promotion_ready` (sample of 2/2).**
For both force-evaluated patterns (8% of audited sample, but probed
specifically because they had the highest trade counts), the gate
exited at `_promotion_min_ensemble_hypothesis` before
`finalize_promotion_with_cpcv` was reached. In this branch the handler
sets `lifecycle_stage='challenged'` but writes no `cpcv_*` numeric
fields, because `cpcv_eval_to_scan_pattern_fields({})` returns `{}`
(`promotion_gate.py:1042-1046`). **The audit signal `cpcv_n_paths IS
NULL` therefore conflates two states: "handler never ran" AND
"handler ran but ensemble pre-gate blocked CPCV evaluation."**

The conflation matters: a successful Phase 1 backfill would move
ensemble-failing patterns from `lifecycle_stage='candidate'` to
`'challenged'` without ever populating `cpcv_n_paths`. That outcome is
useful (challenged is a definite verdict) but the drought-metric the
parent brief tracks (n patterns with non-NULL CPCV) will not improve
for the ensemble-failing subset.

## Caveats

1. **Brain-worker uptime window.** Container has been up only ~4h
   (started ~2026-05-11T11:14Z; first log line is the
   `[docker-entrypoint-chili]` boot message). The 24h log grep
   therefore over-counts the `event_done_but_no_handler_log` bucket
   *for events created before the restart* — but the full-history
   `grep -c "brain_work:cpcv_gate"` returning **0** rules that caveat
   out: even events created since 11:14Z generated no handler logs.
2. **Sample is 50 of 275.** Cap from the brief. Pattern-ID ordering
   was by `ptr_rows DESC`, so the sample is biased toward the heaviest
   patterns (the `event_done_but_no_handler_log` ones have IDs
   clustered around 533–1240 with 4,500–7,100 PTR rows). Bucket counts
   may shift across the remaining 225 — but the two breaks (dispatcher
   silence, ensemble pre-gate) are systemic and apply project-wide.
3. **Force-eval is 2 patterns.** Both high-trade-count and both
   ensemble_failed. The relationship between ensemble_failed and
   pattern characteristics (timeframe, evidence_kind, hypothesis_family)
   is unaudited here; could be selection effect from the
   `ptr_rows DESC` cap.
4. **Identity of the "done"-writer is unknown.** Phase 1 design must
   not assume `run_brain_work_dispatch_round` will pick up enqueued
   events; some other path is marking events done at ~9/hour without
   logging through the dispatcher prefix. Find it before backfilling.

## Recommendation for Phase 1

A naive backfill of synthetic `backtest_completed` events into
`brain_work_events` is **insufficient**. Two-stage remediation:

**Stage 1a — restore dispatcher observability (operator/Cowork side,
not a Phase 1 deliverable per se).** Determine why
`run_brain_work_dispatch_round` is not logging — whether the loop runs
under a different worker, whether `brain_work_ledger_enabled()` returns
False, whether log level filtering hides the dispatch prefix, or
whether some legacy path (`run_learning_cycle` despite the `DISABLED`
log line, or the `backtest_queue_worker`) is marking events done
without invoking handlers. The audit script generated all the inputs
this stage needs.

**Stage 1b — synthetic-event backfill (the original Phase 1
deliverable).** Once Stage 1a confirms the dispatcher will drain new
events, enqueue one `backtest_completed` event per candidate with
payload:

```python
{
  "scan_pattern_id": <int>,
  "source": "cpcv_backfill_2026_05_11",
  "synthetic": True,
}
```

Rate-limit at N per minute (default N=8 matches
`brain_work_cpcv_gate_batch_size`). Re-running is safe — the handler's
own `lifecycle_stage NOT IN ('promoted','retired')` guard (line 76)
prevents re-litigating decided patterns. Expected outcome from the
275:
- Some subset will reach the CPCV gate, produce a verdict, and
  populate `cpcv_n_paths` + lifecycle change to `backtested` or
  `challenged`.
- A subset will short-circuit at the ensemble pre-gate (per the
  force-eval finding) — lifecycle moves to `challenged` but
  `cpcv_n_paths` stays NULL. Phase 1 should track BOTH state
  transitions (not just `cpcv_n_paths` non-NULL) when measuring
  backfill effectiveness.

**Stage 1c — surface the ensemble pre-gate as a separate audit
dimension.** Phase 2's adaptive-gate redesign should treat
`ensemble_failed` patterns explicitly. Today they look identical to
"never evaluated" in `scan_patterns.cpcv_*` columns. A new column
(or a payload tag in the new `cpcv_adaptive_eval_log` table the parent
brief proposes) should record the ensemble-failed verdict so the
operator can see how many patterns are genuinely under-evidenced vs
how many failed an upstream gate.

## Files produced by this audit

| Deliverable | Path                                                          | Size  |
|-------------|----------------------------------------------------------------|-------|
| D1 (script) | `scripts/audit-cpcv-gate-coverage.ps1`                         | 207 lines |
| D2 (script) | `scripts/audit-cpcv-gate-force-eval.ps1`                       | 158 lines |
| D3 (run)    | `scripts/audit-cpcv-gate-coverage-out.txt`                     | ~120 lines |
| D3 (run)    | `scripts/audit-cpcv-gate-force-eval-731-out.txt`               | 38 lines |
| D3 (run)    | `scripts/audit-cpcv-gate-force-eval-1212-out.txt`              | 38 lines |
| D4 (memo)   | `docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`                 | this file |
| D5 (report) | `docs/STRATEGY/CC_REPORTS/2026-05-11_cpcv-gate-coverage-audit.md` | separate |

## Open questions surfaced for Cowork

1. **Dispatcher silence: bug or expected?** If `run_brain_work_dispatch_round`
   has been intentionally muted (log level, feature flag), the audit
   should re-classify `event_done_but_no_handler_log` as "expected".
   If it's a regression, Stage 1a above is critical and predates any
   Phase 1 backfill brief.
2. **Who is marking events `done`?** 205 events/24h, 0 dispatcher log
   lines. Some other writer is involved. Identification likely
   requires either `pg_stat_activity`/`pg_stat_user_functions` audit
   or strategic logger.warning insertion (out of scope for Phase 0).
3. **Ensemble pre-gate visibility.** Should Phase 2's adaptive-gate
   redesign treat `ensemble_failed` as a separate, surfaced state, or
   continue to roll it into "blocked"?
