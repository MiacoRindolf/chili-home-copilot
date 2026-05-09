# COWORK_REVIEW: f-phase-e-revert-and-bracket-writer-crash-fix

**Verdict: PARTIAL.** Phase E revert is clean and verified — Phase E
source is gone, migration 234 retained, ImportError on
`run_crypto_stale_trade_close` confirms removal. But the
bracket_writer crash fix is **not actually preventing the IndexError
from firing on ADA-USD**. Live verification post-deploy shows the
same crash repeating every minute at 02:50/02:51/02:52/02:53 UTC.

## Algo-trader lens

The Phase E revert closes the door on the false-cancel bug class.
That's a real win: the dangerous code is gone, no env-flag
re-enable footgun. Net positive for code health.

The crash-fix half is the worry. **ADA-USD's bracket reconciler is
still spending its retry budget on a guaranteed-fail call to the
broker.** The new prefilter (line 1037, `endswith("-USD")`) IS in
the loaded module — confirmed via `inspect.getsource()` — but
ADA's call path through `place_missing_stop` skips it somehow and
reaches `place_sell_stop_loss_order`. The new exception cooldown
also isn't engaging; every cycle re-fires the same crash without
an `in_exception_cooldown` SKIP message ever surfacing.

Real-money implication: same as before the fix. ADA's bracket
reconciler is wasting RH API calls every 60s. It's not actively
LOSING money (RH rejects each call), but it's cluttering the
audit trail and burning rate-limit budget. The 12 crypto positions
still have no working broker stops/targets — that's the
architectural gap from earlier today, not affected by tonight's
work.

## Dev-architect lens

CC's three notable choices:

1. **Migration 234 retention** with re-added registry entry. Right
   call — keeps `schema_version` consistent with deployed DBs.
2. **Crypto refuse broader than originally specified** (all `-USD`
   tickers, not just unsupported bases). Architecturally correct
   reasoning: the equity instruments API is wrong for ALL crypto.
3. **`venue_unsupported_crypto_path` reason name** (with `_path`
   suffix) chosen to distinguish from the old whitelist filter
   (`venue_unsupported_crypto`). Helps diagnose which gate
   triggered — useful and well-thought.

What surprised me (negatively):

* **The fix doesn't take in-the-running-system.** All three
  containers loaded the new module (line count matches, source
  code grep confirms the new prefilter at line 1037, .pyc is
  fresh from 02:43 UTC). But ADA still bypasses the prefilter
  AND the cooldown. There's a code path or condition I haven't
  traced. CC's tests passed (9/9) but the live-system observation
  shows different behaviour.

* **TRUMP works, ADA doesn't.** Both end with `-USD`. Both go
  through the same function. TRUMP gets caught by the OLD
  whitelist filter (`_is_crypto_supported_on_robinhood`) at
  some line BEFORE 1037. ADA passes that older filter (it's
  on the supported list) and should then hit the new prefilter
  at 1037 — but doesn't.

## Verification gap

The CC report's acceptance criterion 5 says:
> Live verification: After deploy, watch ADA's
> `g2_place_missing_stop_submitting` events for 10 min. Either
> successfully places a stop OR fails with a meaningful broker-error
> reason (not "list index out of range").

The actual post-deploy observation: ADA still fails with "list index
out of range" on every cycle. CC's tests passed but the integration
behaviour didn't match. This is the *third* instance of the
"tests-not-AST" lesson today: code paths can be correct in
isolation but wrong in the live system.

Suspect: there's an EARLIER call path or guard inside
`place_missing_stop` that the function takes for ADA which bypasses
the new prefilter. Possibilities I haven't traced:
- An override that re-enters the broker call from a recovery
  branch
- A second `place_missing_stop` import I missed
- Conditional logic where ADA passes the early checks but takes a
  different branch than I read

## What's left

### Immediate

- **Phase E source is gone** — that goal achieved.
- **ADA crash loop continues** — operator should mute the
  log noise OR disable `chili_bracket_writer_g2_place_missing_stop`
  temporarily until the prefilter bypass is debugged.

### Queued follow-up

`f-prefilter-bypass-and-cooldown-investigation`: a focused
diagnostic brief to find and fix:
1. Why `place_missing_stop`'s line 1037 prefilter isn't catching
   ADA (despite being in the loaded module).
2. Why the new exception cooldown at line 954 isn't engaging
   (every cycle re-fires the same crash; cooldown should arm
   on the FIRST exception).
3. Add a test that exercises the LIVE call path with a bracket
   intent fixture matching ADA's shape; current 9 tests pass
   but the live behaviour fails.

### Larger follow-ups (queued earlier today)

- `f-crypto-reconcile-architectural-rebuild` (Phase 1 = auth
  liveness) — the multi-week structural fix.
- `f-pattern-demote-sweep-wiring-fix` — small, can ship anytime.

## Final note

I called this an "algo-trader-architect" surgical fix and shipped
it overconfidently. The revert worked; the crash fix did not. The
issue isn't CC's choice (the prefilter and cooldown logic look
right in isolation), it's that I didn't insist on a live-fixture
verification test BEFORE committing. The CC report's "9/9 tests
PASS" was true but didn't capture the production behaviour. Same
class of mistake as Phase E earlier — trusting tests-as-evidence
without integration verification.

I'm stopping here for the night. The operator's positions are no
worse off than before this fix; only the log noise persists. The
follow-up brief I'm queuing should be the FIRST thing tomorrow
morning before the larger architectural work.
