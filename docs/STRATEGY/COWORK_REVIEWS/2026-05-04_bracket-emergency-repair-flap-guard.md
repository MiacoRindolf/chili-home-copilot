# COWORK_REVIEW: bracket-emergency-repair-flap-guard

## Verdict

Functional fix shipped. Closes the recent regression (`ef50d3f` sub-branch 2 single-sweep close). Tests pass 10/10. Live deploy clean.

**However:** the shipped version is the brief's v1 (counter + threshold + env-overridable default). Cowork rewrote the brief to v2 (positive confirmation via `BrokerView.peer_position_count` + `trading_execution_events` SELL-fill cross-check, no thresholds) before Claude Code launched, but the rewrite arrived after CC was already running and CC reads NEXT_TASK only at launch. The shipped commits implement v1.

Cowork's bad call: when the operator asked "should I stop that and run this instead?", Cowork advised "don't stop" under the misread that the in-flight session was the previous-agent emergency_liquidation diagnosis. It wasn't. The in-flight session was about to ship the very magic number the operator had just rejected. Cowork owes the operator a clearer same-session coordination signal next time NEXT_TASK is rewritten while a session is in flight.

## Algo-trader lens

**What's good.** The flap guard works. A single broker_qty=0 sweep no longer auto-closes a trade that's still held at the broker. Three new test scenarios cover the protection. The audit emit (`status='phantom_close_deferred'`) gives funnel-accounting visibility. Counter reset on the broker_qty>0 path is correct (any positive observation clears the streak).

**What's concerning.** N=3 is a magic number. The CC report acknowledges this in Open Question #4 ("seed values; bump N if oscillating, drop if real phantoms take too long to recognise"). The brief's stated principle was zero new literals — the shipped commit ships one. Operator's expectation was positive confirmation from observable system state, not a tunable threshold.

**What's deferred and still load-bearing on live money.** The 11 broker-vs-DB mismatched positions remain unmanaged. The flap guard prevents the *next* phantom-close cascade; it does NOT undo today's. Until the operator reconciles, CHILI's stop engine and bracket writer cannot manage exits for any of those 11. Today's other landmines (emergency_liquidation Bugs 1-4) all remain in the codebase.

## Dev-architect lens

**What's good.** Migration 223 is idempotent, additive, follows the existing convention. Helper `_bump_phantom_close_zero_qty_counter` is a single SQL statement with a safe fallback. The existing `_bump_repair_attempt` was extended in-place to reset the counter — no second query, clean. Tests are scenario-coherent.

**What's concerning.**

1. **Magic number violates PROTOCOL Hard Rule #3.** The literal `"3"` in `EMERGENCY_REPAIR_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS` should have been called back to Cowork per the Hard Rule, not shipped with a defense-in-depth justification. CC noted this implicitly (Open Question #4 frames it as "seed values"); should have explicitly invoked the Hard Rule.

2. **R32 framing was misleading and CC caught it but shipped anyway.** Surprise #1 in the report: "R32 is a single-snapshot empty-positions guard, not a counter." The brief used "R32-mirror" framing that didn't actually map to R32's machinery. Correct CC reading. The shipped fix is a *new* pattern, not an R32 port — should have been named accordingly.

3. **Migration 223 created a column that v2 wouldn't have needed.** If we ship v2 next, mig 223's column orphans (NOT NULL DEFAULT 0, never written by anything). Harmless but accumulates schema dust. Follow-up ticket if v2 lands.

4. **Unsanctioned env-flag activation during deploy.** Surprise #2: `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` went hot without authorization. CC honestly surfaced this; the discipline gap is real. Pre-deploy `printenv` snapshot should be standard practice when `restart` is used after compose-file edits sit between deploys. Add to operator runbook.

## Decisions for Cowork to make

1. **Do we ship v2 (positive confirmation) on top of v1?** Three options:
   - **A. Revert `f917c02`, ship v2 as a single commit. mig 223 column orphans (harmless).** Cleanest history, biggest churn.
   - **B. Land v2 as a follow-up commit that retires the counter logic. Counter becomes dead code, env override becomes inert.** Less disruptive, muddier history.
   - **C. Accept v1, defer v2 indefinitely.** Magic number stays. Functional but against principle.
2. **Is `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` desired state?** It's hot now. If yes, document that the staged decision has graduated to deployed. If no, set it back to 0 and recreate broker-sync-worker.
3. **Priority order for the deferred items?** All five remain:
   - Operator reconciliation of 11 broker-vs-DB mismatches (manual action)
   - Bug 1: `is_disconnected()` weekend gap (fires next Monday if unaddressed)
   - Bug 2: lying `exit_price = entry_price` (already corrupted 6 trades' P/L)
   - Bug 3: redundant `activate_kill_switch` arming (cosmetic + log noise)
   - Bug 4: `emergency_close_all` silent shadow (largest blast radius — fix Bug 1 still leaves drawdown-trigger unprotected)

## Recommended next move

The biggest live-money risk right now is **operator reconciliation** — 11 unmanaged positions. The flap guard prevents new phantom closures; it doesn't undo old ones. Until reconciliation runs, CHILI is blind to those positions and the autotrader is kill-switched anyway.

After reconciliation, **Bug 4** (emergency_close_all silent shadow) is the highest-value next ticket. Bug 1 fix without Bug 4 fix means: next Monday's weekend-gap trigger gets gated → BUT a real drawdown-trigger could still fire emergency_close_all and create another wave of DB-only-closures with positions still held at broker. Bug 4 closes that path.

The v1→v2 cleanup (option A or B above) can wait until those two are resolved. It's hygiene; it's not bleeding.

## Status of NEXT_TASK.md

CC marked v2 of the brief as DONE — but CC actually shipped v1. The DONE marker is honest about the slug ("bracket-emergency-repair-flap-guard") but the brief content is v2. Operator should either re-stage v2 (if pursuing option A/B above) or move on to the next ticket. Don't leave the file in this mismatched state long.
