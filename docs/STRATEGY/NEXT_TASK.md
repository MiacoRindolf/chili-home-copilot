# NEXT_TASK: f-trump-usd-poisoned-quote-source-audit

STATUS: DONE

**Promoted from `docs/STRATEGY/QUEUED/f-trump-usd-poisoned-quote-source-audit.md` on 2026-05-07 11:15 UTC after the prior thread shipped the implausible-quote-guard chain (`f-fix-implausible-quote-vs-exit_now-ordering` + `f-exit-monitor-quote-guard-unification`). Defense layer is now complete across all three exit lanes (equity / crypto / options); this brief closes the loop by fixing the upstream cache that's still emitting the poisoned value.**

**Why this is next**: with the guards in place, the system safely refuses to act on the bogus `$0.0003` quote — but the bad value still streams in every cycle, polluting `trading_stop_decisions` with `DATA_IMPLAUSIBLE` rows and bumping the new `skipped_implausible_quote` counter on every monitor pass. The TRUMP-USD storm has been observed for 24+ hours with the same value to four decimal places. That's a stale singleton-cache fingerprint, not transient noise. Identify and clear the cache.

## Goal

Identify which upstream quote source is returning `price=$0.0003` for `TRUMP-USD` (real Coinbase ground truth at investigation time was `$2.37`), and fix the source. The Round-13/14 implausible-quote guards (now uniformly across all three exit lanes via `_exit_monitor_common.is_implausible_quote`) correctly refuse to act on the bad value, but every monitor pass still writes a `DATA_IMPLAUSIBLE` row to `trading_stop_decisions`. Forensic evidence shows the same `price=0.0003` repeating across 24+ hours of decisions, every ~14 minutes, identical to four decimal places — a CACHED bogus value, not transient noise.

## Why now

1. The implausible-quote guard is a defense, not a fix. Anything else in the trading brain that reads the same quote-source without the guard (UI surfaces, alert formatters, downstream features computed off price) will silently pollute its output. The pattern-monitor's `exit_now` recommendation for TRUMP-USD on 2026-05-06 was based on the LLM seeing `price=$2.39` (a healthy quote) — so we already know two paths read DIFFERENT quotes, and one of those paths is poisoned.

2. This is the second crypto-cache-pollution incident with the same shape. Round-13 originated when ARB-USD trade 585 was force-closed at `px=0.00075706` vs entry $0.1295 because the upstream quote provider returned a bad value. The guard was added to prevent re-triggering, but the upstream-bad-value pattern is recurrent.

3. It is a fingerprint: identical bad value, repeated, suggests a stale entry in a singleton cache that was never invalidated. That is a broader hygiene concern across the price-bus / Massive WS / Massive REST stack.

4. Defense-only is operationally costly. Every monitor pass writes a stop_decision row + bumps the counter; that's noise for forensics and budget for the DB.

## Suspected source ranking

`market_data.fetch_quote(ticker)` walks (in order, 1st hit wins):
1. **price_bus** — unified WS cache via `price_bus.get_live_quote(ticker)` (line 696)
2. **Massive WS cache** — `_massive.get_ws_quote(ticker)` (line 727)
3. **Massive REST** — `_massive.get_last_quote(ticker)` (line 748)
4. **Polygon** (line 770) — equities only
5. **yfinance** — fallback

The implausible value (`$0.0003`) being **stable across 24+ hours and across `evaluate_trade` cadence** strongly implicates 1 or 2 (a cached entry that nobody invalidated). REST would generally return a fresh value or fail, not a stale day-old one. Coinbase ground truth at any sample time during the storm has been `$2.30-$2.40`, so neither WS nor REST should naturally return $0.0003 for ANY recent timestamp.

Most likely root causes (by prior probability):
1. **Massive WS subscribed to a stale or wrong-symbol pair** — TRUMP-USD ambiguity (e.g., `OFFICIAL TRUMP / SOLANA` listed at low precision) returning a fragment of an order book at the wrong scale.
2. **price_bus cached a malformed broadcast on first connect** — TLS failure / partial frame returned a near-zero price; cache TTL absent or too long.
3. **Symbol normalization bug** — `TRUMP-USD` resolved to a different upstream symbol that exists at a different price.
4. **Massive REST returning $0.0003 with a stale `last_price` field** — possible if the provider serves cached quotes during a Massive-side incident; check `runtime_surface_state` for `market_data.state='degraded'` rows in the storm window.

## Phase 1 — Diagnose (READ-ONLY)

**Phase 1.1 — Reproduce the read.** Write a one-shot diagnostic script `scripts/dispatch-trump-quote-trace.ps1` that:
- Imports `app.services.trading.market_data` inside `chili-env`.
- Logs the COMPLETE return dict from `fetch_quote("TRUMP-USD")` including the `source` field.
- Hits each upstream in isolation: `price_bus.get_live_quote("TRUMP-USD")`, `_massive.get_ws_quote("TRUMP-USD")`, `_massive.get_last_quote("TRUMP-USD")`. Logs raw response from each.
- Hits Coinbase public ticker `https://api.exchange.coinbase.com/products/TRUMP-USD/ticker` for ground truth.
- Writes results to `scripts/dispatch-trump-quote-trace-output.txt`.

Acceptance: the script runs without auth and shows which of the four sources returns the bad value, what it actually returns, and how it differs from Coinbase.

**Phase 1.2 — History check.** Query `trading_stop_decisions` for ALL `DATA_IMPLAUSIBLE` rows in the last 30 days (any ticker, any trade), grouped by `(ticker, trigger_price_from_reason)`. The reason string carries `price=$X.XXXX entry=$Y.YYYY` — extract via regex. Also check the new `skipped_implausible_quote` counter from the equity lane (added in `f-exit-monitor-quote-guard-unification`) for additional evidence. Goal: see whether this is a TRUMP-only outage, a recurring crypto-symbol class issue, or a broader cache-pollution incident. Write findings to `docs/AUDITS/2026-05-07_implausible-quote-history.md`.

Acceptance: a table with one row per `(ticker, bad_price)` pair showing how often each fired and over what window. Identify whether the value is identical across rows (one cached entry) or varies (dynamic upstream issue).

## Phase 2 — Fix the source

Branch on what Phase 1.1 finds:

**If price_bus is the cache holding the bad value:**
- Add a TTL invalidation: any cached quote older than 60s is dropped on next read.
- Add a sanity guard at write-time: never cache a quote where `price < 0.0001` for tickers with prior known-good price > $1. The guard should LOG and SKIP the write, not silently substitute.
- Lean on the existing `is_implausible_quote(px, prior_known_good)` helper from `_exit_monitor_common.py` rather than introducing a parallel threshold. Reuse the same 0.1x/10x bounds.
- Add a test in `tests/test_price_bus_sanity_guard.py` covering the bad-write rejection.

**If Massive WS is returning the bad value:**
- Audit symbol normalization in `_massive.get_ws_quote`. If TRUMP-USD is being resolved to a different upstream symbol (e.g., low-supply tokens), surface the resolved symbol in the response payload.
- Add a periodic re-subscribe at the WS layer to recover from torn frames.
- File a vendor ticket with Massive if their ground-truth response is wrong; cite the timestamp and resolved symbol.

**If Massive REST is returning the bad value:**
- Check `runtime_surface_state` for `market_data.state='degraded'` rows; if Massive flagged itself, add a fallback skip.
- If Massive is healthy but returning bad data, that's a vendor escalation. Stage a yfinance fallback for crypto when Massive returns implausible values.

In ALL branches:
- Apply the implausible-quote guard at the `fetch_quote` boundary, not just at the exit-monitor consumers. Per the no-hardcoded-fallback rule, the guard returns `None` (not a substitute price) when the quote is implausible relative to the most recent fresh good quote. This forces every consumer to handle the absence rather than acting on poison.
- Use the shared `is_implausible_quote` helper — do not introduce a fourth threshold.

## Phase 3 — Add a runtime monitor

`runtime_surface_state.market_data` already exists. Extend it: when an implausible-quote rejection fires at the `fetch_quote` boundary, write a `degraded` row with the bad value and source. After 5 consecutive rejections within 10 minutes for the same `(ticker, source)`, escalate via the existing alert pipeline. This catches the next poisoned-cache incident in real time rather than via a forensic look-back.

Acceptance: a unit test in `tests/test_market_data_implausible_alert.py` simulating 5 bad reads in window and asserting an alert is queued.

## Phase 4 — Postmortem doc

Write `docs/AUDITS/2026-05-07_trump-usd-poisoned-quote-postmortem.md` capturing:
- Timeline (first observation 2026-05-06 ~02:42 UTC per the original `trading_stop_decisions` rows; storm ongoing through 2026-05-07; fix-source-identified date; fix-shipped date).
- Root cause (whichever upstream Phase 1.1 names).
- Why the implausible-quote guard caught it but didn't surface it operationally before the unification work added `skipped_implausible_quote` to the equity lane summary.
- Why no other consumer was visibly affected during the window (the LLM pattern-monitor reads via a different code path; document which one to ensure that path stays clean post-fix).
- Generalization: the guard is a backstop. The next quote-cache issue might present at a ratio JUST under the 0.1-10x threshold and slip through. Consider tightening or making the threshold ticker-specific (lifetime-realized-vol-scaled).

## Open questions

1. **Is the bogus value `$0.0003` IDENTICAL across all DATA_IMPLAUSIBLE rows for TRUMP-USD?** If yes, it's one cached entry. If it varies, the upstream is dynamically returning bad values and the diagnosis branches differently. Phase 1.2 query answers this directly.

2. **Was there a Massive-side incident around the time the storm started (2026-05-06 ~02:42 UTC)?** Phase 1.2 history narrows the start time; cross-reference Massive's status page if accessible.

3. **Does the LLM/pattern-monitor pipeline read price_bus directly or use a different cache?** Per the `pattern_monitor_decision.price_at_decision` rows for trade 1829 (range $2.34-$2.43 during the storm), the LLM was reading clean quotes. Need to confirm which path it uses to ensure that path stays clean post-fix.

4. **Should Phase 2's `fetch_quote`-boundary guard use `is_implausible_quote` directly or a wrapper that also handles the "first quote ever" case (no prior known-good)?** The shared helper requires `entry > 0` to evaluate ratio. For a brand-new ticker the first quote IS the prior known-good. Need a small adapter: maintain a per-ticker last-known-good in price_bus, falling back to `entry_price` from any open Trade, falling back to "accept as known-good" when there's no prior reference. Surface this adapter design in Phase 2 if Phase 1.1 implicates price_bus.

## Out of scope

- Replacing Massive as the primary crypto data source.
- Refactoring `market_data.fetch_quote`'s fallback chain.
- Adding new ticker-specific thresholds beyond the existing 0.1x-10x ratio.
- Tightening the threshold itself; that's a separate brief if Phase 4's postmortem argues for it.

## Acceptance bar

- Phase 1 diagnostic identifies the poisoned upstream by name (price_bus / Massive WS / Massive REST / something else).
- Phase 2 fix lands the implausible-quote guard at `fetch_quote` boundary AND clears the bad cache entry (ad-hoc clear via the script if cache is in-memory; restart of the relevant container if cache is process-local; vendor escalation if upstream).
- Phase 3 monitor surfaces future occurrences within 10 minutes via `runtime_surface_state`.
- Phase 4 postmortem filed.
- One unit test per phase that has new code.
- After deploy: zero new `DATA_IMPLAUSIBLE` rows for TRUMP-USD in `trading_stop_decisions` for 1 hour. (If the upstream is still emitting the bad value but `fetch_quote` now refuses at boundary, the rows stop because the exit monitor never sees the bad quote.)

## Operator-side after CC ships

- Clear the corrupted git index lock first: `Remove-Item C:\dev\chili-home-copilot\.git\index.lock` (currently blocking commits — `git status` returns `fatal: unable to read cf7133c5...` because the index is locked by an IDE process).
- Push the fix.
- Restart whichever container holds the poisoned cache (likely autotrader-worker or chili — Phase 2 names which).
- Run the diagnostic script from Phase 1.1 once more to confirm the upstream is now clean.
- Eyeball `trading_stop_decisions` for 1 hour: if zero new `DATA_IMPLAUSIBLE` rows for TRUMP-USD, the source is fixed. If the rows continue, the upstream is still bad but the boundary guard is shielding consumers — that's a partial win and the source-fix needs a vendor escalation.
