# CC_REPORT: f-coinbase-tick-size-precision-fix

## What shipped

Three commits on `main`, single logical fix in three WIP slices per the
APPROVED plan (interactive Cowork override, 2026-05-10 13:23 PT).

| Commit | Subject | Files | Lines |
|---|---|---|---|
| `e5a6deb` | wip(brain): coinbase product-info cache + quantize helpers | `app/services/trading/venue/coinbase_spot.py` | +94 |
| `4501169` | feat(brain): coinbase quantize stop/limit/size before submit | `app/services/trading/venue/coinbase_spot.py`, `tests/test_coinbase_stop_primitive.py` | +110 / -1 |
| `5f6576a` | test(brain): tick-size quantization coverage | `tests/test_coinbase_tick_size_precision.py` (NEW) | +383 |

Net: 1 production file (`coinbase_spot.py` 1180 → 1358 lines), 1 existing
test file updated (monkeypatch helper), 1 new test file. No migration.

### Mechanism (additive, no existing safety gate weakened)

`place_stop_limit_order_gtc` now performs (after side validation, before
the SUBMITTING state-machine transition):

1. Fetch `NormalizedProduct` via `_get_product_info_cached` — in-process
   `_PRODUCT_INFO_CACHE` (dict, threading.Lock, 1-hour TTL). On cache
   miss, calls existing `get_product`. **Raises `VenueAdapterError`
   if fetch fails or returns invalid increments — no magic-fallback
   tick_size guess (per COWORK_ADVISOR_BRIEF §2.6).** The raise is
   caught at the placement site and packaged as `ok=False, code=
   product_info_unavailable | product_info_invalid`.
2. Quantize `base_size` DOWN to `base_increment` (never order more
   than intended) via `_quantize_size`.
3. Quantize `stop_price` and `limit_price` to `quote_increment` via
   `_quantize_price`, with `mode='down'` for SELL (wider stop band)
   or `mode='up'` for BUY (per plan-c direction semantics).
4. Preserve SELL: `limit ≤ stop`, BUY: `limit ≥ stop`. If quantization
   collapsed the buffer, nudge `limit_price` one increment further
   from `stop_price` (debug-logged).
5. Compute notional = quantized_size × quantized_stop. If
   `notional < min_market_funds` (when product reports it), reject
   with explicit log line and `ok=False`. **No cap-to-min** —
   silently mutating the brain's intent is a data-integrity violation.
6. Use the quantized strings in the SUBMITTING state-machine payload
   AND in the SDK call (forensic traceability).

Quantization uses `decimal.Decimal` for exactness — float modulo loses
precision at 8-decimal increments (e.g., `0.00786416 % 0.00000001`
won't be 0 in float space).

## Verification

### Tests — all pass

```
pytest tests/test_coinbase_tick_size_precision.py tests/test_coinbase_stop_primitive.py -v -p no:asyncio

26/26 passed in 1.37s
```

- **18 NEW tests** in `test_coinbase_tick_size_precision.py`:
  - Helper unit tests (7): quantize_price down high-decimal sub-penny;
    down low-decimal BTC-class; up for BUY; quantize_size DOWN; invalid
    increment (0, negative); non-finite value (nan, inf); invalid mode.
  - Cache + fetch (4): hit avoids refetch; TTL refetches via patched
    `time.time`; fetch failure → `ok=False` `product_info_unavailable`
    (NO magic fallback); invalid increments → `ok=False`
    `product_info_invalid`.
  - End-to-end (7): ALEPH-USD smoking-gun reproducer (8-decimal raw
    → 7-decimal SDK kwargs); SELL `limit≤stop` preserved; BUY
    `limit≥stop` preserved; min_market_funds reject + explicit error;
    above-min passes; base_size DOWN-quantized; existing duplicate
    check short-circuits before product-info fetch (regression guard).
- **8 EXISTING tests** in `test_coinbase_stop_primitive.py` still pass.
  Added `_PERMISSIVE_PRODUCT` + `_get_product_info_cached` monkeypatch
  in the helper — round-trips existing test prices unchanged
  (0.4500, 0.4400, 100.0 all snap to 0.0001 / 0.1 grids cleanly).

### Truncation scan (per COWORK_ADVISOR_BRIEF §2.1)

After each Edit:
- `wc -l app/services/trading/venue/coinbase_spot.py` showed strictly
  positive line deltas matching the inserted block sizes.
- `git diff --stat` confirmed +3 / +60 / +94 / +178 progressive sums.
- `ast.parse` clean after every edit.

Final state: `wc -l = git show HEAD = 1358 lines`. No silent truncation.

### Pytest-asyncio collection bug (pre-existing, unrelated)

Default pytest invocation hits an `AttributeError: 'Package' object has
no attribute 'obj'` from `pytest-asyncio==0.23.3` colliding with
`pytest==9.0.2`. Worked around with `-p no:asyncio` for these
non-asyncio test files. Flagged below; not in scope of this task.

### Bracket-coverage tests (`test_coinbase_bracket_coverage.py`)

Mock at the bracket-writer level (search for `CoinbaseSpotAdapter` /
`place_stop_limit_order_gtc` returns only a docstring reference at line
506). The new quantize path is therefore not exercised by these tests.
A direct run on the host hung at collection (likely DB-fixture related,
unrelated to this change); `bracket_writer_g2` and the reconciler are
in the HARD CONSTRAINT no-touch list, so no diff there to attribute.

### Hot-fix path for the 9 currently-naked trades

Per the brief: nothing extra needed. Once this commit is deployed:
- `bracket_reconciliation_service.run_reconciliation_sweep` runs every
  2min via the broker-sync APScheduler job.
- It re-classifies each open trade's intent state; the `missing_stop`
  decisions from the 2026-05-10 19:50 UTC log re-fire.
- `_invoke_writer_for_decision` routes Coinbase to `place_missing_stop`
  (Bug C fix from commit 77d9a5e is in place).
- The new quantization runs before SDK submission. Coinbase accepts.
- `intent_state` transitions to `confirmed_at_broker`,
  `broker_stop_order_id` populates.

Verification window: 2 reconciler sweeps after deploy (~5 min total).

### Verification queries (for operator post-deploy)

```sql
-- Pre-deploy snapshot expected: 9 rows, intent_state='intent',
-- broker_stop_order_id IS NULL.
SELECT id, ticker, intent_state, broker_stop_order_id
FROM trading_bracket_intents
WHERE intent_state IN ('intent', 'pending_at_broker')
  AND broker_source = 'coinbase'
ORDER BY id;

-- Post-deploy + 5min: all 9 intent_state='confirmed_at_broker',
-- broker_stop_order_id IS NOT NULL.
SELECT id, ticker, intent_state, broker_stop_order_id
FROM trading_bracket_intents
WHERE broker_source = 'coinbase'
  AND id IN (239, 249, 250, 251, 252, 253, 254, 255, 256)
ORDER BY id;
```

```bash
# Tail brain-worker for the resolution
docker compose logs --tail=200 brain-worker | grep -E "(place_stop_limit|coinbase_spot|bracket_reconcil)"
# Expect: 0 'Too many decimals' lines; new 'confirmed_at_broker' transitions.
```

## Surprises / deviations

1. **Test repro `limit_price` value**: My initial draft of
   `test_aleph_usd_repro_quantizes_to_quote_increment` set
   `limit_price="0.00786416"` equal to `stop_price`, then asserted
   `"0.0078640"` for the limit (expecting a nudge). That was wrong —
   when `limit==stop` after quantization, the SELL ordering invariant
   `limit≤stop` is satisfied and no nudge fires. Updated the test to
   use a realistic buffer (`limit_price="0.00785416"`) which round-trips
   to `"0.0078541"`. Behavior of the production code was correct; only
   the test expectation was off. Iteration cost: 1 re-run.

2. **Existing test compatibility**: Adding the quantize block to the
   primitive's contract means MagicMock-SDK tests need a permissive
   `_get_product_info_cached` patch (otherwise `get_product` returns
   None and we refuse to place). Updated `test_coinbase_stop_primitive
   .py::_make_adapter` to inject `_PERMISSIVE_PRODUCT` (base_increment
   =0.1, quote_increment=0.0001) — the test's 0.4500 / 0.4400 / 100.0
   round-trip cleanly on those grids. Documented in the helper's
   docstring. This is a test-only edit; no production behavior change.

## Deferred

None. The single-shot fix in the brief is fully shipped.

## Open questions for Cowork

1. **`pytest-asyncio` 0.23.3 vs `pytest` 9.0.2 collision.** Default
   `pytest tests/...` invocation breaks at collection with
   `AttributeError: 'Package' object has no attribute 'obj'` (in
   `pytest_asyncio/plugin.py:626 pytest_collectstart`). Worked around
   with `-p no:asyncio` for this run, but the workaround skips any
   `@pytest.mark.asyncio` tests and likely hides regressions in the
   chat / SSE paths. Not in scope for this task — flagging for an
   environment-tooling QUEUED brief (pin a compatible version pair, or
   upgrade `pytest-asyncio` to its current major).

2. **`_PRODUCT_INFO_TTL_SEC = 3600` as a constant vs setting.** Left
   in the file as a constant per plan-b — the value is a Coinbase API
   contract characteristic (tick sizes change rarely), not a brain
   tuning knob. If a future symbol's increments change mid-trading-day
   and operations want a hot-reload override, promote it to
   `settings.chili_coinbase_product_info_ttl_sec` then.

3. **Soak observability.** No new metric emitted for "quantize adjusted
   price" — a future enhancement could record (raw_price,
   quantized_price, increment) to a forensic log so the brain can
   measure how often quantization actually moves a value. Not required
   by the brief; raising it for the next pass.
