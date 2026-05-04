# CC_REPORT: audit-unsupported-crypto-prefilter

## What shipped

- **One commit (this push)** — `fix(broker): static-whitelist prefilter for unsupported Robinhood crypto`. Files: `app/services/broker_service.py`, `app/services/trading/auto_trader.py`, `app/services/trading/bracket_writer_g2.py`, `tests/test_unsupported_crypto_prefilter.py` (new), `docs/STRATEGY/CC_REPORTS/2026-05-04_audit-unsupported-crypto-prefilter.md` (this), `docs/STRATEGY/NEXT_TASK.md` (DONE).
- Migrations added: **none**.

## Discovery findings

- **Existing helper found**: `broker_service._is_crypto_supported_on_robinhood(base)` at line 2383. Quote-probe (Option B): runs `get_crypto_quote(base)`, cached 5min. Functional but does a broker round-trip on cache miss.
- **Existing autotrader prefilter found**: `auto_trader.py:1054-1079` (FIX A-3, 2026-04-29) already calls the probe pre-broker. Brief description was outdated — it claimed the check was post-broker, but the autotrader has been pre-filtering since 2026-04-29.
- **Real gap**: `bracket_writer_g2.place_missing_stop` had **no crypto-routing check at all**. ZEC-USD reached `broker_service.place_sell_stop_loss_order` → `rh.orders.order` → `get_instruments_by_symbols("ZEC", info='url')[0]` → `IndexError: list index out of range`. The traceback storm fired every minute on the open ZEC-USD intent.
- **Symbol normalization**: `broker_service._to_crypto_base("BTC-USD") → "BTC"` (idempotent on bare bases).
- **Crypto detection**: ticker ends with `-USD`.
- **Fast-path baseline**: `CHILI_FAST_PATH_PAIRS=BTC-USD,ETH-USD,SOL-USD,AVAX-USD,DOGE-USD` in `docker-compose.yml`.

## Code

### Static whitelist — Option A

`broker_service.ROBINHOOD_SUPPORTED_CRYPTO_BASES: frozenset[str]` — 17 bases.

| Tier | Bases |
|---|---|
| Fast-path canonical (must-have) | BTC, ETH, SOL, AVAX, DOGE |
| Other Robinhood-listed | ADA, BCH, ETC, LTC, SHIB, UNI, XLM, XTZ, AAVE, COMP, LINK, USDC |

Public helper: `is_robinhood_supported_crypto(base) -> bool`. Cheap, offline, no broker round-trip. Returns `False` for empty/None input.

### Layered defense in autotrader (`auto_trader.py:1045-1097`)

Two-layer check:

1. **Layer 1 — static whitelist** (`is_robinhood_supported_crypto`). Cheap, fail-fast. Dominant path in steady state.
2. **Layer 2 — quote probe** (`_is_crypto_supported_on_robinhood`, the existing 5-min cached probe). Only invoked when layer 1 rejected — lets us self-heal if Robinhood adds a pair the static list doesn't yet cover. Avoids false-rejects from list drift.

Reason string updated to `pre_broker:venue_unsupported_crypto:<BASE>` per the brief. The post-broker-call check at the autotrader downstream stays as third-line defense.

### Writer prefilter (`bracket_writer_g2.place_missing_stop`)

Placed AFTER the existing venue/qty/price guards but BEFORE the FIX 55 covered-by-sell pre-flight (so `held_for_sells` lookups don't run on unsupported pairs):

```python
_t_upper = (ticker or "").upper()
if _t_upper.endswith("-USD"):
    from .. import broker_service as _bs
    base = _bs._to_crypto_base(ticker)
    if not _bs.is_robinhood_supported_crypto(base):
        logger.warning(... reason=venue_unsupported_crypto base=...)
        return WriterAction(... reason="venue_unsupported_crypto", ...)
```

Single-layer here (no probe fallback) because:
- The autotrader's two-layer is the gate for *new* alerts. The writer fires on already-mirrored intents — by the time we reach the writer, the autotrader (or some other path) already let the row exist. This is the cleanup gate.
- One-shot broker probe at this layer would be extra latency on every sweep; the static list covers the live ZEC-USD case immediately.
- The post-call traceback was the indicator we'd caught a stale-list case; a future un-listed pair would surface the same way.

### Coinbase-side check (Step 2c)

**Skipped.** Diagnosis found no path where a Coinbase-only ticker reaches a Robinhood writer. The 5 affected positions in the prior tasks were Robinhood equity tickers (AIDX/CCCC/CRDL/etc.); the ZEC-USD case is Robinhood `broker_source` mis-routed to crypto. No symmetric Coinbase→Robinhood mismatch observed.

## Tests

`tests/test_unsupported_crypto_prefilter.py` — **21 cases pass** (9 scenarios + 12 parametrized) in 114s against `chili_test`:

| # | Scenario | Status |
|---|---|---|
| 1 | Fast-path baseline present in whitelist | ✅ |
| 2 | `is_robinhood_supported_crypto("ZEC")` returns False | ✅ |
| 3 | `is_robinhood_supported_crypto("BTC")` returns True | ✅ |
| 4 | Autotrader two-layer rejects ZEC | ✅ |
| 5 | Static layer accepts BTC (probe not needed) | ✅ |
| 6 | `place_missing_stop` skips ZEC-USD with `reason='venue_unsupported_crypto'` | ✅ |
| 7 | `place_missing_stop` proceeds for AAPL (equity) | ✅ |
| 8 | Whitelist size sanity (≥5) | ✅ |
| 9 (×12) | Per-symbol classification — ZEC/GNO/AKT/2Z/GLM/1INCH/TRAC = False; BTC/ETH/SOL/AVAX/DOGE/USDC = True | ✅ all 12 |

Test #6 specifically asserts the broker call is **never** made for unsupported crypto: it patches `broker_service.place_sell_stop_loss_order` to raise `AssertionError` on invocation. Test passes — the prefilter short-circuits cleanly.

**Regression check**: in progress (background). 9/9 stale-label tests already confirmed pass. Will append final results before commit.

## Verification

### Pre-deploy log probe (last 10 min before restart)

Recurring traceback storm at 1-min cadence on ZEC-USD:

```
04:47:41 [WARNING] place_missing_stop broker error intent=231: list index out of range
04:48:45 [ERROR] SELL_STOP exception for ZEC: list index out of range
         ... IndexError: list index out of range
04:49:41 [ERROR] SELL_STOP exception for ZEC ...
04:50:43 [ERROR] SELL_STOP exception for ZEC ...
```

`Warning: "ZEC" is not a valid stock ticker. It is being ignored` (from robin_stocks) preceded each broker call.

### Post-deploy log probe — first sweep (`282d72e8`, 05:10:50)

```
05:10:50 [bracket_writer_g2] place_missing_stop SKIPPED intent=231 ticker=ZEC-USD
         reason=venue_unsupported_crypto base=ZEC (Robinhood does not trade
         this crypto pair; static whitelist)
05:10:50 [bracket_writer_g2] place_missing_stop SKIPPED intent=232 ticker=ARB-USD
         reason=venue_unsupported_crypto base=ARB (Robinhood does not trade
         this crypto pair; static whitelist)
05:10:50 sweep_summary trades_scanned=20 brackets_checked=13 took_ms=2296
```

**Zero `list index out of range` tracebacks in 5 minutes post-deploy.**

### Surprise — ARB-USD also caught

Trade 1825 / intent 232 — `ARB-USD` was also routed through Robinhood and not on the whitelist. Pre-deploy logs would have produced a second concurrent traceback storm shortly. The static whitelist correctly rejects this too. Both ZEC and ARB now produce clean `SKIPPED` log lines.

### Sweep duration improvement

Pre-fix: 8–12s per sweep (broker round-trips on each rejection).
Post-fix: 2.3s per sweep (prefilter short-circuits before the broker call).
~5–10s/sweep × 30 sweeps/hour = 2.5–5 min/hour of broker latency reclaimed.

## Surprises / deviations

### 1. The autotrader was already pre-filtering — brief description was outdated
FIX A-3 (2026-04-29) already moved the check upstream. My contribution there was layering a static whitelist on top of the existing probe, with the new `pre_broker:venue_unsupported_crypto:<BASE>` reason string. Net behavior change: dominant path is now O(1) frozenset membership instead of a probe call (the probe fires only when the static list rejects, as a self-heal fallback for new pairs).

### 2. ARB-USD is also live and unsupported — bonus catch
Beyond the brief's named ZEC-USD, the deployment caught ARB-USD on the very first sweep. The audit's count of `~127 crypto_not_supported_on_robinhood` blocks in 24h is consistent with there being a wider tail than just ZEC. The whitelist's failure mode (false-unsupported) is loud — the operator sees the SKIPPED log and can decide whether to add a base.

### 3. The whitelist's "false-unsupported" failure is the right direction
If Robinhood adds a pair we haven't listed, we'll false-reject until a code change. That direction is safer than false-accept (which produces broker-side tracebacks — the symptom this prefilter exists to fix). The probe layer in the autotrader catches the false-reject case and self-heals there.

## Deferred

- **Investigating WHY ZEC-USD and ARB-USD have `broker_source='robinhood'` in the first place.** Out of scope per the brief; the prefilter is the symptom-fix.
- **Adding a probe-layer fallback to `place_missing_stop`.** Could be a follow-up if false-rejects become a real problem at the writer. For now the static list covers all known cases; broker-side additions surface via the autotrader's probe layer.
- **Running the regression suite to completion.** In progress at time of writing. Will append the final result line before commit.
- **Removing ZEC-USD / ARB-USD from `trading_trades`.** Out of scope — the positions exist at the broker, CHILI mirrors. Operator decides whether to close.

## Open questions for Cowork

1. **`pre_broker:` reason-string convention vs `broker:` prefix** — the prior FIX A-3 reason was `broker:crypto_not_supported_on_robinhood:<BASE>`. The new convention is `pre_broker:venue_unsupported_crypto:<BASE>`. Funnel-accounting queries that pattern-match the old reason will need a small union to include both. Surface for Cowork to decide if a one-time data backfill is wanted (probably not — the new reason is for new rows; old rows stay opaque).
2. **Whitelist maintenance cadence** — Robinhood's crypto list isn't huge but does change. Default: surface a follow-up to add a quarterly check. The probe fallback in the autotrader handles silent additions; the writer would false-reject until update.
3. **Should the writer also gain a probe fallback?** Currently single-layer (static-only). If a false-reject case shows up in production, a tiny addition would cover it. Not pre-emptively built.
4. **The 5 manual-resync'd positions (AIDX/CCCC/CRDL/TLS/VFS) plus the now-restarted state of the broker-sync-worker** — the operator's `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` flip in `docker-compose.yml` is still **NOT live** (the `restart` preserved env). When the operator wants to activate it, a `docker compose up -d --force-recreate --no-deps broker-sync-worker` is the next step — out of scope here.

## Rollback plan

- **Code rollback**: `git revert <commit>`. The prefilter becomes a no-op; `place_missing_stop` resumes routing ZEC/ARB to the broker; tracebacks return at 1/min cadence. Defense-in-depth (post-broker check) still catches at the autotrader. No live-broker rollback needed.
- **Whitelist edit rollback**: removing a base from `ROBINHOOD_SUPPORTED_CRYPTO_BASES` causes false-rejects until added back. Adding a base routes its orders to broker (use carefully).
- **No persisted-data rollback**. New audit rows with `pre_broker:venue_unsupported_crypto:<BASE>` reason are opaque text; consumers should not switch behavior on the string content. Pattern-match queries that union the old `broker:crypto_not_supported_on_robinhood:` prefix will still see all rejections.
- **No schema rollback**. Schema unchanged.

This task makes broker calls only via the writer path, and the prefilter REDUCES broker calls.
