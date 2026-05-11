# f-cpcv-gate-coverage-audit (Phase 0 of adaptive-promotion-architecture)

> **Type:** Read-only audit + diagnostic memo (NO code changes)
> **Parent brief:** `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`
> **Goal:** Prove or disprove the hypothesis that the CPCV gate handler
> (`app/services/trading/brain_work/handlers/cpcv_gate.py`) is failing to
> reach 275 patterns that have ≥ 30 PTR rows but NULL `cpcv_n_paths`.
> **Trust budget:** none of this phase touches running code, migrations,
> autotrader, or pattern lifecycle. It writes one audit script and one memo.

## Why this phase exists

Empirical probes (commit `e70bc5c`) showed:
- 586 active patterns; only 39 have CPCV verdict (`cpcv_n_paths IS NOT NULL`)
- 314 patterns have ≥ 30 PTR rows in `trading_pattern_trades` (the CPCV
  gate's own minimum) — so 275 *should* have a verdict and don't
- The Phase 2 cpcv_gate handler subscribes to `backtest_completed` events,
  and that event type fires 175 times / 24h, so the handler is alive
- Conclusion: events are firing but not for these patterns — *or* the
  handler is short-circuiting silently — *or* both. We need to find which.

Phase 1 (backfill) and Phase 2 (adaptive gate redesign) both depend on this
answer. If the gate is silently rejecting due to a code defect, backfill
just rebuilds the same backlog. If events aren't reaching these patterns,
backfill is the right tool.

## Deliverables

### D1. `scripts/audit-cpcv-gate-coverage.ps1`

Idempotent, read-only PowerShell script that:

1. **Selects the candidate set** — patterns where:
   - `active = true`
   - PTR row count ≥ 30 (joined from `trading_pattern_trades` with
     `outcome_return_pct IS NOT NULL`)
   - `cpcv_n_paths IS NULL` (gate never produced a verdict)
   - `lifecycle_stage NOT IN ('promoted','retired')` (handler's own guard)

2. **For each candidate pattern (cap at 50 to keep audit cheap)**:
   - Most recent `brain_work_events` row where event_type =
     `backtest_completed` AND payload references that pattern id
     (search both `payload->>'scan_pattern_id'` and `payload->'id'`)
   - Whether any `[brain_work:cpcv_gate]` log line in last 24h
     references the event id (via docker logs grep — limit window so
     this completes in <60s)
   - Count of `trading_pattern_trades` rows for the pattern (sanity)
   - `last_backtest_at` from scan_patterns
   - Suggested classification: `event_missing` /
     `event_present_but_no_handler_log` /
     `handler_logged_but_no_persist` / `unknown`

3. **Aggregates** — counts by classification, top 10 examples per class,
   one-line summary.

4. **Writes output** to `scripts/audit-cpcv-gate-coverage-out.txt`
   (committed alongside this brief's report).

### D2. `scripts/audit-cpcv-gate-force-eval.ps1`

Read-only diagnostic that lets the operator pick a single pattern id
(default: 731, the largest-trade-count candidate) and dry-run the gate
handler against it:

1. Loads the pattern's `PatternTradeRow` rows the same way the handler
   does
2. Calls `check_promotion_ready` with `min_trades=30, n_hypotheses_tested=1`
   inside a brain-worker `docker exec python -c` block — but **wraps the
   whole thing in `sess.rollback()` finally** so nothing persists
3. Dumps the returned `(ok, detail)` payload to stdout
4. Reports: would the gate have passed? With what metrics? What were
   the reasons (if blocked)?

This proves the gate **can** evaluate these patterns when reached.
Combined with D1's "event reaches handler?" verdict, we know which lane
of the funnel is broken.

### D3. `docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`

One-page memo with:
- The two scripts' findings as a `WHERE-THE-FUNNEL-BREAKS` summary
- Whether Phase 1 (backfill) is the correct next step, or whether a
  handler-side fix is needed first
- Concrete recommendation for what Phase 1 should enqueue (event type +
  payload shape that the handler will actually process)
- Tables: classification counts; force-eval results for 3 representative
  patterns (one with lots of trades, one near the 30-trade gate floor,
  one with EV-pass)

## Hard constraints

1. **No writes.** Audit scripts use `psql -c` with SELECT-only queries
   and `python -c` blocks that explicitly `sess.rollback()`. The
   `docker exec` calls run as the existing brain-worker user with no
   migration / DDL paths.
2. **No restart of any worker.** Audit must complete with workers in
   their current state (we're observing, not perturbing).
3. **No new tables, columns, or migrations.** Phase 0 is pure
   observability.
4. **No env edits.** Don't touch `.env`. The audit uses what's running.
5. **No changes to `cpcv_gate.py` or any handler.** Read it, don't edit.
6. **Quote the verdict.** Memo must include exact percentage of audited
   patterns in each classification — no hand-wave summaries.

## Success criteria

- D1, D2 committed and runnable
- D3 committed with concrete findings + recommendation
- `scripts/audit-cpcv-gate-coverage-out.txt` committed (the actual run
  output)
- CC_REPORT under `docs/STRATEGY/CC_REPORTS/2026-05-11_cpcv-gate-coverage-audit.md`
- No code changes anywhere outside `scripts/` and `docs/`

## Approved next step after CC_REPORT lands

I (Cowork) will read the audit memo, write a Phase 1 brief
(`f-cpcv-gate-backfill.md`) calibrated to the classification breakdown,
and surface to operator with concrete numbers ("backfilling N events
should move ~M patterns from candidate to backtested"). Operator decides
whether to proceed.
