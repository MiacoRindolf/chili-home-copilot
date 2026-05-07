# COWORK_REVIEW: f-fix-implausible-quote-vs-exit_now-ordering

## Verdict

Highest-quality task in the recent stretch. The Phase 0 audit found a hidden parallel of the bug in the options lane that the brief author had pre-cleared as out of scope. CC correctly invoked the brief's authorization to expand ("if either equity or options actually does have the same shape, expand Phase 1's fix to cover them too") and shipped both fixes in one commit instead of leaving the options vulnerability to wait for a separate brief.

The deviation from the brief's design choice (crypto kept the prefix-match per spec; options needed a return-type widen) is justified, scoped, and surfaced honestly — exactly the kind of judgment call that compounds into trust. 11 + 8 tests passing. Case 5 xfail removed; passes naturally. The bug-fix case is no longer parked as visible debt.

## Algo-trader lens

**What's good.** The crypto fix matches the brief: prefix-match `no_trigger:implausible_quote` gates the LLM advisory consultation. Surgical (~5 lines), reversible, no contract change. The lane abstains rather than picking between two contradictory inputs — the no-hardcoded-fallback principle in action.

The options expansion is the real win. CC's surprise narrative spells it out clearly: options' `bid<=0 AND mark<=0 → continue` short-circuit ONLY fires when the quote is FULLY missing. When the quote has bid > 0 AND mark > 0 but is implausible (e.g., bid=$0.001 for a $0.50 entry), `_evaluate_exit_triggers` returned `None`, the call site treated `None` as "no trigger fired," and a fresh `pattern_exit_now` advisory dragged the lane into selling from the implausible quote — same identical exposure to crypto, just hidden inside a different return shape. Without the audit, this would have stayed unfixed indefinitely because the brief author's pre-clearance said options was protected.

The Case 5b regression test (`test_case5b_no_trigger_plus_fresh_exit_now_still_closes`) is the right discriminator — proves the new gate doesn't extend reflexively to ordinary `no_trigger` (the LLM is still consulted as the secondary signal when the lane simply finds no actionable trigger; it's only blocked when the lane DOESN'T TRUST the price).

**What's narrow.** The equity vulnerability remains unfixed. CC correctly scoped it as a different bug class (no guard at all vs ordering) and surfaced as `f-equity-lane-implausible-quote-guard`. But that means a bogus equity quote like $0.50 for a $50 stock would still force a stop-loss sell at the bad price today — pre-existing, not regressed by this work, but real. Worth queuing the follow-up while quote-guard context is hot.

The implausibility threshold (0.1x to 10x ratio per CC's Open Q #2) is hardcoded inside both `crypto/exit_monitor.py::_evaluate_exit_triggers` and `options/exit_monitor.py::_evaluate_exit_triggers`. Two parallel magic numbers right now. If/when equity gets the same guard, three lanes will carry the same threshold. Cowork-side tracking item.

## Dev-architect lens

**What's good.** Single commit covering both lanes + tests + the xfail removal. Crypto's prefix-match approach kept blast-radius minimal per the brief. Options' return-type widen was the right call given `Optional[str]` collapses two semantically-different states. CC justified the deviation explicitly: the brief's Out-of-Scope said "don't pre-factor unless a future brief needs the same contract in another lane" — that future arrived in this brief during Phase 0. Single-caller refactor, type-safe.

The new `_evaluate_exit_triggers_implausible_quote_prefix` test pins the prefix-match contract Phase 1 relies on. If anyone ever changes the reason string from `no_trigger:implausible_quote ...` to something else, the test catches it before the gate silently breaks. Right discipline.

The `test_case4_native_dte_trigger_wins` source-text guard update is honest: tightening the gate from `if not reason:` to `if not reason and not abstained_implausible:` broke a literal-string match in a guard test; CC updated the assertion to accept either form rather than dropping it. The semantic invariant (native triggers win on tie) is unchanged because `not reason` is still required.

The cookbook update at the bottom of the report is gold: *"Optional[str] collapses refused-vs-no-trigger; use discriminated returns when functions can refuse on data quality."* Future trigger-evaluator code in any lane should reference this.

**What's concerning.**

1. **Three open questions that compound.** Open Q #1 (equity vulnerability), #2 (parallel hardcoded thresholds), and #3 (parallel implementations of the gate) all become more expensive the longer they sit. Recommend bundling them into a single `f-exit-monitor-quote-guard-unification` brief that:
   - Adds the equity-lane guard
   - Promotes the implausibility threshold to a shared `_exit_monitor_common` helper
   - Promotes the gate logic (`should_consult_monitor_after_refusal`) to the same helper
   
   That makes the equity addition cheap, eliminates the magic-number duplication before it grows to three copies, and unifies the gate behavior across all three lanes. Probably ~100 LOC + tests.

2. **The brief's pre-audit hypothesis was wrong on options.** Cowork (me) wrote "Options has implausible-quote refusal as a `bid<=0 AND mark<=0 → continue` short-circuit. The `continue` short-circuits before ANY exit logic. ✓ Out of scope." That conclusion was based on reading the no-quote branch and missing the implausible-quote branch (different code path inside `_evaluate_exit_triggers`). CC caught the gap during Phase 0 and surfaced it. Lesson: when authoring lane-audit briefs, read each lane's `_evaluate_exit_triggers` end-to-end, not just the entry-point quote check.

3. **Stale uncommitted work** (CC's carry-forward note) is now persistent across multiple sessions. `.commit_msg_*.txt`, `docs/AUDITS/*`, `app/models/trading.py` event listener, `.env.example` flags, `brain_worker.log`, `data/ticker_cache/crypto_top.json`. Operator-tracked. Not blocking, but the longer it carries the more likely a future CC will accidentally inherit and commit some of it.

## Decisions for the operator

1. **Queue the bundled `f-exit-monitor-quote-guard-unification` brief?** Recommended. Closes Open Q #1+#2+#3 in one ~100 LOC pass. Equity gets the guard, threshold lives in one place, gate logic lives in one place. If the operator prefers to leave equity unprotected and ship it as three separate small briefs, that's also defensible — just slower.

2. **Clean the stale uncommitted work** before the next NEXT_TASK. `git stash` or `git restore` on the listed paths, OR document what each is for if any of it is intentional in-progress work.

3. **Phase 3 deployment watch (per brief)** is operator-side: watch `trading_stop_decisions` for the next `DATA_IMPLAUSIBLE` row on TRUMP-USD; verify no `[crypto_exit] CLOSED` for the same trade with same-cycle `pattern_exit_now`. Should be passive monitoring; the next implausible-quote storm will exercise the new gate naturally.

## Pending items still on this chat's list

(Carrying forward; nothing changed since the last review.)

- **PED bracket-writer fix** (`f-bracket-writer-stop-construction-fix`) — staged in NEXT_TASK earlier this thread, not yet executed. Live-money exposure: PED stop placement still failing every minute with "no order_id" error.
- **EKSO/ELTX P/L cleanup** — −$71.80 misreported. Two SQL UPDATEs.
- **CURRENT_PLAN.md cosmetic cleanup** — historical "Open architectural questions" section.
- **Phase 6 multi-leg-order language tightening** in design doc — defer until Phase 6.

PED is the load-bearing one. The operator switching their attention between quote-guard work and the bracket-writer bug is fine; both are real live-money concerns. When ready, run `claude` on the f-bracket-writer-stop-construction-fix brief.

## Status of NEXT_TASK.md

CC marked DONE for `f-fix-implausible-quote-vs-exit_now-ordering`. Awaiting operator's call on what queues next:

- The bundled quote-guard unification brief (cleans up Open Q #1+#2+#3)
- The PED bracket-writer fix (already drafted in this thread)
- Whatever else is in the queue

## Status of CURRENT_PLAN.md

Forward pointer to design doc § 8 still accurate. Open architectural questions section still historically inaccurate (operator answered all of them). Cosmetic; flagged in earlier reviews; non-blocking.
