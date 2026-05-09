# NEXT_TASK: f-prefilter-bypass-and-cooldown-investigation

STATUS: PENDING

## Goal

Tonight's bracket_writer crash fix (commit `3be20ea`) shipped with
all tests passing but **doesn't take in the live system**. ADA-USD
continues to fire `[broker] SELL_STOP exception for ADA: list
index out of range` every minute post-restart, identical to
pre-fix. CC's new prefilter at line 1037 of `bracket_writer_g2.py`
IS in the loaded module — verified via `inspect.getsource()` —
but ADA bypasses it. The new exception cooldown also doesn't
engage.

This brief is a focused diagnostic + fix.

The full brief is at
`docs/STRATEGY/QUEUED/f-prefilter-bypass-and-cooldown-investigation.md`
— read it first.

## Why now

Real-money state unchanged from earlier today: the 12 open crypto
positions have no working broker stops/targets. ADA's crash loop is
log noise, not active capital loss — but it's burning RH API budget
and cluttering the audit trail every 60s. More importantly, this
indicates that tonight's fix has a hole that needs to close before
relying on the cooldown infrastructure for any future safety work.

## Audit fingerprint (post-deploy)

```
2026-05-09 02:50:24 UTC
  [broker] stop_price rounded to broker tick: ticker=ADA 0.25663137 -> 0.2566
  [broker] SELL_STOP submitting: ticker=ADA qty=3621.0 stopPrice=0.2566
  [broker] SELL_STOP exception for ADA: list index out of range
  [bracket_writer_g2] place_missing_stop broker error intent=237: list index out of range
  reason=place_failed

[same pattern at 02:51, 02:52, 02:53, ... every minute]
```

Compare to TRUMP-USD which IS correctly skipped:
```
[bracket_writer_g2] place_missing_stop SKIPPED intent=235 ticker=TRUMP-USD
   reason=venue_unsupported_crypto base=TRUMP (...static whitelist)
```

TRUMP's reason is `venue_unsupported_crypto` (the OLD whitelist
filter, no `_path` suffix). The NEW prefilter reason
`venue_unsupported_crypto_path` is what we expected ADA to hit but
isn't appearing.

## Hypotheses to test (in order)

1. **Earlier exit before line 1037 prefilter**. There's a code path
   between `decision.kind` check (line 943) and the new prefilter
   that returns early for ADA but still routes to a downstream
   broker call.
2. **Second entry point**. Maybe `place_missing_stop` is called
   from multiple sites and ONE bypasses the prefilter via a
   different code path.
3. **Ticker stripped before prefilter check**. The `[broker]` log
   shows `ticker=ADA` (no `-USD`), so something strips the suffix.
   If the prefilter runs after that stripping... actually the
   prefilter is INSIDE place_missing_stop which receives the full
   `ticker=ADA-USD`. But worth tracing.
4. **`place_missing_stop_replacement` variant**. There's a function
   at bracket_writer_g2.py around line 870 that may be a separate
   crypto entry point lacking the prefilter.

## The change

1. **Diagnostic step**: write a script that exercises the LIVE
   `place_missing_stop` path with ADA's audit fingerprint. Compare
   the result against the in-process direct-call result. If they
   differ, there's a different entry point.
2. **Fix the bypass**: either patch the bypassing code path to also
   route through the prefilter, OR add a second prefilter at the
   bypassing entry point.
3. **Belt-and-suspenders**: add a defensive crypto check inside
   `broker_service.place_sell_stop_loss_order` itself, so even if
   the upstream prefilter is bypassed, the broker function refuses
   crypto tickers up front. This is defence-in-depth.
4. **Verify exception cooldown**: write a test that arms the
   cooldown via a single direct call with a mocked broker that
   raises, then verify a subsequent call returns
   `reason=in_exception_cooldown` without invoking the broker.
5. **Integration test (LIVE path)**: trigger the full chain from
   `bracket_reconciliation_service` down to the broker call, and
   assert the prefilter fires.

## Acceptance criteria

1. Diagnostic complete: identify which code path bypasses the
   prefilter for ADA. Document in CC report.
2. Fix the bypass.
3. Defence-in-depth: `broker_service.place_sell_stop_loss_order`
   refuses crypto tickers up front (returns a meaningful
   error like `crypto_ticker_unsupported_via_equity_primitive`).
4. Verify the new exception cooldown ACTUALLY engages.
5. Integration test in
   `tests/test_bracket_writer_crash_loop_repro.py` — exercises
   the FULL call chain, not just the helper functions.
6. Live verification post-deploy: watch ADA's bracket_writer for
   5 min. Either real stop placed (prefilter triggered correctly
   logs `venue_unsupported_crypto_path` via the bypass path) OR a
   clean `in_exception_cooldown` SKIP after the first crash. NOT
   another `list index out of range` cycle.
7. CC report at
   `docs/STRATEGY/CC_REPORTS/2026-05-08_f-prefilter-bypass-and-cooldown-investigation.md`.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/bracket_writer_g2.py` — add bypass-path
  patches; the new prefilter and cooldown helpers stay.
- `app/services/trading/bracket_reconciliation_service.py` — audit
  for any other entry to `place_missing_stop` or its variants.
- `app/services/broker_service.place_sell_stop_loss_order` — add
  defensive crypto check at the BROKER layer too as a backstop.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Don't revert tonight's commits.** The Phase E removal and
  cooldown-helper code are correct; just need to fix the bypass.
- **Edit-tool truncation discipline (HARD).**
- **Tests use `_test`-suffixed DB.**
- **No magic numbers.**

## Out of scope

- Architectural rebuild Phase 1 (auth liveness).
- Wiring crypto-native stop-loss primitive (`rh.crypto.order_*`).
  Tonight's brief just makes ADA cleanly skip instead of crashing.
- Any of the other queued briefs.

## Sequencing

1. Truncation scan.
2. Diagnostic: direct-call repro vs live-path observation.
3. Fix the bypass.
4. Add belt-and-suspenders backstop in `broker_service`.
5. Verify exception cooldown engagement.
6. Add integration test (LIVE path).
7. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker
   scheduler-worker broker-sync-worker`.
3. Watch ADA's bracket_writer activity for 5 min. Expected: either
   NO `[broker] SELL_STOP exception for ADA` lines OR ONE crash
   followed by `in_exception_cooldown` SKIP messages for the next
   ~5 min.

## Rollback plan

`git revert` the commit. The bypass continues; same as the current
state.

## What CC should do if it's unsure

1. **If the bypass path can't be located via grep + log tracing**,
   add a `logger.debug` line at every branch point in
   `place_missing_stop` and observe ONE cycle. The branch ADA
   takes will be obvious.
2. **If the IndexError is genuinely deep in third-party
   `robin_stocks` code**, the broker_service backstop becomes the
   primary fix; the bracket_writer prefilter becomes
   defence-in-depth.
3. **If the integration test setup is too complex** (e.g., requires
   mocking the entire broker stack), surface in CC report and
   propose a smaller-scope test that exercises the
   bracket_reconciliation → bracket_writer → broker_service chain
   with a mock broker that raises.
