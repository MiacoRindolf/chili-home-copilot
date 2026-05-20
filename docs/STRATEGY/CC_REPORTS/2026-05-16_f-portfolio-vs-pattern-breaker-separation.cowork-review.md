# Cowork review: f-portfolio-vs-pattern-breaker-separation

Reviewed by Cowork scheduled-task at 2026-05-16T20:54Z.

## Verdict: APPROVED (autonomous)

Both the original execute session and the continuation session shipped the two-tier
drawdown breaker per the APPROVED plan. The CC_REPORT contains no negative-trigger
words (WARN/FAIL/regression/STOP/ABORT/halt/parity break/hard gate failed) — all
verifications PASS, all defaults OFF, all hard rules upheld.

## What landed at HEAD

Six commits (`d6ddbe8`, `6259912`, `d1bdb18`, `4f11100`, `3d6bf9a`, `864eca8`) realize
the rename + helpers + venue gates + tests + runbook chain. Pattern-tier behavior is
unchanged via `AliasChoices` legacy env-var alias. Portfolio tier is dormant behind
three default-OFF flags. Migrations: none (shadow-log reuses `trading_risk_state`).

## Tests

6/6 PASSED in `TestPortfolioBreakerSeparation` (pytest 9 / `-p no:asyncio`):
trip-on-aggregate, no-trip-no-offsets, post-rename pattern tier still works, venue
short-circuit (Coinbase mock untouched), pattern-tripped-attributed-vs-portfolio,
legacy alias compat. Test 7.4 confirmed gate short-circuits before the broker mock
is touched.

## In-flight discovery I'm signing off on

CC self-discovered + fixed the `user_id=None` Postgres NULL semantic gap (commit
`4f11100`): the venue gate has no per-user context, so `WHERE user_id = :uid`
returned zero rows under default `user_id=None`. The fix `(:uid IS NULL OR user_id
= :uid)` is architecturally consistent with CHILI's single-broker-account-per-
household scope. CC flagged the multi-account edge case for future review — saved
to follow-up backlog.

## Open items the operator should know about

1. **Promotion path is in operator hands**: flip `CHILI_PORTFOLIO_DD_BREAKER_ENABLED
   =true` → observe `trading_risk_state` rows with `regime='portfolio_breaker_shadow'`
   for 7 days → if shadow-log decisions look correct, flip
   `CHILI_PORTFOLIO_DD_BREAKER_LIVE=true` to arm.
2. **`git push`** of the 6-commit chain — operator's call.
3. **Legacy alias retirement**: `CHILI_MONTHLY_DD_BREAKER_ENABLED` works for one
   release. Migrate `.env` files to `CHILI_PATTERN_DD_BREAKER_ENABLED` before the
   alias is removed in a follow-up.
4. **Working-tree residue** (~30 M files visible via `git status`): pre-existing
   uncommitted WIP from prior sessions (Phase A canonical-outcome-layer + fast-path
   WIP + other CC report edits). NOT scope-drift for this session — the six-commit
   chain landed cleanly on top of pre-existing residue.

## Follow-ups already queued by CC

- `f-pytest-asyncio-env-pin` — still queued from predecessor task to fix local
  self-verification under pytest 9.0.
- `f-launcher-hang-investigation` — second session in a row where the daemon's
  `WaitForExit` hung post-work after a clean claude.exe exit.
- Multi-broker-account user_id-resolution refactor — only relevant if CHILI ever
  becomes multi-broker; today's scope is correct as-committed.

-- Cowork (autonomous, scheduled-task)
