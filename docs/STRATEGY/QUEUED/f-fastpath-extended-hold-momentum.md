# f-fastpath-extended-hold-momentum

STATUS: QUEUED
SLUG: fastpath-extended-hold-momentum
PROPOSED: 2026-05-17
REQUESTED_BY: architect re-eval — fee math vs holding period
COMPLEMENTARY_TO: f-fastpath-confluence-signals (this brief extends what those high-quality alerts trade), f-fastpath-maker-only (this brief defines what the maker-filled position does next)

## TL;DR

Fast-path exit logic is tuned for **scalp** holding periods (under 1 minute, tight stops, tight targets). Per `fast_signal_decay`, the 1-minute realized edge on current pairs is 0.06–0.5 bps. Round-trip taker fees are 120 bps. **The math says 1-minute holds can never recover fees.** Replace the scalp exit with an intraday-momentum exit: ATR-trailed stops with a wall-clock cap of 30 minutes default. The signal half-life data justifies it — high-quality alerts (volume_breakout, spread_squeeze) show edge persistence for 5-30 minutes.

## Why

Fee math:

```
Coinbase taker round-trip (current default):  120 bps
Coinbase maker round-trip (f-fastpath-maker-only target):  ~10 bps with rebates, ~80 bps worst case

1-minute realized edge (current 5 pairs):  0.06 - 0.5 bps
5-minute realized edge (current 5 pairs):  ~3 bps
15-minute edge:  ~8-15 bps (per fast_signal_decay extrapolation)
30-minute edge:  ~15-30 bps for the high-quality alert buckets
```

Even under the most optimistic maker-tier assumption (10 bps round-trip), you need at least 15 bps gross edge to break even after slippage + variance. That horizon is **5-30 minutes**, not 1 minute.

The current exit logic exits at a fixed-percentage target or fixed-percentage stop, both calibrated for sub-minute moves. Result: most trades are stopped out by ordinary minute-bar noise before the signal has time to play out. The brief's hypothesis: **just hold longer**.

## Design

### New exit policy: `ATR-trailed-with-time-cap`

Today's exit (paraphrasing — verify in `app/services/trading/fast_path/executor.py` and any `exit_manager.py`):

```python
exit_when:
    pnl_pct >= +0.3% (target)
    or pnl_pct <= -0.2% (stop)
    or time_in_position >= 60s (time stop)
```

Proposed replacement (settings-driven, scalp behaviour preserved when knobs set tight):

```python
on_entry:
    atr = compute_atr_n_bars(ticker, n=14)
    initial_stop = entry_price - (atr_stop_mult * atr)         # default 1.5x
    trail_high_water_mark = entry_price

on_each_bar:
    current_price = book.mid_price
    if current_price > trail_high_water_mark:
        trail_high_water_mark = current_price
        trailing_stop = trail_high_water_mark - (atr_trail_mult * atr)  # default 1.0x
        active_stop = max(initial_stop, trailing_stop)

    if current_price <= active_stop:
        exit "stop"

    if time_in_position >= max_hold_s:                          # default 1800s = 30min
        exit "time_cap"

    if pnl_pct >= profit_take_pct (optional, default None):     # off by default
        exit "target"
```

Key changes:
- **No fixed-percentage stop.** ATR-multiple scales with volatility. Tight in low-vol, wider in high-vol.
- **Time cap raised** from 60s to 30 minutes (settings-controlled).
- **Trailing stop, not fixed target.** Let winners run; lock in once moved.
- **Optional profit-take off by default.** Let the trail do the work.

### Settings (all new, all overridable)

```python
exit_policy: str = "atr_trailed"  # "scalp" (legacy) | "atr_trailed" (new)
exit_atr_stop_mult: float = 1.5
exit_atr_trail_mult: float = 1.0
exit_atr_period_bars: int = 14
exit_max_hold_s: int = 1800        # 30 min
exit_profit_take_pct: float = 0.0  # 0 = off (let trail handle it); operator can override
```

`exit_policy="scalp"` retains current behaviour for A/B comparison.

### ATR calculation reuse

Use `fast_snapshots` table — 1-minute bars are already there. Compute simple ATR(14) on `(high - low)` per bar over the last 14 bars per ticker. Cache per-ticker so it isn't recomputed every book emit.

## Deliverables

D1. **`app/services/trading/fast_path/exit_manager.py`** (new file OR new function in executor.py — keep close to existing exit code)
- Pure function `compute_exit_decision(position, current_book, atr, settings, now) -> ExitDecision`.
- `ExitDecision = {action: "hold" | "exit_stop" | "exit_trail" | "exit_time" | "exit_target", price: float, reason: str}`.
- Time-cap takes precedence over trail (cleaner audit on the time-cap close cases).

D2. **`app/services/trading/fast_path/settings.py`**
- 6 new fields above + env loads.

D3. **`app/services/trading/fast_path/executor.py`**
- Replace the existing scalp exit path with a single call to `compute_exit_decision`, gated on `settings.exit_policy`. Legacy "scalp" branch retained.

D4. **`tests/test_fastpath_extended_hold_exit.py`**
- ATR stop fires when price drops `>= atr_stop_mult * atr` below entry.
- Trailing stop fires after price moves up and then retraces `>= atr_trail_mult * atr`.
- Time cap fires after `max_hold_s` regardless of PnL.
- `exit_policy="scalp"` preserves current behaviour bit-identical.
- Empty bar history (n < period_bars) falls back to a conservative fixed pct stop (no None propagation crash).

D5. **`docs/RUNBOOKS/FAST_PATH_EXIT_TUNING.md`** (new)
- How to A/B compare scalp vs atr_trailed in shadow log.
- The "raise max_hold_s, watch realized 30d, decide" loop.

## Hard constraints

- **No regression of scalp behaviour** when `exit_policy="scalp"`. Bit-identical to today.
- **Backwards-compat on the live-trading flag.** `CHILI_FAST_PATH_LIVE` remains the master kill switch; this brief doesn't touch live activation.
- **ATR calculation must not block the executor hot path.** Cache per-ticker, refresh every 60s, not every book emit.
- **No magic-default fallback when ATR can't be computed.** When `n_bars < period_bars`, fall back to fixed-pct stop AND log a warning. No silent neutral-default.

## Acceptance

- `exit_policy="atr_trailed"` (new) and `exit_policy="scalp"` (legacy) both work end-to-end in the executor.
- Tests pass.
- Post-deploy A/B (1 week each, shadow log):
  - Scalp baseline: realized win-rate, avg holding period, avg PnL per trade.
  - ATR-trailed: same metrics. Expect avg holding period 10-30 min, win-rate similar, **avg PnL per trade meaningfully higher** (the whole point).
- If atr_trailed shows ≥2× the avg PnL/trade of scalp (gross), operator promotes to default.

## Operator activation

After ship, default is `exit_policy="atr_trailed"` (the new behaviour). To run a shadow A/B:

1. Half the fast-path tickers on `exit_policy="atr_trailed"` (via a per-ticker override mechanism — not in this brief's scope; for now, run sequentially).
2. Compare realized stats from `fast_exits` table joined on `fast_executions`.
3. Decision after 2 weeks: keep new default, revert to scalp, or tune the ATR multipliers.
