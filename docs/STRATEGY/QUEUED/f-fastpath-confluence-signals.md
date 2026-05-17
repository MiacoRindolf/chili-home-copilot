# f-fastpath-confluence-signals

STATUS: QUEUED
SLUG: fastpath-confluence-signals
PROPOSED: 2026-05-17
REQUESTED_BY: architect re-eval after the emit_short_alerts gate (`02cb278`)
PREREQUISITE_FOR: f-fastpath-realized-decay-conditioned-admission (sharper gating requires the higher-quality alert stream this brief produces)

## TL;DR

The fast-path scanner emits 4 (post-gate) alert types independently: `imbalance_long`, `volume_breakout_long`, `volume_breakout_pullback_long`, `spread_squeeze`. Their realized signal-score gap is enormous: `imbalance_long` averages 0.36 (basically random) vs `spread_squeeze` 0.63 and `volume_breakout_long` 0.64. **Trade only confluence events** — ≥2 alert types firing on the same ticker within 60 seconds — instead of every single alert. This is the cheapest selectivity gain available without changing venues.

## Why

Per the 2026-05-16 probe:

| alert_type | n/24h (post emit_short gate) | avg signal_score |
|---|---:|---:|
| imbalance_long | ~3,000 (91%) | **0.363** |
| volume_breakout_pullback_long | ~94 (2.9%) | 0.648 |
| volume_breakout_long | ~87 (2.6%) | 0.641 |
| spread_squeeze | ~103 (3.1%) | 0.627 |

Imbalance dominates the volume but has half the quality. The `negative_edge` reject rate at the cost-aware gate (~85% of all rejects post emit_short fix) is concentrated on the imbalance stream because cost-aware sees its weak signal vs. the 120 bps round-trip.

**Confluence as a selectivity multiplier:** when imbalance_long fires on a ticker AT the same time as `spread_squeeze` (i.e., spread just compressed AND book is tilted long), the joint event has materially better expected edge than either component alone. The conditional probability of follow-through given the joint event is much higher than the marginal probability given imbalance alone.

This is well-known in the literature (multi-feature signal stacking), and it costs ~100 LOC.

## Design

### New alert type: `confluence_long`

Fires when ≥2 of the long-side alert types co-occur on the same ticker within a rolling window (default 60s):

- `imbalance_long`
- `volume_breakout_long`
- `volume_breakout_pullback_long`
- `spread_squeeze`

The component alerts continue to emit normally (for calibration / observation / future ML feature use). Only `confluence_long` is **trade-eligible** when the new flag is on; the others stay in shadow-log calibration mode.

### Per-ticker confluence window tracking

In `MomentumScanner.__init__`:

```python
self._confluence_window_s = float(window_s)  # ctor kwarg, default 60.0
self._recent_alerts: dict[str, deque[tuple[str, datetime, float]]] = {}
# ticker -> deque[(alert_type, fired_at, signal_score)] within window_s
```

Every time the scanner emits a long-side alert, push `(alert_type, fired_at, signal_score)` to `self._recent_alerts[ticker]` and prune entries older than `window_s`. Then check whether the dedupe-set has ≥2 distinct types — if yes, emit a synthetic `confluence_long` alert with `signal_score = weighted_combo` and `features = {component_alerts: [...], component_scores: [...]}`.

### Confluence score

Weighted by component quality (per the table above):

```python
WEIGHTS = {
    "imbalance_long": 0.20,
    "volume_breakout_long": 0.40,
    "volume_breakout_pullback_long": 0.30,
    "spread_squeeze": 0.30,
}
# composite = clip(sum(WEIGHTS[t] * score[t] for t in components), 0, 1)
# +0.20 bonus per additional component beyond 2 (max 3 components)
```

### Trade-eligibility flag

`settings.confluence_only_for_trading: bool = False` (default OFF — preserve current behaviour through one release of observation; operator flips after seeing confluence-alert rate stabilise).

In `executor.py` decision path: when flag is on, reject every alert whose `alert_type` is not `confluence_long` with `reject_reason="non_confluence:single_signal_when_confluence_required"`. This keeps the component alerts firing into the calibration tables while gating trading to the high-quality joint events.

### Suppression of duplicate confluence emissions

Per-ticker confluence cooldown (default 30s — longer than imbalance's 30s) so a sustained multi-signal regime doesn't spam confluence alerts every tick.

## Deliverables

D1. **`app/services/trading/fast_path/scanner.py`**
- Add `_recent_alerts` dict + `_confluence_window_s` ctor kwarg (default 60).
- Hook into existing alert emission sites to push into the window.
- New `_check_confluence(ticker, now)` method called after each emit; returns optional `confluence_long` alert.
- Counter `fired_confluence_long` surfaced via `.stats()`.

D2. **`app/services/trading/fast_path/settings.py`**
- `confluence_window_s: float = 60.0` + env load `CHILI_FAST_PATH_CONFLUENCE_WINDOW_S`.
- `confluence_only_for_trading: bool = False` + env load `CHILI_FAST_PATH_CONFLUENCE_ONLY_FOR_TRADING`.

D3. **`app/services/trading/fast_path/ws_client.py`**
- Pass `confluence_window_s` to `MomentumScanner` constructor.

D4. **`app/services/trading/fast_path/executor.py`**
- New reject reason `non_confluence:single_signal_when_confluence_required` when `confluence_only_for_trading=True` and alert is not `confluence_long`.
- No structural rewiring — single early-return at the top of the decision path.

D5. **`tests/test_fastpath_confluence_signals.py`**
- Two single-signal alerts within window → confluence fires.
- One single-signal alert → no confluence.
- Two alerts >window_s apart → no confluence (window pruning works).
- Same alert_type twice (not 2 distinct types) → no confluence (dedupe on type).
- Three distinct types → confluence with +0.20 bonus.
- `confluence_only_for_trading=True` rejects non-confluence in executor; confluence passes.

## Hard constraints

- **Component alerts keep emitting** during shadow window. The whole point is to gather calibration data for confluence in parallel with single-signal trading until the operator flips.
- **No DB schema change.** `confluence_long` reuses the existing `fast_alerts.alert_type` text column.
- **Backwards-compat ctor kwarg** on `MomentumScanner` — default behaviour unchanged when called without kwargs.
- **Confluence detection is O(n) per emission** where n is the rolling-60s alert count per ticker. With pruning at every emit, deque size stays bounded.

## Acceptance

- Confluence alerts fire when 2+ distinct types co-occur within window; counted via `fired_confluence_long` in stats.
- `confluence_only_for_trading=False` (default) behaviour: nothing changes downstream; component alerts still place orders.
- `confluence_only_for_trading=True` behaviour: only `confluence_long` makes it past the executor gate; component alerts are rejected with the new reject reason and continue to populate `fast_signal_decay`.
- Tests pass.
- Post-deploy 7-day shadow window: `fast_executions` shows confluence rate of 5-50 per day (single-digit % of component-alert rate). Operator decides whether to flip the trading flag based on observed quality.

## Operator activation

After this ships:
1. Deploy. Confluence emits start logging in `fast_alerts` immediately.
2. Wait 7 days. Probe daily: `SELECT COUNT(*) FROM fast_alerts WHERE alert_type='confluence_long' AND fired_at > NOW() - INTERVAL '24 hours';`. Expect 5-50/day.
3. Check `fast_signal_decay` rows accumulating for `alert_type='confluence_long'`. Compare realized 5m/15m/30m returns vs the component-alert buckets. If confluence has 2-5x better realized edge than imbalance_long (the expected outcome), proceed.
4. Flip `CHILI_FAST_PATH_CONFLUENCE_ONLY_FOR_TRADING=true` in `.env` + `docker compose up -d --force-recreate fast-data-worker autotrader-worker`.
5. Watch `fast_executions` for the `non_confluence:*` reject rate jump as expected. Component alerts now shadow-only.
