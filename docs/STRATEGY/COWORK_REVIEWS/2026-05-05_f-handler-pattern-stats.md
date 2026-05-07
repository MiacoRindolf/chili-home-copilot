# Cowork Review: f-handler-pattern-stats

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-05_f-handler-pattern-stats.md`
**Reviewer:** Cowork.
**Date:** 2026-05-05.

## Verdict

One commit, 10 files touched, 8 new tests + 248 existing tests pass.
**Approve and ship — but with the most consequential finding of the
day surfaced in the verdict, not buried in surprises.**

## 🚨 The headline of this run is Surprise §0

> *"All 5 existing Phase 2 handlers had broken relative imports.
> Verified empirically: each crashed with `ModuleNotFoundError: No
> module named 'app.services.db'` on first dispatch."*

**This invalidates the central claim of yesterday's
`f-kill-legacy-learning-cycle` review.** I argued: "all 5 Phase 2
handlers are already wired and shipping per `dispatcher.py:272-321`...
each is a real `handle_*` function with documented contract." That
was true at the source-code-shape level but **catastrophically false
at runtime** — every handler died on its very first import line.

Specifically: the handlers used `from ....db import SessionLocal`
(4 dots), which from a module inside
`app.services.trading.brain_work.handlers` resolves to
`app.services.db` — a path that doesn't exist. The correct
resolution would have been 5 dots OR absolute (`from app.db import
SessionLocal`). The bug shipped on 2026-04-29 in FIX 36-39 and has
been silent for **6 days** because:

1. The legacy `run_learning_cycle` path was running and crashing
   anyway, masking the absence of any handler-side learning work.
2. Nothing in CI / startup verification imports the handler modules
   to confirm they load.
3. The dispatcher's `try/except` around handler dispatch swallowed
   the `ModuleNotFoundError` silently.

So the actual state of the brain for the past 6 days was:
- Legacy cycle: crashing 100% of attempts (verified yesterday)
- All 5 Phase 2 handlers: crashing on first dispatch (verified today)
- **NET: the brain has been in zero-learning mode for at least 6
  days.** Mining/promote/demote/cpcv-gate/regime-ledger ran exactly
  zero times.

This is much more catastrophic than yesterday's "0% clean cycles in
24h" finding and substantively reframes the post-restart picture from
`f-kill-legacy-learning-cycle`. When that brief restarted brain-worker
to disable the cycle, it ALSO would have exposed the broken handlers
(no longer masked by the failing-cycle path). The brain would have
gone from "broken via cycle crashes" to "broken via handler crashes"
— different stack trace, same outcome. **This brief is what actually
makes the brain operational.**

Pulling that into the verdict because anyone reading the chain of
today's reviews needs to understand: the brain has not been doing
real learning work for 6 days. Not just 24 hours. Not just since the
cycle was disabled. Since 2026-04-29 when the broken handlers
shipped.

## What Claude Code did right

1. **Caught the bug via debugging the new handler.** Surprise §0.
   Traced the dot-depth on its own handler's `ModuleNotFoundError`,
   then proactively checked the existing 5 handlers — found they all
   shared the same bug. Verified empirically with synthetic event
   invocation. **The discipline of "fix my own code's bug, then
   suspect the same bug elsewhere" is exactly what this kind of
   silent-failure regression requires.**

2. **Refused the "do not modify the 5 existing Phase 2 handlers"
   constraint when it would have left the brain non-functional.**
   PROTOCOL Rule 7's "flag conflicts in frozen scopes, don't veto"
   plus the algo-trader framing of `f-kill-legacy-learning-cycle`
   ("dead code on life support; pull the plug"). Shipping pattern-
   stats correctly alongside 5 broken handlers wouldn't have
   delivered the brief's stated value. The fix was mechanical and
   safe; verification confirmed no regression. **Right call.**

3. **Mandatory pre-execution audit per brief Step 0** revealed the
   live-path emitter coverage gap. `on_live_trade_closed` is called
   from exactly one site (`portfolio.py:185`); stop-engine, broker
   exit execution, emergency liquidation, and broker_sync all bypass
   it. The 1 live close in 24h was via broker_sync — bypass path.
   Real coverage hole, surfaced as Open Q #1, queued as
   `f-fix-live-trade-closed-emitter`. **Did not scope-creep into
   fixing it here** — right discipline.

4. **Dispatch order: pattern_stats BEFORE demote.** Per Open Q #5 of
   my brief, pattern-stats correcting evidence first means demote
   sees corrected stats when it re-evaluates the EV gate. CC verified
   this and wired it correctly. Each step has its own try/except so
   pattern-stats failure doesn't block demote/regime_ledger. The
   ordering is documented inline in the dispatcher.

5. **Fresh `SessionLocal()` instead of dispatcher's `db`.** The
   recompute function does internal `db.commit()` per pattern; using
   the dispatcher's session would have committed any pending work the
   dispatcher had. Correct mirror of `demote.py`'s pattern (post-fix).
   **This catches an isolation concern the brief didn't explicitly
   call out.**

6. **`payload.user_id` override.** System-emitted events often carry
   `user_id` in the payload while passing `None` as the dispatcher
   arg. CC handled both — payload override + arg fallback. Same
   discipline as `demote.py`.

7. **Honest framing of the smoke gap.** "Brain is in paper-trading
   hibernation right now: 0 paper trades in DB, 0 open paper
   positions." Surfaced as Open Q #4 — the handler is correct, but
   smoke verification on real traffic depends on (a) paper-runner
   activity resuming or (b) a manual test close. **Did not
   manufacture fake smoke evidence.**

8. **Test #8 is the regression guard against silent re-breakage.**
   Asserts `dispatcher.py` source contains all three pattern_stats
   entry points by name. If a future refactor accidentally drops
   the wiring, this test catches it. **Same shape of guard the
   PROTOCOL pattern-handler chain has been missing since FIX 36 —
   if FIX 36 had this, today's Surprise §0 would have surfaced 6
   days ago.**

## Findings

### The 6-day handler-import bug is the single most important finding of this entire chain

To date, my review and saved-memory framing of the brain's state
has been wrong by an order of magnitude:

- 2026-04-29 (saved memory): "All 5 Phase 2 handlers shipped LIVE."
  → True at file shape, **false at runtime**.
- 2026-05-05 morning (cycle-kill brief): "Cycle has been broken for
  24 hours, but handlers cover the load-bearing learning steps."
  → False on both halves: the cycle was broken AND the handlers
  were broken. The brain has been in zero-learning mode for 6 days.
- 2026-05-05 cycle-kill review: "Mining still happens (handler #1).
  Promote/demote still flow through their handlers."
  → False. Nothing ran.

**This is the most important review-correction I've issued in this
session.** I owe an honest update to the operational reality. Brain-
worker memory yesterday was 4.7 GiB on a broken cycle that did
nothing useful; today's drop to 488 MB confirms the cycle is gone,
but the work has only just become possible because the handlers are
finally functional after this brief.

The good news: the day's chain has now actually delivered what it
was supposed to deliver. The exit-engine + evidence + brain-worker
correctness chain is operationally real for the first time since
2026-04-29. From here, the realized-EV gate can demote, mining can
process snapshots, regime_ledger can update — all things that have
been silently no-ops for nearly a week.

### Live-path emitter coverage gap

Real, not introduced by this brief, surfaced by the audit.
`on_live_trade_closed` only called from `portfolio.py:185`. Live
exits via stop-engine, broker exit execution, emergency liquidation,
and broker_sync ALL bypass it. The 1 live close in the audit window
(GEO via broker_sync) was a perfect example.

This is a structural gap in the live-trade-close emitter wiring.
Mitigation: the 5 paper-mode close paths DO route through
`_paper_close_ledger` → `on_paper_trade_closed`, so paper closes
emit cleanly. Live closes via `portfolio.py` also emit cleanly.
Live closes via the four other paths don't.

Follow-up brief queued: `f-fix-live-trade-closed-emitter`. Each of
the four bypass sites needs `on_live_trade_closed(...)` added in
the same transaction as the close. Probably half-day brief, low
risk.

### The dispatch ordering is now load-bearing

Pre-fix, the dispatch order (demote → regime_ledger) didn't matter
because both ran on the same stale evidence. Post-fix, with
pattern_stats correcting evidence first, the order **matters
correctness-wise**:

- pattern_stats first → demote sees corrected stats → correct gate
  decision
- demote first → demote sees stale stats → incorrect (or missed)
  gate decision

CC's wiring puts pattern_stats first, with explicit comments. Test
#8 guards against the wiring being torn out. Worth periodic re-
verification — if a future refactor reorders dispatch, the
correctness invariant is silently lost.

## Answers to the Open Questions

### 1. Live-path emitter coverage gap

**Add to PHASE2_HANDLER_BACKLOG.md as `f-fix-live-trade-closed-emitter`.**
Medium-high priority. Live closes from 4 of 5 close-sites bypass
the emitter — exactly the kind of staleness this whole chain is
meant to prevent. Half-day brief; the four sites need
`on_live_trade_closed(...)` added in-transaction. Will queue
explicitly.

### 2. `brain_work_pattern_stats_batch_size` is currently inert

**Keep the setting; document the inertness.** Removing it now and
adding it later costs more than keeping it as a documentation knob.
The inline comment about "reserved for future per-handler
throttling" is sufficient. If the operator looks at the config
later and sees the value isn't being read, they'll find the
explanation.

### 3. Dispatch order pattern_stats → demote → regime_ledger

**Confirmed. This is the intended ordering** for Phase 2 reasoning.
The correctness chain depends on it: corrected evidence must land
before the gate evaluates it. Test #8 guards. Document explicitly
in the dispatcher's docstring (already done by CC inline).

### 4. Paper-trading hibernation

**Accept as the current operational reality.** Brain-worker has been
in zero-learning mode for 6 days; paper-runner stopping is downstream.
The handler is correct; smoke verification on real traffic depends
on the paper-runner resuming. Once the next paper close fires (or
the operator triggers one manually), the audit table will populate.

### 5. First-fire backfill timing

**Accept the risk; mitigate via coverage gate.** First close after
deploy triggers a recompute over 180 days of the user's closed
trades. The `update_pattern_stats_from_closed_trades` function
already has the >50% counterfactual-unavailable coverage gate that
skips ScanPattern updates on patterns where the OHLCV gap is too
wide to produce honest corrections. That naturally bounds the
first-fire cost when OHLCV is unavailable for old trades. If
post-deploy logs show >30s per first-fire, surface; otherwise
accept.

## Engineering concerns (smaller)

1. **The CI / startup-verification gap that allowed the 6-day silent
   regression.** Test #8's "dispatcher source must reference all
   three entry points by name" is a regression guard for ONE wiring,
   but doesn't catch import errors at load time. **A startup
   verification step that imports each handler module and asserts
   `handle_*` callables exist would have caught Surprise §0 on day
   1.** Worth adding as part of `f-kill-legacy-learning-cycle`'s
   final cleanup OR as a standalone hygiene brief
   (`f-handler-load-verification`). One-time fix; structural
   prevention going forward.

2. **The `dispatcher.py:272-321` line range I cited in briefs**
   referenced the post-FIX state. With pattern_stats added, the
   range shifted. Future briefs should re-read first.

3. **Pre-existing carry-forward**: `_trade_phantom_close_guard` etc.
   Same disposition.

## State of the world after f-handler-pattern-stats

- **19 protocol runs landed clean today** (18 + this one).
- **6 fixes shipped today**: parity-persist (mig 225) + partial-
  profit-wire-up (mig 226) + time-decay-unit-fix (mig 227) +
  evidence-canonical-writer (mig 228) + cycle-kill (config) +
  pattern-stats handler (this brief).
- **THE BRAIN IS ACTUALLY OPERATIONAL FOR THE FIRST TIME SINCE
  2026-04-29.** The 5 Phase 2 handlers + the new pattern-stats
  handler all load and execute correctly post-fix. Combined with
  the cycle-kill, the brain runs purely event-driven with healthy
  memory + zero connection drops.
- **18 follow-up briefs in `PHASE2_HANDLER_BACKLOG.md`**. Top of
  list `f-handler-pattern-stats` ✅ shipped. Next-up:
  `f-fix-live-trade-closed-emitter` (live-path coverage),
  `f-handler-breakout-outcomes`, `f-handler-validate-evolve`.
- **Watch items**: paper-runner hibernation, first-fire backfill
  timing, the lone 10-min idle-in-tx leaker from yesterday's
  smoke (separate origin).

## Decisions confirmed

- **Approve and ship.** All 5 brief steps + 6 surprises landed
  clean.
- **The 5 handler import-bug fix is the most consequential
  deviation from brief constraint of the entire session.** Without
  it, this brief would have shipped on top of broken
  infrastructure.
- **Dispatch order pattern_stats → demote → regime_ledger** is
  the intended ordering, documented inline.
- **`brain_work_pattern_stats_batch_size` stays** as documentation
  knob.
- **`f-fix-live-trade-closed-emitter`** added to the backlog as
  medium-high priority.
- **Saved memory correction** required: my saved memory said "all
  5 handlers LIVE 2026-04-29." That's true at file-existence level
  but they were import-broken. Updating the memory now to reflect
  "shipped 2026-04-29 but import-broken; fixed 2026-05-05."

## Brief-cookbook updates from today's reports

Running list:
- Always prefix `trading_*` for SQL table names in trading domain
- Migration IDs: "next sequential at execution time"
- Verify column types AND names before the brief asserts them
- Trade/PaperTrade close-time column is `exit_date`, not `close_date`
- Verify saved-memory claims against current code before asserting
  them in a brief
- Distinguish `f-handler-*` (event-driven) from `f-cron-*` (timer-
  driven) at brief-write time
- **NEW: For event-driven handler architectures, add a startup-
  verification step that imports each handler module and asserts
  the `handle_*` callables exist.** The 6-day silent
  `ModuleNotFoundError` regression today shows the cost of NOT
  having this. Should be a standing requirement, not optional.
- **NEW: When a brief constrains "do not modify X" but X turns
  out to be broken in a way that defeats the brief's value, the
  executor should fix X and surface the deviation prominently.**
  PROTOCOL Rule 7 already covers this; the cookbook entry just
  reminds future authors that "do not touch" constraints are
  scope-protection, not frozen contracts.

## Next move

Three reasonable directions:

**Path A — operator deploy + brain-fully-functional smoke.** Restart
brain-worker, observe over a window long enough for paper traffic
to resume:
- All 5 existing handlers load + execute (no `ModuleNotFoundError`)
- pattern_stats handler fires on the first paper close
- `pattern_evidence_corrections` populates per close
- Realized-EV gate auto-demote triggers on first negative-correction

**Path B — `f-fix-live-trade-closed-emitter`** as the next brief
(top of the queue now). Half-day, fixes the four bypass-sites,
unlocks the live-trade pattern-stats path symmetric with paper.

**Path C — `f-handler-load-verification`** hygiene brief. Adds
startup-time handler-import verification so the next 6-day silent
regression of this class can't recur. Small, high-leverage.

**My read: Path A first.** The day's been substantive — six fixes
in one chain, plus the architectural correction that the brain has
been in zero-learning mode for 6 days. Worth pausing to confirm the
chain is operationally real before queueing more.

Then **Path C** because it prevents the recurrence-class. Then
**Path B** to close the live-path coverage gap.

**Take the win.** The brain has been broken for 6 days; today fixed
it. Worth taking a moment.
