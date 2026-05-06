# Cowork Review: f-options-exit-monitor-pattern-exit-now-audit

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-06_f-options-exit-monitor-pattern-exit-now-audit.md`
**Reviewer:** Cowork.
**Date:** 2026-05-06.

## Verdict

**5/5 phases SHIPPED. APPROVE.** One commit (`4546610`), 5 files changed (3 modified + 2 new). 8 new tests pass + 3 equity regression tests pass unmodified. The architectural cleanup is done and the third (and likely final, until perps land) exit lane is at parity with the equity lane.

The structural win: the three exit lanes (equity, crypto, options) now share a single `_exit_monitor_common.py` module. The drift class that bit us this morning (crypto silently missed the LLM advisory branch for the lifetime of Task HHH) is closed by construction — the only way the lanes can disagree on freshness or selection logic now is to override the shared module, which is loud, not silent.

## What Claude Code did right

1. **Phase 1.2 query was the sharpest finding.** The brief asked CC to count how many open option positions had stale `exit_now` recommendations, framing the result as "the operational cost of the gap." CC ran the query and reported **zero open option positions right now**, with 91 total `exit_now` decisions across all asset classes in the last 7 days. So the architectural gap is real but the current operational cost is zero. **The fix is forward-looking** — once option positions DO open, parity is in place. This is the right framing: ship the structural fix even when it's not paying for itself today, because the next surface is going to be expensive (theta-decaying option holds for 20h would cost more than the TRUMP-USD case did).

2. **Refactor was behavior-preserving.** CC migrated equity + crypto to the shared module while keeping the local private names (`_latest_monitor_decisions_by_trade`, `_fresh_monitor_exit_meta`, `_MONITOR_EXIT_NOW_MAX_AGE_HOURS`, `_CRYPTO_MONITOR_EXIT_NOW_MAX_AGE_HOURS`) as aliases to the shared symbols. Existing tests run unmodified post-refactor (verified: `tests/test_auto_trader_monitor.py::*monitor_decision*` 3 cases pass in 204.76s). No external callers needed updating. The aliases are dead code from the perspective of the implementation but live for consumer compat — exactly the right call when migrating a public-ish surface in a production tree.

3. **Stop-on-tie ordering preserved.** Native premium/DTE/stop triggers WIN over `exit_now` in the options lane, mirroring the crypto fix and the equity lane. CC used the same `if not reason: monitor_exit_meta = ...` shape — meaning "consult the LLM advisory ONLY when no native trigger fired." Postmortems will show "stop hit at $X" vs "DTE proximity" before they show "LLM said so," which is the right semantic ordering for forensics.

4. **Test design correctly chose helper-level + source-text guards over full integration.** The brief Phase 4 spec asked for cases that mirror crypto, including ones that exercise the broker adapter / contract resolver / `place_option_sell`. CC pushed back: full integration with mocked Robinhood adapter would be heavy under the per-test-truncate fixture and would catch the same regression class as helper-level coverage + source-text guards. The 3 refactor-regression tests pin the shared-module wiring at the source level (`assert "_exit_monitor_common" in source`) — that's the kind of guard that catches a future contributor accidentally re-inlining the helpers, which is the actual class of regression the brief was trying to prevent. The 5 case tests cover the helper logic itself. **Both layers caught at the right granularity.**

5. **Postmortem note actually appended.** The crypto fix's `Related queued work` section now has the strikethrough + linkback to this report, which is the audit trail I asked for in brief Phase 5. (I'd flagged it as "optional but useful" — CC shipping it anyway means the next person grepping the crypto report finds the broader pattern resolved without having to chase the queue.)

## What I'd push back on (none, this run)

Nothing material. CC acknowledged each brief Open Question explicitly and made defensible calls:

- **Freshness window**: kept 96h per brief default. ✓ Correct. The "options theta-decay says tighten" intuition isn't backed by data yet; defer until the first option position rides 20+ hours on a stale `exit_now` and we have a real number.
- **Implausible-quote guard parity**: CC noted options has the equivalent at lines 332-347 (`bid≤0 AND mark≤0 → skipped_no_quote`). Different shape (defer-on-no-quote vs implausible-on-bad-quote) but covers the same regression class. ✓ No additional work needed.
- **Re-export cleanup**: deferred. ✓ Correct call; the aliases catch any external consumer we don't know about. Drop in a follow-up brief once we've grepped + confirmed.
- **PatternMonitorDecision writer for options**: surfaced as Open Q #4. ✓ Right place to flag — the audit found 91 decisions in 7 days but didn't break down by asset class. Worth checking once the first option position actually opens; until then it's a theoretical concern.

## Answers to CC's open questions

1. **Freshness window**: keep 96h. If/when the first option position rides a stale `exit_now` past 24h, surface that case and we'll re-evaluate with data. Don't tighten preemptively.

2. **Implausible-quote guard parity** (resolved by CC's own check): no action.

3. **Re-export cleanup**: queue a follow-up `f-exit-monitor-private-alias-cleanup` if you want — it's a 5-minute brief, but unblock-priority is "whenever." Suggestion: do it when the next non-trivial change to either equity or crypto's monitor lands; piggyback the cleanup onto the natural touch.

4. **PatternMonitorDecision writer for options**: I'll add the breakdown query to my watch list. Concrete check: when the first option position opens AND the LLM/pattern monitor flags it for a `pattern_monitor_decision` row, verify the row actually lands. If not, that's a separate `f-options-pattern-monitor-coverage` brief (in the original Phase 1.2 "out of scope" section).

## Code-level spot checks

- `_exit_monitor_common.py` reads cleanly. TYPE_CHECKING for the SQLAlchemy + ORM imports avoids circular load. `__all__` declared. Module docstring captures the WHY (drift between equity and crypto copies) which means the next reader doesn't ask "why does this exist."
- `MONITOR_EXIT_NOW_MAX_AGE_HOURS = 96.0` lives here as the single source. Inline comment hints at the right escape hatch ("introduce a per-asset override at the call site rather than splitting the module") if a future asset class needs different freshness.
- `options/exit_monitor.py` lines 337-352: monitor consultation block has a clear inline comment citing the brief and the stop-on-tie reasoning. The success log line at 426-443 carries the full audit metadata.
- Test file `tests/test_options_exit_monitor_pattern_exit_now.py` reads cleanly: 3 source-guard tests + 5 helper-level cases. The source-guard for "all three lanes import shared" at line ~30 is exactly the regression test the brief asked for.

## Architectural state after this run

The three exit lanes are now structurally uniform on the LLM-advisory dimension:

| Lane | Native trigger | LLM advisory consumer | Freshness window |
|---|---|---|---|
| equity (`auto_trader_monitor.py`) | price (stop/target) | `_exit_monitor_common.fresh_monitor_exit_meta` | shared 96h |
| crypto (`crypto/exit_monitor.py`) | price (stop/target) | `_exit_monitor_common.fresh_monitor_exit_meta` | shared 96h |
| options (`options/exit_monitor.py`) | premium/DTE/stop | `_exit_monitor_common.fresh_monitor_exit_meta` | shared 96h |

The next exit lane (perps, forex, whatever lands) imports from the same shared module from day one. The brief's cookbook entry — "asset-class-split exit lanes are a systematic pattern" — captures this as design guidance.

## Cookbook updates from this run

1. **When the operational cost of an architectural gap is currently zero, ship the fix anyway if the next surface is expensive.** CC's Phase 1.2 query showed zero open option positions, which could have been used to defer the brief. Instead, the brief shipped because the cost of the NEXT incident (a theta-decaying 20h option hold) would have been more expensive than the cost of TRUMP-USD's spot hold. Forward-looking structural fixes pay off when the asset class has a higher per-incident cost than the surfaced one.

2. **Source-text test guards are the right granularity for "did the refactor stick" regressions.** Heavy integration tests can catch behavior regressions but rarely catch "someone re-inlined the helper they were supposed to import." A 10-line `assert "_exit_monitor_common" in source` test catches that class directly and runs in 0.01s. CC made the right tradeoff over the brief's spec.

3. **Re-exports preserve compatibility during a migration but should have an exit plan.** The four private-name aliases in equity + crypto (`_latest_monitor_decisions_by_trade`, etc.) are dead from the implementation's perspective but live for consumer compat. The follow-up to grep + drop them is queued informally; if the operator wants formal tracking, file `f-exit-monitor-private-alias-cleanup`.

4. **Module-level constants in shared code should have an inline escape-hatch comment.** `MONITOR_EXIT_NOW_MAX_AGE_HOURS = 96.0` in `_exit_monitor_common.py` carries a comment saying "if a future asset class needs a tighter window, introduce a per-asset override at the call site." That spares the next consumer the "do I extend the constant or override locally?" decision.

## Push & deploy

Commit is `4546610` on `main`. Combined with the prior 10 unpushed commits + the unpushed crypto fix (uncommitted at session start, now part of the chain), the push will publish:

- 10 carried-over commits from the overnight jumbo + earlier work
- 1 crypto exit fix (today's live debug)
- 1 options exit refactor + parity (this brief)
- Plus the docs (CC reports, COWORK reviews, NEXT_TASK promotions, backlog updates)

`git push` from the host once the commits are organized. No migration to deploy. After push:

- The patched options + crypto + equity will pick up automatically on the next `autotrader-worker` cycle (no rebuild — bind-mount is in effect for `./app:/app/app`).
- Run `pytest tests/test_options_exit_monitor_pattern_exit_now.py -v` once on the host to confirm the 8 cases pass.
- Optional smoke: confirm no test in `tests/test_auto_trader_monitor.py::test_monitor_*` regressed (CC reports they pass; verify locally for confidence).

Watch items:
- First option position that opens — verify a `pattern_monitor_decision` row is written for it (CC's Open Q #4). If yes, the lane is end-to-end functional. If no, file `f-options-pattern-monitor-coverage` and the LLM advisory side is still a no-op for options regardless of this fix.
- First `pattern_exit_now` `[options_exit] CLOSED` log line. The audit metadata format is the proof of life.

## Status update

Marking `NEXT_TASK.md` as DONE for this brief. The natural next promotion is `f-crypto-exit-monitor-pattern-exit-now-test` (regression coverage for today's live crypto patch), or `f-trump-usd-poisoned-quote-source-audit` (the $0.0003 storm investigation). Operator's call.
