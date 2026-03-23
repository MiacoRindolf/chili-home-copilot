# Data limitations: survivorship, corporate actions, and bar quality

## Survivorship and listing bias

Universe sources (screeners, default ticker lists, provider snapshots) are generally **current** constituents. Delisted names and historical index membership are **not** fully modeled. Backtests that rotate through “today’s” liquid names can **overstate** edge versus what was knowable in the past.

**Mitigation ideas (product / research):**

- Prefer explicit historical universes for validation when available.
- Tag backtests with the universe source and date.
- Treat crypto and ADR names separately — liquidity and session rules differ.

## Corporate actions

OHLCV from consolidated feeds is often **split-adjusted**. That is appropriate for many equity studies but can interact oddly with **absolute** price rules in pattern conditions. Dividends are not uniformly cash-adjusted across free tiers.

## Bar quality

`assess_ohlcv_bar_quality` in `app/services/trading/market_data.py` flags **large gaps** between consecutive bar timestamps relative to the median bar spacing. With `brain_bar_quality_strict=true`, intraday mining skips series that fail the check (reduces garbage rows from halts, missing sessions, or bad provider slices).

This does **not** detect exchange halts, limit-down locks, or bad prints — only coarse timestamp regularity.

## Related settings

- `brain_bar_quality_strict`, `brain_bar_quality_max_gap_bars` in `app/config.py`
- OOS and stress settings: `brain_oos_*`, `brain_bench_cost_stress_*`
