# COWORK_REVIEW: f-exit-monitor-quote-guard-unification

## Verdict

Three Open Questions closed in one bundled change. Magic-number audit clean (zero new literals — relocated only). Tests +14 (11 helper + 3 equity-lane), existing crypto and options contract tests pass without modification. Two source-text guards in the options test file got tightened during execution — STRICTER assertions now require both the `not reason` short-circuit AND the helper-routing, which catches any future drop of the unification.

The brief said "~100 LOC + tests" and the actual work matches that scale. No scope creep, no surprise side-effects.

## Algo-trader lens

**What's good.** Equity lane finally has parity with crypto and options on the data-feed-trust boundary. CC verified by code-read that the equity flow places the new guard correctly (no-quote skip → quote-source tracking → **NEW implausibility guard** → stop/target eval → monitor consultation), so the implausible-quote `continue` short-circuits BEFORE the existing monitor consultation. That's why the helper's `should_consult_monitor_after_refusal` doesn't need to be wired in equity today — the position-flow ordering already does the right thing. CC's Surprise #3 documents this with the precision needed to hand off to a future restructure-the-flow brief.

The structural-constants framing is the right one. CC's cookbook update at the bottom of the report — *"physics-of-markets constants are distinct from strategy parameters that the brain learns"* — is a reusable principle. The 0.1x / 10x bounds are categorically different from `ema_period` or `atr_multiplier`; they're "what's-impossible" rather than "what's-best." Operator's no-magic-numbers discipline holds because no NEW behavioral threshold was introduced; the existing bounds got a single, documented home.

**What's narrow.** The 75-second-per-test cost on the equity-lane DB-bound tests (`tests/test_auto_trader_monitor_implausible_quote.py`) is a CI/dev-loop friction. Three tests × 75s = ~225s for that file alone. Probably acceptable since the equity guard fires rarely in production (only when a real data-feed error hits a tracked ticker), but it's worth tracking — if these tests have to expand to cover more scenarios, the runtime adds up. Could be a follow-up "speed up the equity-lane test fixture" task using shared-state setUp instead of TRUNCATE-per-test.

**What's deferred and worth tracking.** Open Q #1 (per-ticker volatility-derived thresholds) is the right shape for a follow-up if the operator ever sees the structural bounds reject a real meme-stock move. Until then, the structural constants are correct-by-construction. Open Q #3 (equity-side LLM advisory consumption) is a real forward pointer — when someone adds advisory to the equity exit lane, they MUST gate on the helper or Case 5's bug class re-emerges. Worth a one-line note in the equity-lane file's module docstring if/when that brief queues.

## Dev-architect lens

**What's good.** Two-commit boundary respected (one fix, one CC report). The shared module `_exit_monitor_common.py` already existed from the prior options task; CC honestly noted it extended rather than created (Surprise #2). That's the right discipline — claim what you actually did, not what the brief said you'd do.

The two source-text guard updates (`test_case4_native_dte_trigger_wins` and `test_options_call_site_gates_monitor_on_abstained_implausible`) are net-positive: the new assertion checks BOTH `not reason` AND the helper call, which is strictly more rigorous than the old single-substring check. Future drops of the helper routing get caught.

CC's Surprise #3 explanation of why equity doesn't need the helper today is the kind of code-read-verified reasoning that earns trust. The flow analysis is precise: implausibility guard placement is BEFORE the existing monitor consultation, so `continue` short-circuits before consultation can race the bad quote. Anyone restructuring the equity flow needs to read that section before changing ordering.

The grand total of +14 tests is right-sized for the bundled work. No test-bloat, no test-paucity.

**What's concerning.**

1. **CC noted: "8/8 PASS expected (run pending at CC-write time)"** for crypto. The CC was written before the crypto suite finished running. Helper-routing IS semantically equivalent (same return values, same prefix string), so the prediction is sound — but worth confirming post-deploy that the crypto run actually completed cleanly. If the operator hasn't already verified, a post-deploy `pytest tests/test_crypto_exit_monitor_pattern_exit_now.py -v` run would close the loop. Five seconds of operator effort.

2. **`_exit_monitor_common.py` location** (CC Open Q #2). At `app/services/trading/_exit_monitor_common.py`. Underscore-prefix suggests "private to this package" but it's now imported from three sublanes (crypto/, options/, root). If the broader trading-services structure has a `common/` or `shared/` convention, the underscore-prefix becomes misleading — readers may think the helpers are private to `trading/` when they're explicitly designed for cross-lane reuse. Cosmetic; non-blocking; surface for operator preference if there's a canonical location.

3. **The stale uncommitted work** (carry-forward from earlier reviews) is now persistent across multiple sessions. Same list, growing operator-tracked debt. Worth a single `git status` audit pass when convenient.

## Decisions for the operator

1. **Per-ticker volatility-derived thresholds (Open Q #1).** Recommended: defer until production data shows the structural bounds rejecting a real move. The bounds are conservative enough that real intraday moves shouldn't trip them; if they do, that's the signal to derive per-ticker bands.

2. **Equity-side LLM advisory queueing (Open Q #3).** No action needed today; track as a forward-pointer requirement when/if a future brief adds advisory consumption to `tick_auto_trader_monitor`.

3. **Verify the crypto suite ran post-deploy.** Five-second `pytest tests/test_crypto_exit_monitor_pattern_exit_now.py -v` run if you haven't already; CC predicted 8/8 PASS but didn't capture the actual result.

4. **`_exit_monitor_common.py` location.** Cosmetic move-or-keep choice. Today's location is reasonable; non-blocking.

## Pending items still on this thread's list

**Correction (filed after this review's first draft):** the prior carry-forward incorrectly listed `f-bracket-writer-stop-construction-fix` as pending. It actually shipped 2026-05-06 in commit 87c2fe0 (tick-size rounding via `normalize_price` + full-diagnostic logging on rejection + 7 tests in `tests/test_broker_stop_construction.py`); CC_REPORT is bundled in `2026-05-06_f-overnight-jumbo-2026-05-06.md`. The "still the most load-bearing live-money concern" line was a stale claim from earlier in the thread; removed below.

Actual remaining items:

- **EKSO/ELTX P/L cleanup** — −$71.80 misreported. Two SQL UPDATEs (trade 1815 EKSO, trade 1816 ELTX): set actual exit_price (10.76 / 10.70), pnl (−38.80 / −33.00), exit_reason='broker_stop_filled_outside_chili'. Verifiable from staging or live DB.
- **PED rejection storm — verify quiet post-fix.** Scheduler-worker logs were clean for the last 2h at review time, but no PED was actively in scope. When PED next enters a tracked position, watch for the new INFO `stop_price rounded to broker tick` log line on placement and zero `SELL_STOP rejected (full diagnostic)` entries.
- **CURRENT_PLAN.md cosmetic cleanup** — file still opens with 5 architectural questions the operator already answered (the doc-revision brief integrated answers into POSITION_IDENTITY.md but didn't update CURRENT_PLAN.md to point there cleanly). Non-blocking; one editing pass closes it.
- **Stale uncommitted work** — `.commit_msg_*.txt`, `docs/AUDITS/*`, `app/models/trading.py` event listener, `.env.example` flags, `brain_worker.log`. Operator-tracked, persistent across sessions.
- **Phase 1 position-identity 1-week soak** — passive; nothing to do until soak window completes; then Phase 2 (`trading_execution_events.position_id` backfill) queues.

With PED fix shipped, the architectural-correctness chain across 2026-05-06/07 (parity persistence, partial-profit consumer, time-decay unit-fix, paper-shadow mode, leak-4 strat_cls, exit-monitor unification) closes cleanly. No active-money bug class is currently known-broken.

## Status of NEXT_TASK.md

CC marked DONE for `f-exit-monitor-quote-guard-unification`. Awaiting operator's call on what queues next:

- EKSO/ELTX P/L scar (small, two-line correction; can ship as a one-shot brief)
- CURRENT_PLAN.md doc cleanup (cosmetic; can do without a brief)
- Per-ticker volatility-derived implausibility thresholds (Open Q #1, future enhancement, not urgent)
- Whatever surfaces from other chats

## Status of CURRENT_PLAN.md

Forward pointer to design doc § 8 still accurate. The 5 architectural-questions block is now stale (operator answered them on 2026-05-04, integrated into `docs/DESIGN/POSITION_IDENTITY.md`). One editing pass to replace the stale block with a "Phase 1 shipped 2026-05-04, soaking through 2026-05-11" note.
