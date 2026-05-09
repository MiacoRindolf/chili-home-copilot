# f-prefilter-bypass-and-cooldown-investigation

STATUS: QUEUED
SLUG: prefilter-bypass-and-cooldown-investigation
PROPOSED: 2026-05-08
SEVERITY: medium (ADA crash loop continues post-fix; not actively losing money but burning RH API budget every 60s + cluttering audit trail)

## TL;DR

Tonight's fix (`f-phase-e-revert-and-bracket-writer-crash-fix`,
commits `1497c1e` revert + `3be20ea` patch) reverted Phase E
correctly but the bracket_writer crash-fix half doesn't take in
the running system. Live observation 2026-05-09 02:50–02:53 UTC
post-restart shows ADA-USD continuing to fire `[broker] SELL_STOP
exception for ADA: list index out of range` every minute,
identical to the pre-fix behaviour.

CC's tests pass (9/9), the new prefilter at line 1037 of
`bracket_writer_g2.py` IS in the loaded module
(`inspect.getsource()` confirms), and the .pyc is fresh — yet ADA
bypasses the prefilter AND the new exception cooldown.

This brief is a focused diagnostic: trace WHY ADA reaches the
broker call when TRUMP correctly skips, and fix the bypass.

## Audit fingerprint

```
2026-05-09 02:50:24 [broker] SELL_STOP submitting: ticker=ADA qty=3621.0 stopPrice=0.2566
2026-05-09 02:50:24 [broker] SELL_STOP exception for ADA: list index out of range
2026-05-09 02:50:24 [bracket_writer_g2] place_missing_stop broker error intent=237: list index out of range
2026-05-09 02:50:24 [bracket_reconciliation_ops] writer=place_missing_stop ok=false reason=place_failed
[... same pattern at 02:51, 02:52, 02:53, ...]
```

Compare to TRUMP-USD which IS correctly skipped:
```
2026-05-09 02:52:21 [bracket_writer_g2] place_missing_stop SKIPPED intent=235 ticker=TRUMP-USD
                     reason=venue_unsupported_crypto base=TRUMP (Robinhood does not trade this crypto pair; static whitelist)
```

Note TRUMP's reason is `venue_unsupported_crypto` (the OLD
whitelist filter, no `_path` suffix). The NEW prefilter reason
`venue_unsupported_crypto_path` is what we're trying to make ADA
hit, but isn't appearing in any log line.

## Hypotheses

### H1: Earlier exit before reaching line 1037

Some early-return between `decision.kind != 'missing_stop'` (line
943) and the new prefilter (line 1037) is firing for ADA but not
for TRUMP. Possibly:
- The `(broker_source or "").lower() not in _SUPPORTED_VENUES`
  check at line 997 — but ADA is robinhood-source so that should
  pass.
- `local_quantity <= 0` check at 1003 — ADA is 3621.
- `stop_price <= 0` check at 1008 — ADA is 0.25663137.

But if any of these returned early with `reason=invalid_decision`
or `reason=unsupported_venue`, the broker call would NOT happen.
The broker call IS happening for ADA. So the function reaches
PAST these checks AND past the new prefilter — somehow.

### H2: A second entry point that doesn't have the new prefilter

Maybe `place_missing_stop` is called from multiple sites, and
ONE of them bypasses the prefilter via a different code path.
But reading the function source at lines 910-1100 doesn't show
any branch that conditionally skips the prefilter.

### H3: The function in the running module is somehow different

`inspect.getsource(place_missing_stop)` returned the new code
including the prefilter. But maybe `place_missing_stop` is
re-exported or wrapped somewhere else (`__init__.py`?) and the
wrapper bypasses?

### H4: The prefilter check evaluates falsy somehow

`_t_upper = (ticker or "").upper()` then `_t_upper.endswith("-USD")`.
If `ticker` somehow arrived as `'ADA'` (without `-USD` suffix),
the check would fail to match. BUT the bracket_reconciliation log
shows `ticker=ADA-USD`. Unless something strips the suffix between
the reconciler and `place_missing_stop`.

Actually — looking at the broker log line `[broker] stop_price
rounded to broker tick: ticker=ADA 0.25663137 -> 0.2566` — the
broker function logs `ticker=ADA` (no `-USD`). So somewhere between
`place_missing_stop` and the broker call, the ticker has its `-USD`
stripped. If the prefilter runs AFTER this stripping, it would pass
ADA through.

**This is probably the bug**: `place_missing_stop` calls
`adapter.place_stop_loss_sell_order` which calls
`broker_service.place_sell_stop_loss_order` which strips `-USD`
from the ticker before the call. The prefilter would catch
"ADA-USD" but the broker function sees "ADA". Maybe there's an
intermediate function that strips first, then re-enters the
function chain?

Or simpler: the prefilter at line 1037 IS a string-end-check, but
maybe `decision.kind` is `'missing_stop_replacement'` or similar
that the H1 check at line 943 catches AND returns early — but for
some reason, ADA's flow re-enters at a later point that bypasses
the prefilter.

### H5: A separate `place_missing_stop_replacement` or similar variant

The codebase has `place_missing_stop_replacement` at
bracket_writer_g2.py around line 870. If THAT function is called
from `cancelled_limit_replacement_candidate` path and doesn't
have the prefilter, ADA would route through it. Worth checking.

## Goal

1. Find the actual code path ADA takes from
   `bracket_reconciliation_service` to the broker `SELL_STOP submit`
   that bypasses the new prefilter. Use a real chili_test fixture
   matching ADA's audit shape.
2. Verify whether the new exception cooldown at line 954 is
   engaging (or not, and why). The cooldown gate is at the very
   start of `place_missing_stop`; if ADA reaches the broker call
   on every cycle, the cooldown is NOT being armed correctly when
   the exception fires.
3. Add an integration test that exercises the live path with the
   ADA fixture, asserting (a) prefilter catches it (returns
   `reason=venue_unsupported_crypto_path`) AND (b) the exception
   cooldown engages on a simulated downstream IndexError.

## Acceptance criteria

1. Diagnostic complete: identify which code path bypasses the
   prefilter for ADA. Document in CC report.
2. Fix the bypass. Either patch the bypassing code path to also
   route through the prefilter, OR add a SECOND prefilter at the
   bypassing entry point.
3. Verify the new exception cooldown ACTUALLY engages: arm the
   cooldown via a single direct call with a mocked broker that
   raises, then verify a subsequent call returns
   `reason=in_exception_cooldown` without invoking the broker.
4. Add the integration test (LIVE path) — `tests/test_bracket_writer_crash_loop_repro.py`
   or extend the existing resilience test file. The test must
   trigger the IndexError via the actual call chain, not by
   directly calling the bottom function.
5. Live verification post-deploy: watch ADA's bracket_writer for
   5 min. Either real stop placed (prefilter triggered correctly
   would log `venue_unsupported_crypto_path`) OR a clean
   `in_exception_cooldown` SKIP after the first crash.
6. CC report at
   `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-prefilter-bypass-and-cooldown-investigation.md`.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/bracket_writer_g2.py` — the new prefilter
  + cooldown stay; add bypass-path patches.
- `app/services/trading/bracket_reconciliation_service.py` — the
  caller. Audit for any other entry to `place_missing_stop` or
  variants.
- `app/services/broker_service.place_sell_stop_loss_order` —
  consider adding a defensive crypto check at THIS layer too as
  a backstop (defence in depth: even if the upstream prefilter
  is bypassed, the broker function refuses crypto tickers).

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Don't revert tonight's commits.** The Phase E removal is correct
  and the cooldown helpers are correct; we just need to fix the
  bypass.
- **Edit-tool truncation discipline (HARD).**
- **Tests use `_test`-suffixed DB.**

## Out of scope

- Architectural rebuild Phase 1 (auth liveness). Separate brief.
- Any of the other queued briefs.
- Wiring crypto-native stop-loss primitive (would let ADA
  actually place stops via `rh.crypto.order_*`). Out of scope —
  separate brief; tonight's brief just makes ADA cleanly skip
  instead of crashing.

## Sequencing

1. Truncation scan.
2. Direct repro: write a Python script that calls
   `place_missing_stop` with ADA's exact arguments and observes
   the return value. Compare to what the live path produces. If
   direct repro returns `venue_unsupported_crypto_path` but the
   live path crashes, there's a different entry point.
3. Trace the live path: add a debug log line at every branch
   point in `place_missing_stop` and one cycle of bracket
   reconciliation will reveal which branch ADA takes.
4. Fix the bypass.
5. Add the integration test.
6. Verify the cooldown engagement separately.
7. Commit + push + CC report.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker
   scheduler-worker broker-sync-worker`.
3. Watch ADA's bracket_writer activity for 5 min. Expected:
   either NO `[broker] SELL_STOP exception for ADA` lines OR ONE
   crash followed by `in_exception_cooldown` SKIP messages for
   the next ~5 min.

## Rollback plan

`git revert` the commit. The bypass continues; same as the current
state.

## Open question

If the IndexError is genuinely deep in third-party `robin_stocks`
code, a defensive check inside `broker_service.place_sell_stop_loss_order`
(refusing crypto tickers up front) is a SECOND line of defence
worth adding regardless. Belt + suspenders.
