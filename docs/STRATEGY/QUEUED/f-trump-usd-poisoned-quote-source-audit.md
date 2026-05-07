# QUEUED TASK: f-trump-usd-poisoned-quote-source-audit (PROMOTED)

**Promoted to `docs/STRATEGY/NEXT_TASK.md` on 2026-05-07 11:15 UTC. The defense layer (implausible-quote guards across all three exit lanes) shipped via `f-fix-implausible-quote-vs-exit_now-ordering` + `f-exit-monitor-quote-guard-unification`; this brief closes the loop by fixing the upstream cache.**

The full brief content (with revisions reflecting the now-shared `_exit_monitor_common.is_implausible_quote` helper and the storm having continued for 24+ hours) now lives in `NEXT_TASK.md`. This file is preserved as a placeholder so the queue history stays linkable; do not edit. If the brief is ever re-queued, restore the body from `docs/STRATEGY/CC_REPORTS/<date>_f-trump-usd-poisoned-quote-source-audit.md` once it ships, or from git history.

---

The original body below is preserved verbatim for reference.

# QUEUED TASK: f-trump-usd-poisoned-quote-source-audit

**Originally surfaced during the live debug session on 2026-05-06 that fixed `f-crypto-exit-monitor-pattern-exit-now`. The implausible-quote guard at `stop_engine.py:436-443` is correctly refusing to act on a `$0.0003` quote for TRUMP-USD vs entry $2.4194, but the bogus quote keeps coming back every cycle. This brief tracks down which upstream is poisoned and fixes the source.**

**Promote to NEXT_TASK whenever the operator wants this off the watch list. Not blocking — the guard is doing its job. But the storm is filling logs and any other consumer that lacks the same guard could be misled.**

The body below is the complete brief.

---

# NEXT_TASK: f-trump-usd-poisoned-quote-source-audit

STATUS: PENDING

## Goal

Identify which upstream quote source is returning `price=$0.0003` for `TRUMP-USD` (real Coinbase ground truth at investigation time was `$2.37`), and fix the source. The Round-13/14 implausible-quote guard at `stop_engine.py:436-443` correctly refuses to act on the bad value, but every `evaluate_trade` pass for trade 1829 (and presumably any future TRUMP-USD position) writes a `DATA_IMPLAUSIBLE` row to `trading_stop_decisions`. Forensic evidence shows the same `price=0.0003` repeating across 9+ hours of decisions on 2026-05-06, every ~14 minutes, identical to four decimal places — meaning it is a CACHED bogus value, not transient noise.

## Why now

1. The implausible-quote guard is a defense, not a fix. Anything else in the trading brain that reads the same quote-source without the guard (UI surfaces, alert formatters, downstream features computed off price) will silently pollute its output. The 14:05 LLM `exit_now` recommendation was based on the LLM seeing `price=$2.39` (a healthy quote) — so we already know two paths read DIFFERENT quotes, and one of those paths is a poisoned cache.
2. This is the second crypto-cache-pollution incident with the same shape. Round-13 originated when ARB-USD trade 585 was force-closed at `px=0.00075706` vs entry $0.1295 because the upstream quote provider returned a bad value. The guard was added to prevent re-triggering, but the upstream-bad-value pattern is recurrent.
3. It is a fingerprint: identical bad value, repeated, suggests a stale entry in a singleton cache that was never invalidated. That is a broader hygiene concern across the price-bus / Massive WS / Massive REST stack.

## Suspected source ranking

`market_data.fetch_quote(ticker)` walks (in order, 1st hit wins):
1. **price_bus** — unified WS cache via `price_bus.get_live_quote(ticker)` (line 696)
2. **Massive WS cache** — `_massive.get_ws_quote(ticker)` (line 727)
3. **Massive REST** — `_massive.get_last_quote(ticker)` (line 748)
4. **Polygon** (line 770) — equities only
5. **yfinance** — fallback

The implausible value (`$0.0003`) being **stable across 9+ hours and across `evaluate_trade` cadence** strongly implicates 1 or 2 (a cached entry that nobody invalidated). REST would generally return a fresh value or fail, not a stale 9h-old one. Coinbase ground truth at any sample time during the storm was `$2.36-$2.40`, so neither WS nor REST should naturally return $0.0003 for ANY recent timestamp.

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

**Phase 1.2 — History check.** Query `trading_stop_decisions` for ALL `DATA_IMPLAUSIBLE` rows in the last 30 days (any ticker, any trade), grouped by `(ticker, trigger_price_from_reason)`. The reason string carries `price=$X.XXXX entry=$Y.YYYY` — extract via regex. Goal: see whether this is a TRUMP-only outage, a recurring crypto-symbol class issue, or a broader cache-pollution incident. Write findings to `docs/AUDITS/2026-05-XX_implausible-quote-history.md`.

Acceptance: a table with one row per `(ticker, bad_price)` pair showing how often each fired and over what window.

## Phase 2 — Fix the source

Branch on what Phase 1.1 finds:

**If price_bus is the cache holding the bad value:**
- Add a TTL invalidation: any cached quote older than 60s is dropped on next read.
- Add a sanity guard at write-time: never cache a quote where `price < 0.0001` for tickers with prior known-good price > $1. The guard should LOG and SKIP the write, not silently substitute.
- Add a test in `tests/test_price_bus_sanity_guard.py` covering the bad-write rejection.

**If Massive WS is returning the bad value:**
- Audit symbol normalization in `_massive.get_ws_quote`. If TRUMP-USD is being resolved to a different upstream symbol (e.g., low-supply tokens), surface the resolved symbol in the response payload.
- Add a periodic re-subscribe at the WS layer to recover from torn frames.
- File a vendor ticket with Massive if their ground-truth response is wrong; cite the timestamp and resolved symbol.

**If Massive REST is returning the bad value:**
- Check `runtime_surface_state` for `market_data.state='degraded'` rows; if Massive flagged itself, add a fallback skip.
- If Massive is healthy but returning bad data, that's a vendor escalation. Stage a yfinance fallback for crypto when Massive returns implausible values.

In ALL branches:
- Apply the implausible-quote guard at the `fetch_quote` boundary, not just at `stop_engine`. Per the no-hardcoded-fallback rule, the guard returns `None` (not a substitute price) when the quote is implausible relative to the most recent fresh good quote. This forces every consumer to handle the absence rather than acting on poison.

## Phase 3 — Add a runtime monitor

`runtime_surface_state.market_data` already exists. Extend it: when an implausible-quote rejection fires, write a `degraded` row with the bad value and source. After 5 consecutive rejections within 10 minutes for the same `(ticker, source)`, escalate via the existing alert pipeline. This catches the next poisoned-cache incident in real time rather than via a forensic look-back.

Acceptance: a unit test in `tests/test_market_data_implausible_alert.py` simulating 5 bad reads in window and asserting an alert is queued.

## Phase 4 — Postmortem doc

Write `docs/AUDITS/2026-05-XX_trump-usd-poisoned-quote-postmortem.md` capturing:
- Timeline (first observation, fix-source-identified, fix-shipped).
- Root cause (whichever upstream).
- Why the implausible-quote guard caught it but didn't surface it operationally.
- Why no other consumer was visibly affected during the window (was the LLM reading a different path?).
- Generalization: the guard is a backstop. The next quote-cache issue might present at a ratio JUST under the 0.1-10x threshold and slip through. Consider tightening or making the threshold ticker-specific (lifetime-realized-vol-scaled).

## Open questions

1. **Is the bogus value`$0.0003` IDENTICAL across all DATA_IMPLAUSIBLE rows for TRUMP-USD?** If yes, it's one cached entry. If it varies, the upstream is dynamically returning bad values and the diagnosis branches differently.
2. **Was there a Massive-side incident around the time the storm started?** Phase 1.2 history query will narrow the start time; cross-reference Massive's status page if accessible.
3. **Does the LLM/pattern-monitor pipeline read price_bus directly or use a different cache?** Per the `pattern_monitor_decision.price_at_decision` rows for trade 1829 (range $2.34-$2.43), the LLM is reading clean quotes. Need to confirm which path it uses to ensure that path stays clean post-fix.

## Out of scope

- Replacing Massive as the primary crypto data source.
- Refactoring `market_data.fetch_quote`'s fallback chain.
- Adding new ticker-specific thresholds beyond the existing 0.1-10x ratio.

## Acceptance bar

- Phase 1 diagnostic identifies the poisoned upstream by name (price_bus / Massive WS / Massive REST / something else).
- Phase 2 fix lands the implausible-quote guard at `fetch_quote` boundary.
- Phase 3 monitor surfaces future occurrences within 10 minutes.
- Phase 4 postmortem filed.
- One unit test per phase that has new code.
