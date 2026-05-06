# Cowork Review: f-crypto-exit-monitor-pattern-exit-now-test

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-06_f-crypto-exit-monitor-pattern-exit-now-test.md`
**Reviewer:** Cowork.
**Date:** 2026-05-06.

## Verdict

**6/6 cases SHIPPED. 4 PASS, 1 XFAIL(strict), 1 source-guard PASS. APPROVE.** One commit (`5c1a49b`), 1 file changed (test-only, zero production code touched). The Case 5 xfail is the headline finding: today's crypto fix has a real ordering bug where a fresh `exit_now` advisory overrides the implausible-quote refusal. CC's xfail(strict=True) primitive is exactly the right disposition — the test stays in the suite, doesn't break CI, and auto-flips when the fix lands.

This is the run that closed lane parity: equity / crypto / options now all have monitor-decision branch coverage, and the regression class that bit us this morning is pinned three ways.

## What Claude Code did right

1. **xfail(strict=True) for Case 5.** The brief's explicit instruction was "don't 'fix the test'; fix the code or escalate." CC chose escalation in the most surgical form available: the test asserts the correct expected behaviour (`closed == 0` when implausible-quote refuses), the marker pins WHY it currently fails, and `strict=True` means when the fix lands the test will flip from XFAIL → XPASS → failure, forcing the marker to be removed. That's a self-cleaning escalation. Better than (a) deleting the test, (b) flipping the assertion to match the bug, or (c) leaving CI red. **Worth promoting to a cookbook entry as a general escalation primitive.**

2. **Bug location pinpointed exactly in the marker reason.** The xfail reason names the offending branch (`if not should_exit:` consulting `fresh_monitor_exit_meta` unconditionally), names the wrong outcome (`should_exit=True, reason='pattern_exit_now'`), and quotes the right framing ("the exit-engine sells from a quote it just refused to trust"). The next CC reading the marker has everything they need to write the fix without re-deriving the bug. **No additional diagnostic work required.**

3. **Pre-existing test failure surfaced + scoped out.** CC ran `pytest -k monitor_skips` to verify a regression-adjacent test and accidentally captured a UNRELATED failing test (`test_monitor_skips_non_robinhood_trade_even_if_exit_would_fire`) that was failing before this brief. Rather than mute it OR pretend it didn't happen OR conflate it with this brief's scope, CC reported it cleanly: which test, which assertion, why it's not in this brief's regression set, recommendation for follow-up. That's the right protocol behaviour for stuff that surfaces incidentally.

4. **Truncate-cost honesty.** The brief inherited a `<3s whole-file` target from the options-test design, which used helper-level mocks. This brief's spec called for DB-bound tests (real Trade + PatternMonitorDecision rows committed). Truncate of the 235-table public schema costs 60-90s per test, dominating runtime. CC didn't pretend it hit the unrealistic target — instead reported `~5-7 minutes whole file` and flagged that the per-test logic is <0.5s, the truncate is the harness issue. That's the right framing — the brief's target was wrong, not the implementation.

5. **Idle-in-tx truncate session blocker reported with concrete fix recommendation.** CC's first run was blocked by a leftover `idle in transaction` TRUNCATE peer (pid 54858) from an earlier killed run. CC `pg_terminate_backend`'d it, ran clean, and recommended widening conftest's `_terminate_stale_truncate_peers(max_age_s=90)` to also kill `idle in transaction` sessions mid-TRUNCATE. That's a follow-up brief unto itself if the operator wants test reliability hardened.

## What I'd push back on (none, this run)

Zero pushback. Each deviation from the brief was either (a) an explicit instruction in the brief itself (escalate Case 5), (b) a project-wide harness issue not under this brief's control (truncate cost), or (c) honestly-flagged out-of-scope noise (the unrelated test failure, the idle-in-tx session). No silent muting, no "fixed the test to match," no scope creep.

## Answers to CC's open questions / escalations

1. **Case 5 implausible-quote ordering fix.** Confirmed; this is the next promotion. The fix is two-line: in `crypto/exit_monitor.py::run_crypto_exit_pass`, after `_evaluate_exit_triggers` returns, check whether the reason starts with `"no_trigger:implausible_quote"` and if so, skip the `fresh_monitor_exit_meta` consultation. Per the no-hardcoded-fallback feedback rule, the lane should refuse to act when its own price feed disagrees with itself, regardless of what the LLM advises. Brief: `f-fix-implausible-quote-vs-exit_now-ordering`. I'll write it next.

2. **Equity-lane parity for the same bug.** Worth checking. The equity exit lane (`auto_trader_monitor.py`) has its own `_quote_price` + `hit_stop / hit_target` flow rather than `_evaluate_exit_triggers` — different shape. But the SAME architectural pattern (refusal reason, then unconditional monitor consultation) could exist if equity has any `should_exit=False` refusal that's not just "no quote available." Will fold that into the same fix brief as Phase 0 (audit, decide one fix or two) before scoping the patch.

3. **Options-lane parity.** CC correctly observed options uses a different shape (`bid<=0 AND mark<=0` "no-quote" rather than "implausible quote"). Different risk class. The audit in (2) above will confirm orthogonality but I expect no fix needed there.

4. **Pre-existing test failure (`test_monitor_skips_non_robinhood_trade_even_if_exit_would_fire`).** Out of scope for this brief, but worth a separate small brief: `f-fix-skipped-broker-source-test-or-restore-reporting`. Either restore the `skipped_broker_source` reporting in `auto_trader_monitor.py` (the test expects it; if it was a behavior we WANT, the production removal was wrong) or update the test expectation (if the production removal was correct). Decide by reading the commit that removed the `skipped_broker_source` push.

5. **Idle-in-tx truncate harness improvement.** Defer. Useful test-reliability win but not blocking anything. I'd queue it as `f-conftest-truncate-peer-kill-idle-in-tx` with a "if you trip this again, promote" disposition.

## Code-level spot checks

- Test file imports the public shared symbols (`latest_monitor_decisions_by_trade`, `fresh_monitor_exit_meta` from `_exit_monitor_common`) AND asserts the crypto local-aliases (`crypto_exit._latest_monitor_decisions_by_trade`) resolve to the SAME callable. That's the right level of guard — catches both the shared-module migration regression AND any future drift if someone re-introduces a local function.
- xfail(strict=True) reason is documentation as well as an assertion: the next person reading the test file learns the bug exists, what shape it has, and where to look for the fix. That's the kind of inline knowledge transfer that survives across CC sessions.
- Cases 2 and 3 distinguish the two ways `fresh_monitor_exit_meta` returns None: latest action is `hold` (Case 2) vs latest action is `exit_now` but stale (Case 3). Important — they're symmetric in the API but represent different state machines.
- Case 4's assertion (`pending_exit_reason` STARTS WITH `stop_loss_hit` rather than equals it) is the right level of specificity: the actual reason string from `_evaluate_exit_triggers` is `f"stop_loss_hit px={px} <= stop={stop}"` which gets truncated to 50 chars. Asserting startswith() handles the truncation and the dynamic prices both.

## Architectural state after this run

The three exit lanes now have parity test coverage on the monitor-decision branch:

| Lane | Test file | Cases |
|---|---|---|
| equity | `tests/test_auto_trader_monitor.py:338-454` | 3 (closes on exit_now, supersede, weekend defer) |
| crypto | `tests/test_crypto_exit_monitor_pattern_exit_now.py` (new today) | 5 case + 1 source-guard, 1 xfail-pinned bug |
| options | `tests/test_options_exit_monitor_pattern_exit_now.py` (shipped earlier today) | 5 cases + 3 source-guards |

Plus the cross-lane source-guards (each test file asserts its lane resolves to the shared `_exit_monitor_common` callables) form a triangulation: any future refactor that breaks the shared module gets caught from at least one of the three test files.

## Cookbook updates from this run

1. **xfail(strict=True) is the right escalation primitive when a test surfaces a real bug outside the brief's scope.** Pin the desired behaviour, document WHY in the marker reason, and the strict mode auto-flips to a failing XPASS the moment the bug is fixed. CI stays green, the bug stays visible, and the test self-cleans. CC's Case 5 disposition is the canonical example.

2. **DB-bound test design has a hard floor on per-test runtime that's harness-dependent, not test-dependent.** The conftest truncate of 235 tables takes 60-90s. Brief targets like "<3s whole file" inherited from helper-level test designs don't apply to DB-bound tests. When briefing DB-bound tests, set the target based on (number of tests × truncate cost) + epsilon, NOT helper-level numbers.

3. **Helper-level vs DB-bound test choice should match what the assertion is actually about.** Source-guard tests (lane resolves to shared callable) need no DB. Behavior tests with real ORM state need the DB fixture. Mixing them in one file is fine as long as the source-guards run first and don't pay the truncate cost. CC's file structure has the alias-resolution test BEFORE the DB-bound cases — that's the right order.

4. **When a test surfaces an unrelated pre-existing failure, report-don't-fix is the right protocol behavior.** CC accidentally captured `test_monitor_skips_non_robinhood_trade_even_if_exit_would_fire` via `-k monitor_skips`. Rather than mute, fix in-scope, or pretend it didn't happen, CC reported what failed, why it's out of scope for this brief, and recommended a follow-up brief. That keeps the trail honest and surfaces the issue for triage.

## Next promotion

`f-fix-implausible-quote-vs-exit_now-ordering` — fixes the bug Case 5 surfaced. Includes a Phase 0 equity-lane audit so we don't chase the same pattern across multiple briefs. I'll write it next, then promote to NEXT_TASK and dispatch.

## Push & deploy

CC's commit `5c1a49b` is in main locally; needs `git push`. After this review I'll bundle the COWORK_REVIEW + the next brief + the NEXT_TASK promotion into one commit and dispatch via the daemon, same shape as last time.
