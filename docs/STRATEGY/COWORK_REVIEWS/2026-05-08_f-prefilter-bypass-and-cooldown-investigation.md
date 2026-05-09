# COWORK_REVIEW: f-prefilter-bypass-and-cooldown-investigation

**Verdict:** FIXED. Live verification on broker-sync-worker
2026-05-09 03:21–03:23 UTC shows ADA-USD now correctly skipped
with `reason=venue_unsupported_crypto_path`. The crash loop is
gone. All three defense layers verified working across all four
containers.

## Algo-trader lens

The failure mode tonight was subtle: the IndexError NEVER ESCAPED
`broker_service.place_sell_stop_loss_order`. It was caught
internally and packaged as a normal-looking
`{"ok": False, "error": "list index out of range"}`. The bracket
writer's exception cooldown was wired to fire on exceptions that
ESCAPED — and the IndexError didn't escape, so the cooldown
never armed. Every 60s sweep re-fired the same crash with no
backoff.

CC's actual fix is excellent because it doesn't depend on which
upstream layer is current:

* **Broker-layer backstop**: refuses crypto bases BEFORE the
  try/except so the equity SDK primitive can't reach the SDK at
  all.
* **Code-bug detector**: matches IndexError/TypeError/etc.
  signatures in the broker's swallowed-error string and arms the
  cooldown anyway.
* **Existing line-1037 prefilter**: unchanged, still in place.

Defense in depth means even if one layer is reverted, missed by a
deploy, or stale-cached, the next layer catches.

## Dev-architect lens

Three notable choices:

1. **Conservative `_CODE_BUG_ERROR_PATTERNS`** — only Python
   exception class names + the canonical IndexError text + the
   new backstop's error string. Generic words like "error" or
   "fail" are NOT matched (they'd false-positive on legitimate
   broker rejects). Demonstrated by the test
   `test_genuine_broker_reject_does_not_arm_exception_cooldown`.

2. **Audit field added (`code_bug_cooldown_armed`)** to the
   audit event's `extra` JSON, parallel to `terminal_reject`. Ops
   can grep cooldown engagement events. This is the kind of
   observability hook that yesterday's flow lacked.

3. **Three full-chain integration tests** that exercise the
   actual call path, not just helper functions. This is the
   verification I begged for in tonight's earlier review. CC
   delivered exactly that.

## Live verification

Pre-fix (02:50–02:53 UTC) — same crash every minute:
```
[broker] SELL_STOP exception for ADA: list index out of range
```

Post-fix (03:21–03:23 UTC) — clean SKIP every cycle:
```
[bracket_writer_g2] place_missing_stop SKIPPED intent=237 ticker=ADA-USD
   reason=venue_unsupported_crypto_path
[bracket_reconciliation_ops] writer=place_missing_stop ok=false
   reason=venue_unsupported_crypto_path
```

Direct probe of broker_service across containers:
```
chili-1 / autotrader-worker-1 / broker-sync-worker-1 / scheduler-worker-1:
  place_sell_stop_loss_order('ADA', 100, trigger_price=0.25)
  -> {'ok': False, 'error': 'crypto_ticker_unsupported_via_equity_primitive'}
```

`_is_code_bug_error` matrix:
- "list index out of range" → True ✓
- "Not enough shares to sell" → False ✓ (no false-positive on real broker rejects)

## Lesson nailed

The "tests-pass-but-system-fails" theme that haunted today's three
fixes (Phase E false-cancel, the prior crash-fix bypass, the
cooldown-not-engaging) is closed by this brief's three full-chain
integration tests. Every fix that depends on a runtime code path
should have a test that exercises that path end-to-end, not just
the helper in isolation. That's the discipline I should have
insisted on for Phase E.

## What's left

The wipeout-cascade chain is now closed at the equity book
(Phases A+B+C+D from earlier today) and Phase E is reverted.
Crypto-side reconcile remains the architectural hole — the
`f-crypto-reconcile-architectural-rebuild` 4-phase brief stays
queued for tomorrow's fresh-start work.

ADA's stops still aren't placed (the equity primitive is wrong
for crypto), but the system now CLEANLY SKIPS instead of crashing.
A future brief wires `rh.crypto.order_*` for actual crypto-native
stop placement.

## Closing the night

- Phase E revert: clean
- Bracket_writer crash loop: fixed
- 14 crypto positions: 12 still open at broker (2 closed at target — DOT, SOL — with +$99.55 realized)
- Drawdown breaker: clean
- RH auth: alive (operator's manual reconnect)
- All scripts, briefs, memory updates, COWORK_REVIEWS committed and pushed

Real-money state secured. ADA crash loop dead. Tomorrow's first
NEXT_TASK will be the architectural rebuild Phase 1 with
integration-verification baked in as a hard acceptance criterion.

Stopping for the night.
