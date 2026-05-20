# CC_REPORT: f-portfolio-vs-pattern-breaker-separation

## What shipped

Six commits on `main`, executing the brief in full per the APPROVED plan at
`scripts/_claude_session_consult/portfolio-vs-pattern-breaker-separation-2026-05-16/plan.response.md`:

| # | Hash | Files | Description |
|---|---|---|---|
| 1 | `d6ddbe8` | `app/config.py`, `portfolio_risk.py`, `walkforward_monthly_dd_breaker.py`, `test_phase3_stop_bleed.py` | rename `chili_monthly_dd_breaker_enabled` → `chili_pattern_dd_breaker_enabled` (AliasChoices for legacy env var), add 4 new portfolio-tier settings |
| 2 | `6259912` | `portfolio_risk.py` (+284 lines) | new helpers: `_portfolio_dd_threshold`, `_monthly_total_pnl`, `_persist_portfolio_breaker_state`, `check_portfolio_drawdown_breaker`, `_assert_portfolio_breaker_ok` |
| 3 | `d1bdb18` | `venue/coinbase_spot.py` (+34), `venue/robinhood_spot.py` (+25) | wire `_assert_portfolio_breaker_ok` into 5 BUY-only entry sites (3 Coinbase + 2 Robinhood); zero SELL / cancel / preview paths touched |
| FIX | `4f11100` | `portfolio_risk.py` (+13/-2) | inserted fix commit between 3 and tests: the WHERE clauses needed `(:uid IS NULL OR user_id = :uid)` because the venue gate has no per-user context (see Surprises §1) |
| 4 | `3d6bf9a` | `tests/test_phase3_stop_bleed.py` (+383) | `TestPortfolioBreakerSeparation` — five D6 sub-tests + the alias backwards-compat test (7.6) using a fresh `Settings()` instantiation per Cowork's Q4 answer |
| 5 | `864eca8` | `docs/DRAWDOWN_BREAKER_RUNBOOK.md` (+117) | Two-tier architecture section; absorbing-state manual reset lifted verbatim from plan §10.2 per Cowork's ask |

Migrations added: none. Shadow-log persistence reuses `trading_risk_state` with new
`regime` tags (`portfolio_breaker`, `portfolio_breaker_shadow`) — column-compatible per
plan §6.

## Verification

### ast.parse + grep gates (commit 3 nit (c))

BEFORE-grep + AFTER-grep on `git grep -n 'place_market_order\|place_limit_order_gtc\|place_stop_limit_order_gtc' app/services/trading/venue/` are embedded in commit `d1bdb18`'s body. Confirms exactly 5 gate insertions across the BUY branches, 0 in SELL / cancel / preview / stop-loss-SELL paths.

`ast.parse` clean on every edited Python file at each commit boundary.

### D6 test suite (6/6 PASSED, local pytest with `-p no:asyncio`)

```
tests/test_phase3_stop_bleed.py::TestPortfolioBreakerSeparation
  test_portfolio_breaker_trips_on_all_closed_pnl_exceeding_threshold       PASSED
  test_portfolio_breaker_does_not_trip_on_chili_loss_when_no_pattern_offsets PASSED
  test_pattern_breaker_still_works_post_rename                             PASSED
  test_portfolio_tripped_blocks_manual_buy_through_coinbase_adapter        PASSED
  test_portfolio_not_tripped_pattern_tripped_blocks_attributed_allows_no_pattern PASSED
  test_alias_backwards_compat_legacy_env_var_still_honored                 PASSED
```

Wall time: ~7 minutes for the full TestPortfolioBreakerSeparation class. Test 7.4 alone runs in ~64s post-fix (proves the venue-adapter gate short-circuits before the broker mock is touched — `mock.market_order_buy.called is False`).

`-p no:asyncio` was required to bypass a pytest-asyncio plugin incompatibility under pytest 9.0 (`'Package' object has no attribute 'obj'` at collection). The predecessor brief hit the same issue; it is unrelated to this work.

### Default-OFF posture (Hard Rule + brief constraint)

All three new flags default `False`. The helper short-circuits on the first
`getattr(_s, "chili_portfolio_dd_breaker_enabled", False)` check, so commits 3 and the
fix commit are no-ops at runtime until an operator explicitly arms the breaker. The
existing pattern-tier behavior is unchanged.

## Surprises / deviations

### 1. user_id=None semantic gap surfaced in test 7.4 (fix commit `4f11100`)

The plan §5 said "Default `user_id=None` matches the existing pattern in
`_persist_breaker_state` and `restore_breaker_from_db`." That is true for
persistence — `trading_risk_state` rows are system-keyed by NULL user_id. But the
plan inherited the pattern tier's `WHERE user_id = :uid` filter for `_portfolio_dd_threshold`
and `_monthly_total_pnl`, which under Postgres NULL semantics returns zero rows when
the venue gate calls the helper with default `user_id=None`. Result: the threshold helper
short-circuited on n<30, the breaker permanently returned `(False, None)` regardless of
seeded history.

Fix: change both WHERE clauses to `(:uid IS NULL OR user_id = :uid)`. Architecturally
consistent — CHILI is single-broker-account-per-household, so "portfolio" means the
broker account aggregated across all household users. The pattern tier remains per-user
because it's a strategy-decision lever the autotrader can scope by user_id.

Inserted as its own fix commit (`4f11100`) between the venue-adapter wiring and the
tests rather than amending commit 2, to preserve the commit-boundary integrity CLAUDE.md
requires.

**Flag for Cowork review:** if the household ever becomes multi-broker-account, the
"None = all users" semantic needs revisiting. Today it's correct.

### 2. Commit count is 6 on the branch, not 5

Plan §9 specified 5 commits. Adding the user_id-semantic fix between commit 3 and the
tests bumped the count. The original 5-commit logical structure is preserved (config,
helpers, wiring, tests, runbook) plus one targeted fix between 3 and the tests.

### 3. Test 7.4 had to commit seeds (not just flush)

`_assert_portfolio_breaker_ok` opens its own `SessionLocal`. The test's `db` fixture
session is `autocommit=False`, so `db.flush()` makes rows visible only within the test's
transaction. Test 7.4 calls `db.commit()` after seeding so the venue adapter's separate
session sees the data. Cleanup at the end of the test re-deletes via raw SQL (the
per-test TRUNCATE in conftest still handles general cleanup between tests).

This pattern is documented inline in the test; nothing surprising once you trace the
session-isolation behavior, but worth flagging for future similar tests.

## Deferred

- **Connection-pool sizing review for `_assert_portfolio_breaker_ok`.** Each BUY adapter
  call opens + closes a `SessionLocal`. BUY entries are not hot-loop (autotrader fires
  on alert events, not per-tick) so the pool pressure is negligible in practice. Plan
  §10.1 noted this; Cowork nit (b) asked for an inline comment which I added at the
  helper definition. No active concern.
- **Cross-venue inconsistency between gate-blocked log lines.** Coinbase envelopes use
  `error="portfolio_breaker:..."`; Robinhood envelopes use the same prefix. Both grep
  identically. Consistent.
- **Backfilling historical `portfolio_breaker_shadow` rows.** Brief out-of-scope; the
  7-day soak begins when an operator first flips `chili_portfolio_dd_breaker_enabled=True`
  in shadow mode.

## Open questions for Cowork

None blocking. The user_id-semantic fix above is the only material deviation; it's
architecturally correct as committed and the runbook explicitly documents the
account-wide scope. If Cowork prefers an explicit `user_id` resolution at the venue
boundary (e.g. settings-sourced primary user) over `(:uid IS NULL OR ...)`, that's a
clean follow-up brief — the helper signature stays compatible.

## State of the breaker post-task

Both flags default OFF. The pattern tier is unchanged in behavior (the rename is alias-
backed). The portfolio tier exists, has tests, has a runbook, and is dormant. An
operator now has the full path: flip `chili_portfolio_dd_breaker_enabled=True` →
observe shadow rows / log lines for 7 days → flip `chili_portfolio_dd_breaker_live=True`
→ tier is armed.

The 2026-05-15 quant audit's open loop ("CHILI account drains through no_pattern
channels the pattern breaker cannot see") now has a closeable mechanism.
