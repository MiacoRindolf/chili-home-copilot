# Cowork Review: bracket-intent-stop-price-live-sync

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-03_bracket-intent-stop-price-live-sync.md`
**Reviewer:** Cowork.
**Date:** 2026-05-03.

## Verdict

Clean diagnostic-driven fix. The CC author refuted my hypothesis #2, partially refuted #3 (real value is `authoritative`, not `shadow`), and confirmed #1 — the alert-event-gated call site at `stop_engine.py:893-896` was the actual root cause. They picked Option B over my brief's Option A lean for a reason I hadn't anticipated (`upsert_bracket_intent` calls `db.commit()` internally inside what I had assumed was a savepoint-safe path) — that's the right call.

8/8 new tests pass + 24/24 prior tests still pass. Live verification at 03:02:27 UTC produced 7 `sync_stop_price` log lines on the first sweep, exactly matching the pre-deploy gap probe direction and magnitude. All 12 open broker-backed trades aligned to `gap=0` within one sweep cycle. Approving.

## What I'd flag

- **Smart restart choice.** CC used `docker compose restart broker-sync-worker` (not `up -d --force-recreate`) to pick up code without picking up the operator's pending `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` env-var change in `docker-compose.yml`. Exactly the right discipline — they noticed the operator decision, didn't self-authorize the broker-side action, and left the flag pickup for the operator's chosen restart timing. That's the kind of judgment we want.

- **Minor framing correction in the report.** The CC author wrote "engine has been tightening stops on longs (trailing-stop behavior); mirror frozen at the wider entry-time value." This is reversed. For longs, lower `trade.stop_loss` than `bi.stop_price` means the engine moved the stop FURTHER below entry — looser protection, not tighter. The earlier conversation with the operator already established this (ATR-based recomputation widening as volatility expanded; not trailing-up behavior since `high_watermark` is null on most). The fix correctness is unaffected — sync brings them into alignment regardless of direction — but the diagnostic narrative would mislead a future reader. Worth a one-line correction in a follow-up if the CC author revisits.

- **Surprise #1 — `BRAIN_LIVE_BRACKETS_MODE=authoritative`, not `shadow`.** I had this tagged as `shadow` in the brief based on prior audit memory. Real current value is `authoritative`. Doesn't change the fix shape (same gating semantics), but worth knowing for future briefs and audits. Memory updated.

- **8 distinct writer paths to `trade.stop_loss`.** The diagnosis enumerated them (auto_trader, auto_trader_monitor, position_overrides, coinbase_service, pattern_position_monitor, portfolio, broker_position_sync). Any of those can move the engine's view without firing through `stop_engine.evaluate_trade`'s alert path. The unconditional sync at the wire site catches all of them — strictly better coverage than alert-only.

- **ZEC-USD `place_missing_stop` Tracebacks (Side-channel observation).** `list index out of range` from Robinhood's equity stop-loss API trying to find a `ZEC` instrument record. Pre-existing — predates this restart. Exactly the unsupported-crypto pre-filter case the original 2026-05-03 audit flagged as HIGH #4 and that I had queued as next-after-this. The traceback storm is the live cost of not having that filter. Confirms priority for the next task.

## Open Questions — answers

1. **`trading_stop_decisions` coverage gap.** Same root cause as the bracket_intent gating — `_record_stop_decision` also sits behind the alert-event gate. So when other writers move `trade.stop_loss` (which the CC found 8 paths for), no decision row is recorded. This is real and worth a follow-up. **Recommended next-after-unsupported-crypto:** `stop-decision-recording-coverage` — extend the unconditional-sync pattern to decision-row writes, OR teach the other writer paths to emit decision rows. Defer the choice to that task's diagnosis. Belongs after the unsupported-crypto pre-filter, not in this loop.

2. **`target_price` symmetric mirror?** YES eventually, NO right now. No live consumer reads `bi.target_price` for placement (no `place_missing_target` writer exists), so urgency is genuinely low. But for audit-clarity symmetry and so future writers don't repeat the stale-cache mistake, worth adding a parallel `sync_bracket_intent_target_from_trade` in the same writer module the next time it's natural. Don't do it as a standalone task; bundle into a future bracket task.

3. **`price_drift` tolerance after the fix.** Pre-fix, the classifier compared broker-side stop to a frozen entry-time number — so `price_drift` was almost always firing on positions whose engine view had moved at all. Post-fix, the comparison is broker-side vs engine-current. Same 25 bps default tolerance, but the semantic is now "broker is out of sync with engine," not "broker has drifted from entry." That's a tighter, more meaningful signal. **Don't tune the threshold** — let it run for a week and see whether the new signal generates actionable discrepancies or false-positive churn. Revisit only if it's noisy.

4. **ZEC-USD positive gap direction.** Out of scope here per the CC author. The unsupported-crypto pre-filter task naturally surfaces it (writer attempts ZEC against Robinhood equity API and fails). Don't chase separately.

## Practical implications for the operator

- **Worker restart timing for the cancel-covering-sell flag is unchanged.** The CC's `docker compose restart` brought in the new sync code but NOT the `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` env var. The operator's plan to wait for Monday post-13:30 UTC to flip the flag (via `docker compose up -d --force-recreate --no-deps broker-sync-worker`) still applies. The new sync code is live now and harmless; it just keeps the cache fresh.

- **Mirror is now self-healing.** If `trade.stop_loss` moves Monday morning (very likely as ATR recomputes on market open), `bi.stop_price` follows within one sweep cycle. So whenever the operator does flip the flag, `place_missing_stop` reads the engine's current view, not yesterday's. The manual one-shot resync SQL is no longer load-bearing — it's a relic of today's diagnostic, not a recurring operational tool.

- **One subtle behavior to expect on Monday.** When market opens 13:30 UTC and the engine recomputes ATR-based stops (the writer at `pattern_position_monitor.py:1029` or another path moves `trade.stop_loss`), the next sweep's `sync_stop_price` log line will fire for each affected ticker. That's normal post-fix behavior. The operator will see a one-time flurry of log lines on Monday's first few sweeps; steady state thereafter is silent except when stops actually move.

## Direction for next task

`audit-unsupported-crypto-prefilter` (audit's HIGH #4). Live evidence is now staring at us via the ZEC-USD traceback storm in the broker-sync-worker logs. Small fix scope: a venue-capability check before broker placement. Suggested approach in the brief I'll write next:

- Robinhood-supported-crypto whitelist (cached lookup or static table).
- Pre-broker-call filter in `auto_trader.py` (the `auto_trader.py:1064/1071` path that currently surfaces the `crypto_not_supported_on_robinhood` reason AFTER the broker round-trip).
- Same filter applied at `place_missing_stop`'s entry — refuse to even try equity stop-loss API on a crypto ticker without an instrument record.

After that: re-promote `f8b-verification-soak-3` (timing-gated to on-or-after 2026-05-04 16:30 UTC). The bracket loop is finally closed; we can return to the fast-path measurement program.

`CURRENT_PLAN.md` does not need rewriting. The plan's broader shape — prove edge before live activation, fast-path stays paper — is undisturbed.
