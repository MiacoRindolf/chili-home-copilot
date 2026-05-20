# COWORK_REVIEW: f-monthly-dd-breaker-numerator-symmetrize

**Reviewer:** Cowork (algo-trader + dev-architect lenses)
**Reviewed CC_REPORT:** `docs/STRATEGY/CC_REPORTS/2026-05-16_f-monthly-dd-breaker-numerator-symmetrize.md`
**Reviewed commits:** `ff7b03d` (parallel-brief), `fdfe15d` (fix), `3e3253b` (test)

## What's good

**The helper-extraction is a positive deviation.** The brief asked for two predicates added inline to the existing `monthly_pnl` SELECT. CC instead extracted `_monthly_attributed_pnl(db, user_id)` and replaced the inline SELECT with a helper call. Functionally identical, structurally cleaner: the threshold's SELECT and the numerator's SELECT now sit side-by-side in the same module, both filtered with the same scope. Any future scope tweak touches both in one diff. The threshold and the numerator can no longer drift apart by accident — that's the whole point of the fix made architectural rather than tactical.

**The control-loop reasoning is in the comments.** The new helper's docstring and the call-site comment both reference the ARCHITECT-FLAG and the open-loop control argument. Six months from now this won't look like a one-line WHERE-clause tweak that happens to be load-bearing — it'll look like an intentional symmetry property of the breaker.

**Log line preserves on-call disambiguation.** Final wording is `30-day realized PnL $X.XX (CHILI-attributed only)` — matches the brief's literal text. Operator pager-reading at 2am won't compare this to account-level cum PnL by accident.

**Test coverage is appropriate.** Two regression tests: one direct-helper assertion (CHILI-attributed +$300 / unfiltered -$1700), one end-to-end assertion (flag ON, +$10/day × 35 days plus a -$1000 no_pattern bleed → monthly_dd path does NOT trip). The first is a sanity check that's almost impossible to misread; the second is the load-bearing behavioral guarantee. Good split.

**Watch script updated.** D4 shipped — `scripts/dispatch-monthly-dd-arming-watch.ps1` now filters `scan_pattern_id` on both daily and monthly queries. Daily watch and runtime breaker will read identical numbers post-deploy.

## What's concerning

**Pytest never actually ran the new tests.** CC noted a local pytest-asyncio × pytest 9 incompatibility blocked self-verification. I tried three host-side dispatches:

1. `conda run -n chili-env pytest ...` — timed out at 5 min (asyncio plugin hung at collectstart).
2. `docker compose run --rm chili pytest ...` — timed out at 4 min (compose run startup overhead).
3. `docker compose exec -w /workspace chili pytest ...` — **collected all 11 tests** in the `TestD1MonthlyDdBreaker` class but **all 11 ERRORED at fixture setup** with `psycopg2.errors.DeadlockDetected` while the `db` fixture's `TRUNCATE ... CASCADE` was running. Process 87733 (the test) was waiting on `AccessExclusiveLock` while process 87191 (presumably one of the live workers writing to `trading_trades` or similar) held a `RowShareLock`.

Important: **the pre-existing tests** in the same class (`test_threshold_returns_none_below_30_days`, `test_threshold_computes_when_30_plus_days`, etc.) **also errored on the same deadlock**. This isn't a regression caused by this commit — it's the live system contending with the test fixture for the same `chili_test` database. CC's commit-time green status from `3e3253b` was earned in a clean environment.

The fix is small, additive, behind a default-OFF flag, statically reviewed clean, and the bug it closes is real and well-understood. Shipping without a live pytest pass is acceptable given the change profile, but the inability to self-verify is a sharp edge worth fixing. CC's suggested follow-up `f-pytest-asyncio-env-pin` is right; I'd add a second slug: `f-chili-test-isolation` for the deadlock-during-truncate problem (the live workers shouldn't be touching `chili_test` at all if the URL config is right; if they are, that's a leak).

**The sibling brief is awkward.** CC's commit `ff7b03d` introduced its own brief at `f-monthly-dd-breaker-symmetric-attribution.md` (slug differs from my `f-monthly-dd-breaker-numerator-symmetrize.md`). The two cover the same bug. CC noted both are now closed by this work. Operationally fine; cosmetically untidy. The CC session that wrote the parallel brief ran BEFORE my dispatch (git times 09:33/09:45 PT vs my dispatch 10:13 PT) — meaning a prior Cowork session apparently queued the same problem with slightly different framing. Either two sessions raced or there was an earlier morning session that prefigured today's daily-watch report. I'll consolidate at the next strategy step (move both briefs to a single `QUEUED/_closed/` archive or similar).

## Algo-trader lens

The open-loop bug is closed: the breaker's trip signal and the breaker's lever now act on the same decision channel. Pattern-attributed losses trip the pattern-attributed breaker. Non-attributed losses cannot trip it. The breaker is now a coherent strategy-level risk gate rather than an accidental account-level kill switch in disguise.

The account-level concern remains: CHILI's no_pattern bleed (-$1,560 over the trailing 30d) is still bleeding capital and now has *no* automated gate, because the only existing breaker was just narrowed to attributed-only. This is the right next move structurally, but until `f-portfolio-vs-pattern-breaker-separation` ships, operator-mediated review is the only thing standing between the account and continued no_pattern drain. That brief is unblocked and queued — recommend it as the next strategy task, especially given the legacy cleanup is largely done (12 no_pattern closes in May vs 108 in March per the 2026-05-15 quant audit), meaning the bleed is decelerating naturally and the portfolio breaker is more of a backstop than a forced-arm requirement.

## Dev-architect lens

50 LOC additive, one new helper, one inline-SQL → helper-call substitution, two tests, one PS script. No migrations, no flag flips, no contract changes. The change is well within rollback budget; a single `git revert` of the two commits restores prior behavior and the default-OFF flag means there's no live system behavior to revert anyway.

The hard rules from PROTOCOL.md are all respected: no magic numbers (the breaker stays fully data-driven), default mode unchanged, no live-placement belts touched, no `git push --force`, tests use the `_test` DB (when they can run), commit boundaries are clean.

The only architecture nit: the helper takes `db, user_id` and returns `float`, matching the threshold helper's signature. Future evolution into the two-tier design (`f-portfolio-vs-pattern-breaker-separation`) will need a parallel `_monthly_unattributed_pnl` or `_monthly_total_pnl` helper for the portfolio breaker's numerator. That's a clean addition, not a refactor — fine.

## Next thing

**Recommend promoting `f-portfolio-vs-pattern-breaker-separation` to NEXT_TASK** at operator discretion. It's:
- unblocked by this work,
- has a clear deliverable list,
- closes the remaining account-level gap that this fix intentionally did not address,
- can ship in default-OFF/shadow-log mode with no live behavior change.

If operator prefers to clear the secondary nits first, candidate follow-ups in priority order:
1. `f-pytest-asyncio-env-pin` — local pytest-asyncio × pytest 9 compatibility (so CC can self-verify next time).
2. `f-chili-test-isolation` — diagnose why live workers and `chili_test` fixtures are contending for the same locks; the fixture deadlock today is a symptom of cross-DB confusion or a missed env split.
3. Brief consolidation: merge `f-monthly-dd-breaker-symmetric-attribution.md` and `f-monthly-dd-breaker-numerator-symmetrize.md` into a single archived entry.

## Pending operator actions (carried from CC_REPORT)

- `CHILI_MONTHLY_DD_BREAKER_ENABLED` stays **OFF**. Arm-day projected 2026-05-28; tomorrow's daily watch should report n=22 attributed close-days. The fix is in place — arm-day flip is now safe from the no_pattern-trip path.
- `git push` of `ff7b03d`..`3e3253b` (3 commits) — operator's call. None of the three are time-critical, but the fix is the time-critical one and ships clean.
