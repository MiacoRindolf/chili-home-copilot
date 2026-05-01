# FIX 31 deletion runbook

**Scope:** Delete the legacy `run_learning_cycle` reconcile pass and the
"skip vs run" gate in `scripts/brain_worker.py:_run_lean_cycle_loop`.

**Status:** Phase 2 handlers (mine, cpcv_gate, promote, demote, regime_ledger)
are written and dispatched. The gate (`_should_skip_reconcile_pass`) is the
bridge that lets us run them in shadow alongside `run_learning_cycle` — once
the handlers prove out, this whole block goes away.

## Pre-conditions for deletion

All five must be verified BEFORE the deletion PR ships. If any fails, the
gate stays and we keep iterating on the failing handler.

1. **Handler coverage.** Every step that `run_learning_cycle` performs has
   a counterpart event-handler. Verify by:
   ```
   grep -n "def _step_" app/services/trading/learning.py
   ```
   For each `_step_*` function, confirm there's a handler in
   `app/services/trading/brain_work/handlers/` that fires on the
   appropriate event_type and produces the same row-mutations.

2. **Shadow output parity (≥ 7 days).** Compare:
   - `run_learning_cycle` outcome metrics: tickers_scanned, snapshots_taken,
     patterns_mined, patterns_tested, hypotheses_validated, etc.
   - Handler-driven equivalents from `trading_brain_work_events` log lines.

   The two should agree within 5% over a 7-day rolling window. Any handler
   producing < 95% of the cycle's output for its step is a regression.

3. **Soak with `_should_skip_reconcile_pass = True` always.** Force-set the
   gate to always skip for ≥ 48 hours and confirm:
   - Pattern lifecycle still progresses (challenged → promoted → live)
   - No new "Mined: 0" cycles building up backlog
   - No alerts firing about stale brain state

4. **Performance.** Handler-driven dispatch round must complete in ≤ 1/3 the
   time of `run_learning_cycle` — that's the point. If it's slower or
   competitive, the architecture isn't paying off yet.

5. **Operator sign-off.** Post the soak metrics + handler/cycle parity
   numbers to ops review before deletion lands.

## Deletion sequence

Once pre-conditions pass:

1. Remove `_should_skip_reconcile_pass()` and the `else:` branch that calls
   `run_learning_cycle` from `scripts/brain_worker.py:_run_lean_cycle_loop`.
2. Delete `run_learning_cycle()` definition + every `_step_*` helper from
   `app/services/trading/learning.py` (estimate: 2,000+ lines deletable).
3. Delete the FIX 31 docstring references in handler files (cosmetic).
4. Remove the "FIX 31 endgame" framing from architecture docs — it's
   shipped, not endgame anymore.
5. Tests: a CI guard ensures nothing imports `run_learning_cycle` from
   anywhere else. (If anything still does, that import was the actual
   bridge — needs migration first.)

## Rollback

If the deletion ships and a regression appears in week 2 of soak post-merge,
revert to the FIX 31 gate state by reverting the deletion PR. The handlers
remain — they ran in parallel during shadow, so reverting just re-enables
the legacy fallback. No data migration needed.

## Estimated timeline

- Verification window: 7-14 days observing parity metrics
- Deletion PR + review: 1-2 days
- Merge + monitor: 7 days
- Total: ~3-4 weeks from "now" to "FIX 31 is gone"

This is not a single-session deletion. Tracking as task #37.
