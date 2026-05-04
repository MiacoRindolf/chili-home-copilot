# NEXT_TASK: audit-unsupported-crypto-prefilter

STATUS: DONE

## Goal

Stop CHILI from sending unsupported crypto symbols into Robinhood's order endpoints. Add a venue-capability prefilter that runs BEFORE the broker call so unsupported tickers (ZEC-USD, GNO, AKT, 2Z, GLM, 1INCH, TRAC, and any future drift) are deterministically rejected with a clean reason instead of producing broker-side rejection storms, `list index out of range` tracebacks, and broker-account notifications.

Success means:

1. **Robinhood-supported-crypto registry** — a single source of truth for which crypto symbols Robinhood actually trades. Either a static curated list, a cached `get_instruments_by_symbols(<base>)` probe, or both. This is the data the prefilter reads.
2. **Pre-broker-call filter at the autotrader funnel.** `auto_trader.py:1047-1075` currently catches `crypto_not_supported_on_robinhood:<BASE>` returning from `broker_service.py:2433/2515` AFTER the broker round-trip and surfaces it as a `blocked` reason. Move the check upstream so the broker call is never made for unsupported symbols.
3. **Same filter at `place_missing_stop` entry.** The bracket reconciler's writer currently attempts equity stop-loss placement for ZEC-USD (a crypto ticker) and triggers `list index out of range` from `robin_stocks.orders.order` because there's no equity instrument record. Filter at the writer's entry too.
4. **Audit-row coverage.** Every prefilter rejection writes a `trading_autotrader_runs` row with a stable reason like `pre_broker:venue_unsupported_crypto:<BASE>`, so the funnel-accounting query the audit team runs continues to see these decisions instead of having them disappear before the audit boundary.
5. **Live verification.** Post-deploy log probe shows zero new `place_missing_stop SKIPPED ... reason=robinhood_no_instrument_for_<crypto>`-class tracebacks for at least one full sweep cycle.

This task ships **prefilter + tests**, not a code-clarity/diagnostic task. Deliverable: `docs/STRATEGY/CC_REPORTS/<date>_audit-unsupported-crypto-prefilter.md`.

## Why now

The 2026-05-03 audit flagged this as HIGH #4 with concrete numbers: ~127 `crypto_not_supported_on_robinhood:<BASE>` blocks + 44+3 broker-error rows in the last 24h. My reevaluation promoted it above audit's HIGH #2 (venue-truth wiring). It then sat in queue while we worked through the bracket-intent chain.

Live evidence accumulated during the chain:

- The `bracket-intent-stop-price-live-sync` CC report (Side-channel observation) captured 4+ recurring tracebacks at 1-minute cadence: `place_sell_stop_loss_order for ZEC: list index out of range` from `robin_stocks.orders.order` calling `get_instruments_by_symbols("ZEC", info='url')[0]` on an empty list. Root cause: ZEC-USD is crypto, the writer routes it through Robinhood's equity stop-loss API, equity API has no `ZEC` instrument.
- Predates 2026-05-03 22:01 sweep restart — pre-existing, not caused by any of today's changes.

The trace storm is the live cost of not having the prefilter. It also blocks meaningful operational signal: every minute the broker-sync-worker logs are noise about ZEC-USD instead of signal about anything else.

`f8b-verification-soak-3` (preserved at `docs/STRATEGY/QUEUED/`) remains gated on or after 2026-05-04 16:30 UTC. Today's task does not affect it.

## Step 1 — Pick the registry shape

Two viable approaches; CC's choice based on what the codebase already has:

### Option A — static curated whitelist

A module-level constant in (probably) `app/services/broker_service.py` or a sibling. List of base symbols Robinhood supports for crypto trading. Membership check is O(1).

**Pros:** dead simple, no broker calls at filter time, deterministic. **Cons:** must be maintained — Robinhood adds/removes pairs over time, drift will surface as new false rejects (which is the safer failure mode than false accepts).

### Option B — cached broker probe

`get_robinhood_crypto_supported(symbol) -> bool` that runs `robin_stocks.crypto.get_crypto_info(symbol)` (or equivalent) and caches results with a TTL (~24h is fine; Robinhood doesn't add pairs hourly). On cache miss, query; on cache hit, return cached.

**Pros:** self-updating; no manual list maintenance. **Cons:** broker round-trip on cache miss; failure mode if broker probe itself fails needs explicit handling (default to "supported" = unsafe, or "unsupported" = false negatives).

### Recommendation

**Option A** for the first version. Robinhood's supported crypto list is small (~20 pairs) and changes slowly. Adding an option-B layer later is a tiny refactor. Failure mode is "false unsupported" which is the safer direction. The list lives in code review where future drift is visible.

If CC discovers a broker_service helper that already maintains a Robinhood crypto symbol list (e.g., for some other path), prefer reusing it over creating a new list.

## Step 2 — Wire the prefilter at three entry points

### 2a — `auto_trader.py` autotrader funnel

Before the broker call at `auto_trader.py` near lines 1064/1071. Currently the flow is roughly:

```python
order = broker_service.place_crypto_buy(ticker, ...)
if order.get("error", "").startswith("crypto_not_supported_on_robinhood:"):
    base = ...
    return AutoTraderRun(decision="blocked", reason=f"broker:crypto_not_supported_on_robinhood:{base}")
```

Replace with:

```python
base = _extract_base(ticker)
if not is_robinhood_supported_crypto(base):
    return AutoTraderRun(
        decision="blocked",
        reason=f"pre_broker:venue_unsupported_crypto:{base}",
    )
order = broker_service.place_crypto_buy(ticker, ...)
# (existing post-call error handling stays as defense-in-depth)
```

Keep the post-call check as defense-in-depth — if Robinhood adds/removes a pair without our list updating, the broker still tells us; we just won't make as many calls in the steady state.

### 2b — `bracket_writer_g2.place_missing_stop` entry

At the top of the function, before any broker call:

```python
if _is_crypto(ticker) and broker_source.lower() == "robinhood":
    base = _extract_base(ticker)
    if not is_robinhood_supported_crypto(base):
        logger.warning(
            f"{BRACKET_WRITER_G2} place_missing_stop SKIPPED intent=%s "
            "ticker=%s reason=venue_unsupported_crypto base=%s",
            bracket_intent_id, ticker, base,
        )
        return WriterAction(
            action="place_missing_stop",
            ok=False,
            reason="venue_unsupported_crypto",
            broker_source=broker_source,
            ticker=ticker,
        )
```

This is the line that closes the ZEC-USD traceback storm.

### 2c — `coinbase_service.py` (if applicable)

If the same broker-routing-mismatch pattern can occur for Coinbase (e.g., a Coinbase-only ticker reaches a Robinhood writer), add the symmetric check. The filter direction depends on which broker should own which assets. If Coinbase is the catch-all for crypto and Robinhood is opt-in supported list, the filter is "Robinhood + crypto + not on whitelist → reject."

If CC's diagnosis shows Coinbase isn't routing crypto through Robinhood's equity API anywhere, skip 2c.

## Step 3 — Audit emission

For 2a (autotrader), the existing AutoTraderRun row write covers it — just use the new `pre_broker:venue_unsupported_crypto:<BASE>` reason string.

For 2b (writer), the WriterAction return is observed by `bracket_reconciliation_service._invoke_writer_for_decision` and surfaces in `[bracket_reconciliation_ops]` lines. No new audit table, no new column.

For consistency across the funnel, the prefilter rejection should be recognizable downstream. The reason string convention is `pre_broker:` prefix (autotrader) and the `venue_unsupported_crypto` token in both. Funnel-accounting queries can pattern-match either form.

## Step 4 — Tests

Add `tests/test_unsupported_crypto_prefilter.py` covering:

1. **Whitelist contains BTC-USD, ETH-USD, etc.** — sanity check the registry is populated.
2. **`is_robinhood_supported_crypto("ZEC")` returns False.** Stable test data (we know ZEC isn't supported).
3. **`is_robinhood_supported_crypto("BTC")` returns True.** Stable test data.
4. **autotrader prefilter blocks ZEC-USD before broker call.** Mock `broker_service.place_crypto_buy` to raise if called; assert it's not called; assert AutoTraderRun reason matches `pre_broker:venue_unsupported_crypto:ZEC`.
5. **autotrader allows BTC-USD through to broker.** Mock broker; assert it IS called.
6. **`place_missing_stop` skips ZEC-USD.** Seed bracket_intent with ZEC ticker, robinhood broker_source. Mock the equity stop-loss API to raise `IndexError` if called. Assert it's not called. Assert WriterAction reason='venue_unsupported_crypto'.
7. **`place_missing_stop` proceeds for AAPL (equity).** Negative-case: stock ticker isn't filtered.
8. **Static-list maintenance hint test.** A test that asserts the whitelist contains a stable subset (BTC/ETH/SOL/AVAX/DOGE — the fast-path pairs). Catches accidental list erosion.
9. **No regression on prior bracket tests.** Run `test_bracket_*` suite; assert all pass.

All tests use `chili_test`.

## Step 5 — Deploy + verify

1. Land the code on a clean commit. `verify-migration-ids.ps1` (no schema change expected).
2. **Run the new tests + the bracket regression suite. Report results in the CC_REPORT.**
3. Pre-deploy log probe (capture in CC_REPORT):
   ```bash
   docker compose logs broker-sync-worker --since 1h | grep -E "ZEC|crypto_not_supported_on_robinhood|list index out of range" | head -50
   ```
   Expected: traceback storm visible, ~1/min for ZEC-USD plus other unsupported crypto blocks if any are firing.
4. Restart `broker-sync-worker` (use `docker compose restart broker-sync-worker` to NOT pick up the operator's pending `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` flag — same discipline as the prior CC). Verify pickup of new code.
5. Wait one full sweep cycle (~2 minutes). Capture log lines: `[bracket_writer_g2] place_missing_stop SKIPPED intent=<> ticker=ZEC-USD reason=venue_unsupported_crypto`.
6. Post-deploy log probe (same query as step 3). Expected: zero new `list index out of range` tracebacks; clean SKIPPED log lines instead.
7. SQL probe: count `trading_autotrader_runs` rows with the new `pre_broker:venue_unsupported_crypto:%` reason in the next 24h. Compare against the audit's prior 24h count of ~127 broker-error rejects. Expected: similar order of magnitude, but now flowing through the pre-broker path.

## Brain integration (reuse, don't rewrite)

- `broker_service.py` — existing crypto routing functions live here. Add `is_robinhood_supported_crypto(base)` alongside the existing helpers; use whatever symbol-normalization helper already exists (don't duplicate).
- `auto_trader.py` — existing AutoTraderRun shape and audit-row write. Just use the new reason string.
- `bracket_writer_g2.py` — existing WriterAction shape. Just add the early-return at the top of `place_missing_stop`.
- The fast-path pairs `BTC-USD, ETH-USD, SOL-USD, AVAX-USD, DOGE-USD` are already canonical in `docker-compose.yml` for the fast-data-worker. Use them as the seed list for the test (#8) and as the must-include-baseline for the whitelist.

## Constraints / do not touch

- **Do not modify the live-fast-path safety belts.** PROTOCOL Hard Rule 1.
- **Do not flip `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL` or any operator-controlled flag.** That's a separate operator decision.
- **Do not use `docker compose up -d --force-recreate`** for the worker restart — `docker compose restart` only, to avoid picking up env-var changes that aren't part of this task.
- **Do not change the post-broker-call error handling.** The new prefilter is additive — keep the existing `crypto_not_supported_on_robinhood:` post-call check as defense-in-depth.
- **No magic numbers.** No new thresholds.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.

## Out of scope

- Coinbase-side venue capability filtering. Out of scope unless CC's diagnosis surfaces the same pattern.
- Investigating WHY ZEC-USD ended up routed through Robinhood's equity stop-loss API in the first place (probable cause: trade was opened from a path that didn't propagate `broker_source` correctly, OR ZEC-USD was originally on Coinbase and got mis-mirrored). Out of scope; the prefilter is the symptom-fix.
- Removing ZEC-USD from `trading_trades` if it's an orphan. The position exists at the broker; CHILI should mirror it correctly, not delete it.
- The `trading_stop_decisions` coverage gap surfaced in the prior task. Belongs to a separate follow-up.
- `target_price` symmetric mirror sync from prior task's Open Q #2. Same deferral.

## Success criteria

1. New helper `is_robinhood_supported_crypto(base)` exists, has the fast-path pairs in the whitelist, returns False for ZEC.
2. Prefilter wired at `auto_trader.py` (pre-broker) and `bracket_writer_g2.py:place_missing_stop` (writer entry).
3. All 9 new tests pass against `chili_test`. Existing bracket tests (32 total: emergency-repair 7 + stale-label-cleanup 9 + cover-policy-clarify 8 + stop-price-live-sync 8) still pass.
4. Post-deploy log probe shows zero new `list index out of range` tracebacks for at least one full sweep cycle. CC_REPORT shows pre/post diff.
5. CC_REPORT written at `docs/STRATEGY/CC_REPORTS/<date>_audit-unsupported-crypto-prefilter.md` per PROTOCOL format. One commit (or tight series), pushed.

## Open questions for Cowork (surface in your report only if relevant)

1. **Static list vs cached probe** — surface the choice and the reasoning. If CC discovers a pre-existing Robinhood crypto symbol list elsewhere in the codebase, surface that too.
2. **List membership for the audit's flagged symbols** — surface concrete True/False for each of GNO, AKT, 2Z, GLM, 1INCH, TRAC, ZEC, BTC, ETH, SOL, AVAX, DOGE. If any are surprising (e.g., 1INCH actually IS supported on Robinhood), the audit's classification was wrong on that ticker and the prefilter would let it through.
3. **Coinbase-routing check** — surface whether the diagnosis found any Coinbase-only ticker reaching a Robinhood writer. If yes, the symmetric Step 2c filter is in scope; if no, skip cleanly.
4. **Reason-string format** — I proposed `pre_broker:venue_unsupported_crypto:<BASE>` for autotrader and `venue_unsupported_crypto` for the writer. Surface if the codebase has a canonical reason-string format that fits better.

## Rollback plan

- **Code rollback**: `git revert <commit>`. Prefilter becomes a no-op; broker-side rejections resume; ZEC-USD traceback storm returns. The post-broker-call defense-in-depth path keeps catching the rejection, just at higher cost than the prefilter.
- **No persisted-data rollback needed.** Rows already written with `pre_broker:venue_unsupported_crypto:%` reason stay valid — opaque audit text.
- **No live-broker rollback needed** — this task makes broker calls only via the writer path, and the prefilter REDUCES broker calls. Removing it can't make the broker side worse.
- **No schema rollback needed** — schema unchanged.

This task makes no broker mutations on its own. The prefilter only PREVENTS broker calls; it doesn't initiate any.
