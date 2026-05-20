# COWORK_REVIEW: f-thread-housekeeping-tail-2026-05-07

## Verdict

Two follow-ups closed in three commits. 40 hardcoded `user_id=1` references swept across 21 tests in 4 service-layer classes via the existing `_seed_user(db, name)` helper. 45 previously-untracked Cowork reviews now tracked (49 total in `docs/STRATEGY/COWORK_REVIEWS/`). Magic-number audit clean. The strategy thread closes clean for real this time.

Two surprises CC handled correctly: (1) brief example was wrong about the helper signature (`user.id` vs `uid` direct return), CC adapted to the real signature; (2) `user_id=999` sentinel in `test_close_wrong_user_returns_none` preserved intact (negative-path test asserting graceful handling of a non-existent user — replacing with a real user would have inverted the test's intent).

The test-runner kill at 4/21 explicit PASS is the only real flag — discussed below.

## Algo-trader lens

Nothing live touched. Tests only.

## Dev-architect lens

**What's good.** Three-commit boundary respected. 49 Cowork reviews now in git history is a meaningful audit-trail enrichment for the position-identity refactor's eventual postmortem and for any future strategy-thread reconstruction. The `user_id=999` sentinel call-out is the kind of test-archeology that earns trust — auto-replacing it would have silently broken a guard-test invariant.

The 40-hit substitution is mechanically simple (literal `user_id=1` → `user_id=uid` after a `uid = _seed_user(db, "<test-suffix>")` line), grep confirms zero residual hits in the target classes, and the rollback path is single-commit revert.

**What's concerning.**

1. **Pytest run killed at 4/21 explicit PASS.** CC documented the kill honestly with reasoning: ~25-30 min/test wall time vs the prior brief's ~1 min/test on `_seed_user`-based helpers (15-30× slower). 8 hours of wall time to complete the remaining 17 tests was operator-authorized as too long. The substitution is mechanical, the helper is the same one that shipped clean before, and the 4 tests that DID run all passed. CC's reasoning is defensible.

   But the test-pace gap is strange. Same DB, same conftest truncate, same helper. The only differences I can think of: (a) service-layer tests insert more rows per test (multiple `add_to_watchlist` / `create_trade` calls that hit more FK checks), (b) some lock contention against another consumer (live brain-worker on the test DB? unlikely if `_test`-suffixed; but worth checking), (c) cold cache vs warm. None of these would normally produce a 15-30× pace gap. **Worth a one-shot diagnostic if the operator wants the full suite green:** run the killed 17 tests in isolation against a clean container restart and time them. If they run at normal pace, it was lock contention; if they're still slow, there's a real test-fixture inefficiency to fix.

   Practically: the unverified 17 tests carry a small risk that one of them has a substitution-related bug we can't see yet. Mitigations: (a) the substitution pattern is identical to the 9/9-passing prior brief, (b) `git revert 42f15e2` is clean if anything regresses, (c) the bug class (bad substitution) would manifest as test failures, not production behavior changes.

2. **`docs/STRATEGY/COWORK_REVIEWS/` content not deeply audited.** CC spot-checked the most recent review and found it well-formed; the brief explicitly asked for surface inspection of the 45 newly-tracked files. Good-enough for first-pass; if any older review contains a draft or sensitive content the operator wants out, `git rm` is local + history-preserving.

3. **Three items still in `git status` after this brief**: `brain_worker.log`, `data/ticker_cache/crypto_top.json`, `docs/STRATEGY/CURRENT_PLAN.md`. The first two are runtime; the third is operator's working state. Per CC report, "all genuinely runtime/operator-side, none are CC-actionable." Concur.

## Decisions for the operator

1. **Optional follow-up: diagnose the test-pace anomaly.** If you want the remaining 17 tests verified green, run `pytest tests/test_trading.py::TestWatchlistService -v` in isolation with a fresh container restart. Should complete in ≤ 5 min if the 25-30 min/test rate was lock contention; if it's still slow, there's a real test-fixture inefficiency worth a follow-up brief.
2. **Audit the COWORK_REVIEWS backlog?** The 45 newly-tracked reviews span 2026-05-01 through 2026-05-07. If any contain content you'd rather not have in git history, `git rm <file>` per file (history of the rest preserved). Otherwise leave; the audit trail is the value.
3. **Working-tree CRLF disk-view sync** (the third follow-up from the prior review's open list). Operator-side, host-mount refresh; CC can't execute it from the Linux mount. Easy fix when convenient: re-sync the project from the host, or `git checkout-index -f -a`.

## Pending items still on this thread's list

**(Empty.)** No actionable Cowork-side items remain. Three optional operator-side conveniences above; none blocking.

The strategy thread is fully closed:
- No carry-forward items.
- No live-money bug class known-broken.
- No protocol-required items in flight.
- Phase 1 of position-identity refactor still soaking through 2026-05-11 (passive; nothing to action until soak window completes).

## Status of NEXT_TASK.md

CC marked DONE for `f-thread-housekeeping-tail-2026-05-07`. NEXT_TASK is whatever the operator queues fresh — Phase 2 of position-identity after 2026-05-11, any new bug class that surfaces, or one of the three optional follow-ups above.

## Status of CURRENT_PLAN.md

Current. Phase 1 soaking; Phase 2 queues 2026-05-11+. Per-asset-class phantom-close threshold + per-ticker volatility-derived implausibility thresholds + CRLF mount-artifact gitattributes-renormalize-on-host + test-pace anomaly diagnostic — all surfaced for future operator triage; none blocking.

## Closing note

Across the full strategy thread (tasks #1–#32, four briefs in the housekeeping arc):

**Architecture & design:** Position-identity design doc + Phase 1 (4 new tables + shadow-mode write + backfill, 19/19 audit parity).

**Exit-engine correctness chain (2026-05-06/07):** Parity persistence, partial-profit consumer wired, time-decay unit-fix (81% of patterns silently broken), implausible-quote ordering, three-lane quote-guard unification, crypto `pattern_exit_now` gap, phantom-close guard live.

**Bracket-writer & broker-truth:** PED tick-size rounding, covering-sell handling, terminal-reject cooldowns (FIX 50-53), R32 wholesale-empty-positions guard, inverse-reconcile + C2 phantom guard.

**Memory leaks (other chat):** f-leak-3 (yfinance Thread leak), f-leak-4 (strat_cls).

**Housekeeping (this session):**
- EKSO/ELTX P/L scar healed (commit 7f348ba).
- Phantom-close guard live (commit 7f348ba).
- 9 orphaned `.commit_msg*.txt` files deleted.
- `.gitattributes` line-ending normalize.
- `docs/AUDITS/` gitignored.
- 48 hardcoded `user_id=1` test-fixture bugs fixed (8 in TestTradingModels via aa5fd0c, 40 in 4 service classes via 42f15e2).
- 45 Cowork review files now tracked in git history.

**Thirty-two tasks marked complete.** Whenever you're ready for Phase 2 (or anything else fresh), it's a new thread. The audit trail for everything that shipped lives in `docs/STRATEGY/{CC_REPORTS,COWORK_REVIEWS}/`.
