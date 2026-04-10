# Pattern Evidence: current data only — **done**

## Implemented

- **Removed** global keyword padding from `_compute_deduped_backtest_win_stats` and `_deduped_win_rate_progress_series` in [`app/routers/trading_sub/ai.py`](app/routers/trading_sub/ai.py).
- **Shared pool** `_EVIDENCE_LINKED_BACKTEST_LIMIT = 4000` for panel list + chart (was 500 vs 4000 mismatch).
- **Chart**: newest `row_limit` sibling-linked rows, reversed to chronological replay; **state updated on every run** (including 0-trade); WR uses **only tickers with `trade_count > 0`** so it matches the header and fixes stale-state bugs.
- **`_compute_evidence_stats`**: now includes `backtest_total_displayed` in the returned dict (was passed but dropped — fixes Live Stats “X/Y with trades” label).
- **UI** ([`app/templates/brain.html`](app/templates/brain.html)): “Evidence files”, “Journal” tab, clearer WR chart blurb, journal empty state, pattern card pill text.

## Rationale (unchanged)

Sibling-linked `BacktestResult` rows only; no unrelated historical global backtests in evidence stats.
