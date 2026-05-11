# CC_REPORT: f-cpcv-gate-coverage-audit (Phase 0)

Date: 2026-05-11
Session: `cpcv-gate-coverage-audit-2026-05-11`
Plan-gate: APPROVED at 2026-05-11T15:12:30+00:00 (autonomous, Cowork scheduled-task)

## What shipped

One commit. All five deliverables landed under `scripts/` and `docs/`:

| Deliverable | Path                                                              |
|-------------|--------------------------------------------------------------------|
| D1          | `scripts/audit-cpcv-gate-coverage.ps1` (207 lines, parses)         |
| D2          | `scripts/audit-cpcv-gate-force-eval.ps1` (158 lines, parses)       |
| D3          | `scripts/audit-cpcv-gate-coverage-out.txt`                         |
| D3          | `scripts/audit-cpcv-gate-force-eval-731-out.txt`                   |
| D3          | `scripts/audit-cpcv-gate-force-eval-1212-out.txt` (extra force-eval) |
| D4          | `docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`                     |
| D5          | this file                                                          |

Plus `docs/STRATEGY/NEXT_TASK.md` flipped `STATUS: PENDING -> DONE`.

No `app/` code changes. No migrations. No DB writes (force-eval wraps
the whole evaluation in `try/finally: sess.rollback(); sess.close()`).
No restarts. No env edits.

## Verification

- Both `.ps1` files pass `[System.Management.Automation.Language.Parser]::ParseFile`.
- D1 ran end-to-end (`audit complete` exit 0, COPY exported 50 rows).
- D2 ran twice (pid=731 and pid=1212), both with `stage=done` and
  clean rollback.
- Classification table is sourced from the script's own emitted
  PowerShell-side tally; pcts rounded to 1 decimal: 52.0% / 0.0% /
  0.0% / 48.0% / 0.0% / 0.0%.

## Key findings (referenced in detail in D4)

1. **Funnel breaks before the CPCV gate.** 26/50 (52.0%) have no
   `backtest_completed` event ever fired; 24/50 (48.0%) have an event
   marked `done` but no `[brain_work:cpcv_gate]` log line ever.
2. **Dispatcher is silent.** `grep -c "brain_work:cpcv_gate"` and
   `grep -c "brain_work:dispatch"` against `docker logs` for *every*
   chili container return **0** across the full container history.
   Yet 205 `backtest_completed` events were marked `done` in the last
   24h. Some non-dispatcher writer is processing them.
3. **Ensemble pre-gate is a second blocker.** Force-eval of patterns
   731 and 1212 both returned `detail_blocked = "ensemble_failed"`
   *before* `finalize_promotion_with_cpcv` could run. In that path,
   `cpcv_eval_to_scan_pattern_fields({})` returns `{}`, so
   `cpcv_n_paths` stays NULL by design. The audit signal conflates
   "never ran" with "ran but ensemble-blocked".

## Surprises / deviations

- **Server-side COPY substitution.** The plan called for psql `\copy`,
  which is a single-line meta-command and doesn't tolerate the
  multi-line query I needed. Switched to server-side
  `COPY (...) TO '/tmp/audit_cpcv.csv'` — psql/postgres both run in
  the same container, so the file write succeeds. Functionally
  identical; flagged here for transparency.
- **PYTHONPATH on `docker exec` for D2.** `docker exec ... python
  /tmp/file.py` doesn't add cwd to `sys.path` (only the script's
  directory). Fixed with `-e PYTHONPATH=/app -w /app`. Not in the
  plan because I didn't anticipate it.
- **Second force-eval run on pattern 1212.** Plan defaulted to 731.
  After the first run surfaced `ensemble_failed`, I ran the same
  script against 1212 (largest-trade-count entry in
  `event_done_but_no_handler_log`) to confirm the ensemble-failed
  finding wasn't 731-specific. Same result. The second output file
  is included; no plan deviation since the script is parameterized
  exactly for this.
- **Plan said this was Phase 0 of "f-adaptive-promotion-architecture".**
  The audit findings narrow the next step: Phase 1 backfill is the
  RIGHT shape, but it cannot land before the dispatcher-silence
  finding is investigated. I've flagged that as an "open question"
  rather than a "blocker on Phase 1 brief" because the brief is
  Cowork's to write; my recommendation is in D4 §"Recommendation
  for Phase 1".

## Deferred

- **Identifying the "done"-writer.** Phase 0 brief said "no env
  edits, no app/ changes" so I did not insert diagnostic logging.
  Phase 1 design needs this answer; I left a clear note in D4.
- **Statistical scan of which patterns ensemble-fail.** Force-eval
  is one pattern at a time; doing it for all 275 would be slow and
  duplicates work Phase 1 will do anyway (the backfill itself
  surfaces the verdict).
- **Container log retention investigation.** Brain-worker has 4h of
  logs available (uptime). The 24h log grep window over-counts the
  "no handler log" bucket *for events created before the restart* —
  but the **full-history** grep returning 0 makes that caveat moot.

## Open questions for Cowork

1. **Dispatcher silence — bug or expected?** If
   `run_brain_work_dispatch_round` is intentionally muted (log level,
   flag), the audit needs to re-classify `event_done_but_no_handler_log`
   as "expected" and Phase 1 design can proceed. If it's a regression,
   it predates Phase 1 backfill and needs its own brief.
2. **Phase 1 backfill: enqueue first, or fix dispatcher first?** If
   the dispatcher genuinely isn't draining `backtest_completed`,
   enqueuing 275 more events just grows the unprocessed pile.
   Recommend: dispatcher restoration as Phase 1a (pre-flight), then
   backfill as Phase 1b.
3. **`ensemble_failed` visibility.** Should Phase 2's adaptive-gate
   redesign surface ensemble-failed patterns as a distinct state
   (not rolled into "blocked")? Right now they look identical to
   "never evaluated" in `scan_patterns.cpcv_*` columns, which would
   distort any adaptive-percentile redesign.
