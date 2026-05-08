# COWORK_REVIEW: f-pdt-count-broker-confirmed-only

**Verdict:** Ship it. Trigger the operator-side restart sequence; this
should drop the live PDT count from 14 to ≤ 4 immediately and unblock
stock entries on the next `pattern_breakout_imminent` alert.

## Algo-trader lens

The brief addressed a real economic loss: every hour the operator's
account stays self-locked, real `pattern_exit_now` AB/COF-class signals
get rejected at the autotrader funnel with `pdt_limit_reached`. The
fix is the smallest possible scope that closes the symptom (single
function, ~37 line diff, 10 helper-level tests). Real broker-confirmed
day-trades still count exactly the way they did before — the SEC
threshold (3-in-5 for sub-$25k accounts) is unchanged; the only change
is that reconcile artifacts are no longer mis-counted as day-trades.

Unblocking this for stock entries is a prerequisite for any further
strategy work on the equity book. Until the count goes down, no equity
pattern — however high its win rate — gets a chance to trade.

## Dev-architect lens

CC's three notable choices, in order of how much I care:

1. **`expanding=True` bindparam over inlined string list.** Brief
   spec'd literal strings; CC parameterized via
   `text(sql).bindparams(bindparam("reconcile_reasons", expanding=True))`.
   This is the right call. Future additions to
   `_RECONCILE_ARTIFACT_EXIT_REASONS` (e.g., when Phase B surfaces a
   third reconcile-artifact exit_reason) auto-propagate without an
   SQL edit — same lesson as separating data from query structure.
   Empty-frozenset cliff is theoretical; the constant is hardcoded
   to two elements and any change goes through a brief.

2. **Helper-bug caught by test run, not AST.** The `_seed_trade`
   default-fill-at-equals-exit-at logic shadowed the explicit `None`
   override; first test pass was 8/10. Fixed via sentinel pattern
   (`_UNSET = object()`). This is the second instance in two days
   of the same lesson — yesterday's `f-fastpath-rotator-http-retry`
   had `_time.sleep` mock-collision; today the sentinel-vs-default.
   Adding to memory: AST passes are necessary but not sufficient;
   the test run is the load-bearing verification gate. Splice
   discipline catches truncation; only running the suite catches
   value-shadowing.

3. **`last_fill_at` over `filled_at`.** CC took Q2 from the brief
   verbatim. Right call — `last_fill_at` is the broker-truth column
   that updates on every fill event; `filled_at` is the older
   entry-side timestamp set on non-fill paths. The audit confirmed
   both NULL on phantom rows so either filter would have worked, but
   the broker-truth column is the more durable choice.

## What surprised me

Nothing. CC followed the brief tightly; the only deviations were
documented (expanding bindparam) or small-quality-of-life choices
(sentinel pattern in tests). No scope creep, no edits outside
`pdt_guard.py` + the new test file.

## What's left

1. **Operator deploys the change.** `docker compose up -d
   --force-recreate chili autotrader-worker` reloads `pdt_guard.py`.
   The verification one-liner from the brief surfaces the live
   count post-deploy.

2. **Phase B follow-up brief queued.** I'm writing
   `f-equity-broker-reconcile-wipeout-protection` now. The phantom
   rows are the symptom; the equity reconciler's
   wipeout-on-empty-`get_positions()` is the cause. R31/R32
   (commits `539e1c2` + `7af3d49`, 2026-04-30) closed this for the
   crypto book; the equity book needs the parallel fix or this
   class of phantom row will accrete again over time.

3. **Two related briefs stay queued for later:**
   `f-pdt-crypto-bypass-cleanup` (hygiene; explicit asset_kind +
   equity-tier policy) and `f-autotrader-pdt-aware-exit-deferral`
   (structural fix for real autotrader day-trades, not the current
   blocker). Operator picks ordering.

## What CC should do if Phase B is next

If the operator promotes Phase B as the next NEXT_TASK, the brief
will spell out the R31/R32 pattern verbatim with the equity reconciler
call sites identified up front. CC should NOT touch `pdt_guard.py`
in Phase B — this brief's exclusion filter stays the durable defence
even after the wipeout protection ships.

## Final note on the operator pushback

The operator was right to push back on my earlier "autotrader
rapid-fire round-trips" diagnosis. The phantom rows had three
independent signals (no `broker_order_id`, no `last_fill_at`, all
exit at the exact same second) that I should have caught on the
first audit pass. Lesson: when the data shape contradicts the
narrative, the data wins — pull the row detail before naming the
cause. Saved to memory as a reference for future audits.
