# Cowork Review: f-overnight-cleanup

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-05_f-overnight-cleanup.md`
**Reviewer:** Cowork.
**Date:** 2026-05-05.

## Verdict

**5 SHIPPED / 1 BLOCKED.** Target was 5/6, acceptable floor 3/6 —
target met cleanly. Approve all five shipped phases. The one blocked
phase (Phase 2 — momentum-viability-tx-leak) blocked correctly per
the stop-on-blocker policy: the targeted leak isn't currently
reproducing, so a fix without reproduction would have been a guess.

The five shipped phases delivered more clarification value than
mechanical-fix value — three of them (Phase 4, Phase 5, Phase 6)
discovered the brief's framing was off in a useful way, and the
shipped commits reflect the corrected reality. **That's exactly the
algo-trader-architect outcome you asked for.**

## What Claude Code did right

1. **Phase 3 reframed the entire paper-trading-output question.**
   The brief assumed "paper-runner ticks → trading_paper_trades rows"
   was a wiring break. CC's audit found it's a **misnomer**: two
   independent paper-trading systems exist. The "momentum paper
   runner" (the thing that emits the `Momentum paper runner: ticked
   N session(s)` log lines) writes to
   `trading_automation_sessions` / `trading_automation_events`, not
   `trading_paper_trades`. The actual `trading_paper_trades` writer
   is `auto_trader.py`'s BreakoutAlert path, called via
   `paper_trading.open_paper_trade()`. **One PaperTrade constructor
   in the entire repo.** Verified by grep.

   And the auto_trader IS firing — 700 AutoTraderRun rows in 24h.
   But every decision is silent-blocking on
   `broker:Robinhood crypto endpoint returned no order_id` and never
   falls through to paper mode. **That's a real safety mechanism, not
   a bug** — but it explains why paper trades have never appeared.
   The fix path is an explicit operator decision: flip
   `chili_autotrader_live_enabled=false` (one-line) OR write
   `f-fix-autotrader-paper-fallback` (code change for fall-through
   on broker-error).

   Today's most important Cowork-side correction: **the brain isn't
   "broken" or "not producing trades" — it's correctly silent-failing
   because the upstream Robinhood crypto endpoint isn't responding,
   and the autotrader's safety policy is "don't shadow-trade in paper
   when live broker errored."** That's a defensible architecture
   choice. Whether to keep it is a Cowork-side decision.

2. **Phase 4 caught broker_sync was already wired.** The brief listed
   4 bypass sites; CC found the 4th (broker_sync) was already wired
   via `on_broker_reconciled_close` which emits `broker_fill_closed`
   (also in the close-event branch of dispatcher). So the gap was
   3 of 4, not 4 of 4. Three sites patched cleanly:
   `stop_engine.py:1057`, `robinhood_exit_execution.py:425`,
   `emergency_liquidation.py`. Each call wrapped in try/except per
   the brief's safety contract.

3. **Phase 6 confirmed the kill code IS implemented.** The brief
   assumed db_watchdog warns-but-doesn't-kill. CC traced the source
   and found the kill IS implemented at `db_watchdog.py:127` via
   `pg_terminate_backend()` wrapped in try/except. The "held times
   grow past 600s without kill" pattern we observed yesterday is
   actually **explained by FIX 32's chili-brain-worker /
   chili-backtest-child exemption** (1800s threshold instead of 600s).
   The audited leakers were brain-worker — under the 1800s exemption
   — which is the intended behaviour from FIX 32.

   But CC didn't stop at "confirmed working." Added a logging
   enhancement: read the `pg_terminate_backend` return value and
   emit one of three lines: `KILLED` / `KILL-FAILED` (returned FALSE
   — permission or pid-already-gone) / `KILL-EXCEPTION` (driver
   error). Future "why didn't the kill fire?" diagnostics now
   unambiguous. **Best kind of "confirm not broken" outcome — adds
   observability so the next investigation is shorter.**

4. **Phase 6 surfaced a follow-up observation as Open Q #5**: the
   1800s brain-worker exemption was set by FIX 32 to allow the legacy
   reconcile cycle to hold its session. **Now that the cycle is
   gated off (f-kill-legacy-learning-cycle), the 1800s exemption may
   no longer be justified.** Recommended `f-tighten-db-watchdog-brain-worker-exemption`
   as a follow-up brief. Right discipline — surfaced the
   architectural shift's downstream implication without acting on it
   without authorization.

5. **Phase 2 stopped on blocker correctly.** No code change shipped
   because the targeted leak wasn't reproducing. CC traced the
   readers (`automation_query.py`, `paper_runner.py`, etc.) and
   confirmed they all use caller-managed sessions correctly. Without
   reproduction, a "fix" would have been a guess. **This is what
   stop-on-blocker policy is for.** The current observable leak is
   `scan_patterns` from chili-brain-worker (802s) — different code
   path, a separate brief if it persists.

6. **Phase 4 made a pragmatic test-scoping call.** Brief asked for
   6 end-to-end emit-verification tests; CC noted each end-to-end
   handler-touching test takes 7-10 min in this repo's pytest setup
   (per-test truncate cycle on a large schema). For 9 tests that
   would've been 60-90 min of CI time for the same regression-
   coverage value the wiring-pin approach delivers in <1s. Documented
   in the commit message. Surfaced for explicit Cowork review of the
   precedent.

7. **Phase 1's verification function is now load-bearing.** With
   Phase 4 + Phase 5 wiring more handlers / emitters across the
   repo, the "5 of 6 handlers silently broken for 6 days" failure
   mode from `f-handler-pattern-stats` would be even more visible
   if it recurred. The new `_verify_handler_modules()` is the
   regression guard, called at startup before any work loop.

8. **Phase 5's emit dedup is the right discipline.** New
   `emit_backtest_completed_outcome` includes dedup per
   `(pattern_id, minute bucket)` so rapid queue churn doesn't flood
   the ledger. cpcv_gate is run-level idempotent, so dedup is correct
   at the emit boundary, not the handler.

9. **Cross-phase observation #4 is the one I'd flag as the most
   important Cowork-side discipline update.** "Two queries against
   `pg_stat_activity` made all 6 phases tractable. Without that
   direct production read, Phase 2 would have been a guess, Phase 3
   would have been wrong, Phase 6 would have been speculative.
   **Future cleanup briefs should include 'audit production state
   before coding' as a default Phase 0.**" Adding this to the
   cookbook.

## Findings

### Three of six phases reframed the brief's premise

| Phase | Brief premise | Reality |
|---|---|---|
| 3 | Paper-runner output gap is a wiring break | Two independent systems; auto_trader silent-blocks on broker error |
| 4 | 4 bypass sites need patching | 3 real, 4th (broker_sync) already wired |
| 6 | Kill code missing or broken | Implemented + intentional 1800s brain-worker exemption |

This is exactly what an algo-trader architect outcome looks like —
**the value isn't in the LOC shipped, it's in the corrected mental
model.** Brief authors (me) operate on yesterday's evidence; CC
operates on today's reality. When they diverge, CC's reality wins
and the brief's framing gets corrected in the report.

### The auto_trader silent-block is the actual paper-trading-zero-rows root cause

Phase 3's audit document (`docs/AUDITS/2026-05-05_paper-runner-output-gap.md`)
is the load-bearing artifact from this run. The chain:

1. AutoTraderRun fires 700 times in 24h (entry-side healthy)
2. Most decisions = `skipped` or `blocked` (per yesterday's deep
   diagnostic Section B6)
3. The `placed` decisions go to live broker first
4. Robinhood crypto endpoint silently returns no order_id
5. `auto_trader.py` errors out without falling through to paper
6. `trading_paper_trades` stays at 0 rows

The chain is logically consistent. It's also a **defensible safety
mechanism** — the architecture is saying "if live broker errored,
don't pretend by shadow-trading in paper." But it has a real
operational cost: **the brain has no realized-trade evidence to
learn from**. The realized-EV gate has nothing to gate. The
canonical-aware writer (mig 228) has no closes to recompute.

This is a Cowork-side decision, not a code-side fix. Two options:
- **(a) Flip `chili_autotrader_live_enabled=false`**: paper mode
  becomes the default, paper trades start populating, all the
  Phase 2 handler chain we built today starts firing on real
  traffic. **Lowest-effort highest-leverage move.**
- **(b) Authorize `f-fix-autotrader-paper-fallback`**: code change
  that adds "fall through to paper on broker failure" — keeps
  live mode as default but no longer silent-blocks when broker
  errors.

I'd recommend (a) for now. It's a one-flag operator decision, fully
reversible. And it makes paper-mode the **canary surface** — when
real paper traffic flows, every handler we built today
(pattern_stats, demote, regime_ledger, cpcv_gate, mine) gets
exercised end-to-end with real data, not synthetic test data.

### Phase 6's brain-worker exemption is now misaligned

`f-tighten-db-watchdog-brain-worker-exemption` is the follow-up:
the 1800s threshold was justified when the brain-worker held
sessions during 60-140 minute legacy cycles. The cycle is gated
off now (f-kill-legacy-learning-cycle, today). Brain-worker no
longer holds sessions for that long — so the exemption that
allowed it to is unused at best, dangerous at worst. Tighten back
to the standard 600s. Would be a 2-line change.

### The cross-phase test-scoping precedent

Phase 4's wiring-pin tests instead of full end-to-end is a real
trade-off. **For this repo's per-test setup cost (7-10 min), the
wiring-pin approach is correct.** But it does mean the regression
coverage is "did we delete the call site" rather than "does the
emitted event actually reach the handler." If we ever change the
dispatcher routing, the wiring-pin tests catch nothing.

Acceptable as a precedent for now. When the test-fixture cost goes
down (e.g., a cheaper fixture pattern is found), retro-add
end-to-end coverage.

## Answers to the Open Questions

### 1. Phase 2 — periodic pg_stat_activity snapshots

**Yes, do it.** Cheap insurance. Add a brain-worker job that runs
every 5 min and dumps idle-in-tx sessions to a log file. When the
next leak surfaces, we have a forensic trail. Small follow-up brief
`f-add-pg-stat-snapshot-logger`. Lower priority than the auto_trader
decision.

### 2. Phase 3 — auto_trader paper-fallback decision

**Cowork's recommendation: Path (a) — flip
`chili_autotrader_live_enabled=false`.** Reasoning: makes paper-mode
the canary, exercises the entire Phase 2 handler chain on real
traffic, fully reversible (one flag flip back). Path (b) is a
defensible code change but more invasive; defer until Path (a)
proves out.

**Surface for explicit operator authorization.** This isn't a
Cowork-only decision; the operator should approve the live → paper
default flip explicitly. If approved, the operator flips the env
var and restarts; brain-worker starts seeing real paper closes
within minutes.

### 3. Phase 4 — wiring-pin tests vs end-to-end

**Acceptable precedent for now.** The 60-90 min CI cost vs <1s for
the same regression class is the right call. Retro-add end-to-end
coverage when the per-test fixture cost goes down. **Do not block
this brief on it.**

### 4. Phase 5 — prescreen path emits

**Skip prescreen emit per CC's call.** cpcv_gate's CPCV requires
more trades than 4-ticker prescreen produces. Prescreen-quality
patterns shouldn't reach cpcv_gate; that's the intended gate. If
the prescreen path needs its own gate, it's a separate evaluation
question, not an emit-coverage question.

### 5. Phase 6 — tighten brain-worker db_watchdog exemption

**Yes, queue `f-tighten-db-watchdog-brain-worker-exemption` as a
follow-up brief.** The 1800s exemption was justified when the
legacy cycle held sessions; cycle is gone, exemption is misaligned.
Tighten back to 600s. Two-line change. Low priority but worth
doing.

## Engineering concerns (smaller)

1. **Phase 1's verification doesn't currently run when modules are
   imported in other ways.** It runs only at brain-worker startup.
   If chili web container ever imports a handler indirectly and the
   import is broken, the web container would crash on the import
   path without firing the verifier. Probably fine — the handlers
   are brain-worker-side — but worth noting.

2. **Phase 5's emit dedup at minute granularity** could occasionally
   suppress a legitimate second backtest of the same pattern within
   one minute. cpcv_gate is idempotent so it doesn't matter for
   correctness, but if we ever care about per-emit observability,
   the dedup window should be tighter.

3. **Pre-existing carry-forward**: `_trade_phantom_close_guard`
   listener still unstaged, etc. Same disposition as prior CC
   reports.

## State of the world after f-overnight-cleanup

- **20 protocol runs landed clean today** (19 + this one). Substantial.
- **5 fixes shipped** in this run (4 actual code changes + 1 audit
  doc). Plus 1 honest blocker.
- **The brain stack today went from "fundamentally broken for 6+
  days" to "structurally correct, ready for real paper-mode
  traffic"** — pending the operator's decision on the auto_trader
  silent-block.
- **PHASE2_HANDLER_BACKLOG.md** updated:
  - `f-handler-pattern-stats`: ✅ SHIPPED (yesterday)
  - Live-trade-closed emitter coverage: 3/4 patched (Phase 4)
  - New entry: `f-fix-autotrader-paper-fallback` (Phase 3 finding)
- **Watch items**:
  - The `scan_patterns` 802s leak in brain-worker (different
    leaker than yesterday's; surface if persists)
  - Operator decision on auto_trader silent-block flip
  - Watch for `KILL-FAILED` logs in db_watchdog (Phase 6
    enhancement)

## Decisions confirmed

- **Approve all 5 shipped phases.** Phase 2 stopped correctly per
  policy.
- **Cookbook update** (running list now includes):
  - "Audit production state before coding" as a default Phase 0
    for cleanup briefs
  - "Verify saved-memory claims against current code" (from prior
    reviews)
  - "ORM column verification before brief asserts them" (from prior
    reviews)
  - "Migration IDs: next sequential at execution time" (from prior
    reviews)
  - "Distinguish `f-handler-*` from `f-cron-*` at brief-write
    time" (from prior reviews)
  - "For event-driven handler architectures, add startup-
    verification" — **now actually shipped via Phase 1**
  - **NEW: When constraint says 'do not modify X' but X is broken,
    fix and surface deviation prominently** — also from prior
    reviews

## Brief-cookbook updates from today

The single most important new cookbook entry from this run:

**Cleanup briefs MUST include a Phase 0 production-state audit.**
Three of six phases in this run reframed the brief's premise based
on production-state queries. Without those queries, the brief would
have shipped wrong fixes. Going forward, any cleanup brief that
diagnoses from logs / saved memory / yesterday's evidence should
have an explicit "verify against current production state" step
before the implementation phases.

## Next move

Three reasonable directions:

**Path A — Operator decision on auto_trader silent-block flip.**
Highest-leverage single action. Flip
`chili_autotrader_live_enabled=false`, restart brain-worker, watch
for the first paper-trade INSERT into `trading_paper_trades` within
minutes. Once paper trades start flowing, every handler we built
today gets exercised on real traffic. This is the operationally-
real moment for the entire week's chain.

**Path B — Re-deploy and watch.** Operator deploys the 5 commits
via brain-worker restart. Phase 1's `_verify_handler_modules()`
either logs `[handler_verify] OK 6/6 ...` (clean) or `SystemExit`
with a clear failure list. If clean, the deploy is silent and
successful.

**Path C — Queue follow-up briefs.** From today's findings:
- `f-fix-autotrader-paper-fallback` (Phase 3, Path B alternative)
- `f-add-pg-stat-snapshot-logger` (Phase 2 observability)
- `f-tighten-db-watchdog-brain-worker-exemption` (Phase 6 follow-up)

**My read: Path B then Path A.** Re-deploy first (catches any
deploy-time regression via Phase 1's verification), then make the
auto_trader decision. Path C briefs queue naturally after.

**Take the win, again.** This week:
- Five fixes Tuesday morning (parity-persist / partial-profit /
  time-decay / canonical-writer / cycle-kill)
- Pattern-stats handler + 5-handler-import bug-fix Tuesday afternoon
- Six-phase overnight cleanup tonight (5 SHIPPED, 1 BLOCKED)

The stack went from "the brain has been silently broken for 6
days" to "the brain is operationally ready for real paper-mode
traffic, pending one operator flag flip." That's substantive
progress.

Review filed. CC report and audit doc both linked from PHASE2 backlog.
