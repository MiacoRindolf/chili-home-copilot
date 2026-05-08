# f-pdt-crypto-bypass-cleanup

STATUS: QUEUED
SLUG: pdt-crypto-bypass-cleanup
PROPOSED: 2026-05-08
SEVERITY: medium (operator confirmed crypto bypass works today, but clean it up + harden + document)

## TL;DR

The PDT guard's crypto-bypass (R35, 2026-04-30) currently relies on a single `ticker NOT LIKE '%-USD'` SQL filter + a per-call `_is_crypto_ticker(ticker)` short-circuit. **Operator at $12k+ equity is above the $2k crypto-account minimum**, so crypto trades should never be subject to PDT (it's a securities-only rule). Today's verification shows the bypass IS working — 0 crypto trades in the day-trade count of 14. But the bypass is implicit (filter pattern), not explicit (asset-class check). Make it cleaner, harden against ticker-shape edge cases, document the thresholds, add unit tests pinning the contract.

## Why now

Operator audit 2026-05-08 surfaced:
1. The PDT count of 14 is correct (all stock round-trips from autotrader Apr 29-30 batch).
2. **Operator wants the crypto bypass cleanly fixed** — explicit, asset-class-driven, threshold-aware (`$2k crypto minimum < $12k operator equity → never PDT-block crypto`).
3. The current `'%-USD'` ticker pattern works for Coinbase but is fragile against future broker-naming variations (Robinhood crypto, alternative venues).

Reference: `app/services/trading/pdt_guard.py:115-160` (`_count_day_trades_5d`), `:163-200` (`can_open_intraday_round_trip` short-circuit).

## Goal

1. **Replace ticker-pattern filter with explicit asset-class check.**
   - Add column-aware filter: `WHERE asset_kind = 'stock'` (or whichever the canonical column is — verify via schema). Today the `trading_trades` table has `asset_kind` column (verified 2026-05-08).
   - Backfill `asset_kind` on rows where it's NULL via a one-shot migration if needed.
   - Keep the ticker-suffix filter as a defense-in-depth fallback for rows where `asset_kind` isn't set.

2. **Make `can_open_intraday_round_trip`'s crypto short-circuit explicit.**
   - Replace `_is_crypto_ticker(ticker)` with `_is_crypto_asset(ticker, asset_kind=None)` that takes the asset_kind hint. Falls back to ticker pattern only when `asset_kind` is None.
   - Update all call sites (autotrader, options engine if it calls in) to pass `asset_kind`.

3. **Account-equity-aware PDT bypass.**
   - Brokers' crypto rules: account ≥ $2k → no PDT applies to crypto. Stocks: $25k threshold.
   - Today the gate already short-circuits crypto unconditionally (which is correct). Add explicit equity-tier logic so the bypass is auditable:
     - `if asset_class == 'crypto' and account_equity_usd >= 2000: return ALLOW (reason='crypto_above_equity_floor')`
     - `if asset_class == 'stock' and account_equity_usd >= 25000: return ALLOW (reason='above_pdt_floor')`
   - The crypto path is unconditionally allowed if equity ≥ $2k. The stock path uses the existing day-trade-count logic if equity < $25k.
   - **Account equity sourcing:** broker-side query is the truth, but it's slow / racy. Cache it on `trading_account_state` (or whichever table) refreshed every 5min by broker_sync. Read the cache in `pdt_guard`. Default conservatively to "below threshold" if cache is missing.

4. **Documentation + tests.**
   - Module docstring at the top of `pdt_guard.py` enumerating the policy: stocks PDT applies <$25k, crypto exempt at any equity ≥ $2k, options ??? (surface as open question).
   - Test file `tests/test_pdt_guard_crypto_bypass.py`:
     - Crypto ticker BTC-USD with various asset_kinds → always allow.
     - Stock ticker AAPL with day-trade-count=22 + equity=12000 → block.
     - Stock ticker AAPL with day-trade-count=22 + equity=30000 → allow (above floor).
     - Crypto ticker with NULL asset_kind → fallback ticker filter still allows.
     - Edge: ticker ending in -USD that isn't crypto (theoretical; document).

## Acceptance criteria

1. `pdt_guard.py` has explicit `_is_crypto_asset(ticker, asset_kind)` and `_account_equity_tier()` helpers.
2. Module-level docstring documents the four-tier policy (crypto $2k / stock $25k).
3. `can_open_intraday_round_trip` short-circuits crypto explicitly with `reason='crypto_above_equity_floor'` (was `'crypto_not_pdt_eligible'`).
4. New tests in `tests/test_pdt_guard_crypto_bypass.py`: 5+ helper-level tests covering the matrix above.
5. Existing `pdt_guard` tests still pass.
6. Live verification (post-deploy): crypto entries continue to flow through; stock entries continue to block at the same threshold. No behavior regression.
7. CC report at `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-pdt-crypto-bypass-cleanup.md`.

## Brain integration (reuse, don't rewrite)

- `trading_trades.asset_kind` column already exists (verified 2026-05-08).
- `broker_sync` already updates an account-state cache somewhere (probably `trading_account_state` or similar — verify via grep).
- Existing `pdt_guard.can_open_intraday_round_trip` shape stays; just internals change.
- Test file pattern from `tests/test_fastpath_settings_validation.py` (plausible-range smell tests) applies here too.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged. Stock entries still go through PDT gate when equity < $25k.
- **No threshold tuning** of the actual PDT counts. The 3-trade-in-5-days threshold for sub-$25k accounts stays.
- **Edit-tool truncation discipline (HARD).** Memory `reference_2026_05_07_widespread_truncation.md`. Six rounds yesterday. Splice pattern only for `pdt_guard.py` (currently 200+ lines). `wc -l + ast.parse` post-edit verification mandatory.
- **No magic numbers** — `$2000` (crypto min) and `$25000` (PDT cap) become module-level constants with docstring linking to SEC/FINRA citation.
- **Backwards-compatibility** — the existing `_is_crypto_ticker` function stays as a thin wrapper around the new helper, so any caller outside `pdt_guard.py` doesn't break.
- **Tests use `_test`-suffixed DB.**

## Out of scope

- Changing the day-trade counting SQL beyond the asset_kind filter.
- Auto-disabling the autotrader on PDT lockout (separate brief: `f-autotrader-pdt-aware-exit-deferral`).
- Pattern-quality demotion (separate brief: `f-pattern-demote-on-thin-evidence`).
- Real-time broker-API equity check (it's too slow for the hot path; we're using the cached value).
- Options PDT rules (open question; surface in module docstring; if the operator wants options handled too, queue another brief).

## Sequencing

1. **Truncation scan** + read `pdt_guard.py` as it is.
2. Splice-rewrite `_count_day_trades_5d` to use `asset_kind = 'stock'` filter (with ticker fallback).
3. Splice-rewrite `can_open_intraday_round_trip` short-circuit to explicit asset+equity logic.
4. Add `_account_equity_tier()` helper that reads cached equity from broker state.
5. Module docstring + constant lift for `$2000` / `$25000`.
6. New test file.
7. Commit + push.

## Operator-side after CC ships

1. Pull + truncation scan.
2. Restart `chili` + `autotrader-worker` (the two services that call into `pdt_guard`).
3. Verify autotrader PDT block reasons are unchanged for stocks (still blocks at 22>=3 if equity <$25k).
4. Verify crypto entries continue placing without PDT interference.
5. As days pass and the count ages out, confirm stock entries resume.

## Rollback plan

`git revert` the commit. The new helpers are additive and the module's public API is preserved; revert restores prior behavior bit-identically.

## Open questions

1. **Options PDT.** Brokers treat options day-trades differently than stocks under PDT, but the rule still applies. Should `pdt_guard` count option round-trips? Today it does (no asset_kind filter excludes them). Surface in CC report.
2. **Which table holds account equity?** Probably `trading_account_state` or similar. Grep `broker_sync.py` for the writer; CC adapts the reader.
3. **Equity cache staleness threshold.** If the cache is >30 min old, `_account_equity_tier()` should return "unknown" → conservative refuse for stocks, but still allow for crypto (since crypto bypass is unconditional at any equity ≥ $2k and operator is well above that). Document the 30-min threshold as another module-level constant.
