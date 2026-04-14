# RSI + Fib 0.382 + FVG Pullback Pattern

## Overview

A **bullish pullback continuation** pattern seeded into Chili's trading brain.
The setup requires:

1. **HTF RSI > 75** (higher timeframe shows strong momentum)
2. **LTF RSI > 50** (lower timeframe still supportive)
3. **Pullback to Fib 0.382** of the most recent impulse leg
4. **FVG in confluence** with the Fibonacci zone

Origin: Filipino trader community pullback criteria (Tagalog seed).

## Pattern Name

`RSI + Fib 0.382 + FVG Pullback (Tagalog seed)`

Stored in `_COMMUNITY_SEED_PATTERNS` in
`app/services/trading/pattern_engine.py` with `origin = "user_seeded"`.

## Conditions (rules\_json)

| # | indicator | op | value | Notes |
|---|---|---|---|---|
| 1 | `1d:rsi_14` | `>` | `75` | HTF RSI via cross-TF prefix |
| 2 | `rsi_14` | `>` | `50` | LTF RSI (native timeframe) |
| 3 | `fib_382_zone_hit` | `==` | `True` | Close is within tolerance of Fib 0.382 |
| 4 | `fvg_fib_confluence` | `==` | `True` | Active FVG overlaps the Fib zone |

The `meta` block stores configurables: `htf`, `ltf`, `fib_target`,
tolerances, and the `requires_cross_tf` flag that triggers HTF indicator
injection during scanning.

## Reusable Capabilities Added

### 1. Fibonacci Retracement (`app/services/trading/fibonacci.py`)

Pure-function module:

- `find_swing_highs` / `find_swing_lows` -- fractal pivot detection
- `find_impulse_leg` -- most recent bull/bear impulse from swing pivots
- `compute_fib_levels` -- retracement price levels from anchor pair
- `check_fib_level_hit` -- tolerance-aware zone check
- `compute_fib_retracement_series` -- bar-by-bar indicator arrays for
  backtests and live scans

### 2. Fair Value Gap (`app/services/trading/fvg.py`)

- `detect_fvg_records` -- 3-candle bullish/bearish FVG detection
- `compute_fvg_series` -- bar-by-bar active-FVG indicator arrays with
  mitigation tracking
- `check_fvg_fib_confluence` -- overlap check between FVG zone and fib level
- `compute_fvg_fib_confluence_series` -- bar-by-bar confluence arrays

### 3. Cross-Timeframe Evidence (`app/services/trading/cross_timeframe.py`)

Extends `mtf_consensus.py` with **asymmetric** condition support:

- `CrossTimeframeEvidence` -- structured dataclass for same-ticker evidence
- `fetch_cross_timeframe_evidence` -- OHLCV + indicator fetch for HTF/LTF
- `eval_cross_timeframe_conditions` -- different conditions per timeframe
- `build_cross_tf_snapshot_keys` -- flatten into prefixed indicator dict

### 4. Pullback Detector (`app/services/trading/pullback_detector.py`)

Orchestrates the full detection pipeline and returns structured evidence for
persistence, learning, and UI display.

## Integration Points

### Indicator Core (`indicator_core.py`)

New keys computed lazily when `needed` includes them:

- `fib_382_zone_hit`, `fib_382_level`, `impulse_high`, `impulse_low`
- `fvg_present`, `fvg_high`, `fvg_low`
- `fvg_fib_confluence`, `fvg_fib_distance_pct`

### Backtest Service (`backtest_service.py`)

`_compute_series_for_conditions` computes the same keys, ensuring
backtest/live parity.

### Scanner (`scanner.py`)

`_enrich_snapshot_cross_tf` injects cross-TF indicators and structural
series when any active pattern has `meta.requires_cross_tf = true`.

## Evidence Persistence

The detector returns a dict containing:

- `ticker`, `side`, `htf`, `ltf`
- HTF/LTF RSI values and timestamps
- Impulse leg anchors and bar count
- Fib levels computed, target level, tolerance
- FVG bounds and direction
- FVG-Fib confluence result
- Coherence check and evidence age
- Human-readable `reasons` list

This evidence fits into existing persistence structures:
- `ScanPattern.rules_json` (pattern definition + meta)
- `PatternTradeRow.features_json` (per-trade structural features)
- `TradingInsight` / `TradingInsightEvidence` (pattern-level evidence)

No new database tables or migrations were required.

## Configurability

All defaults can be overridden via `rules_json.meta` or `pullback_detector`
config:

| Parameter | Default | Description |
|---|---|---|
| `htf` | `"1d"` | Higher timeframe |
| `ltf` | `"1h"` | Lower timeframe |
| `htf_rsi_threshold` | `75` | Minimum HTF RSI |
| `ltf_rsi_threshold` | `50` | Minimum LTF RSI |
| `fib_target` | `0.382` | Target Fibonacci level |
| `fib_tolerance_pct` | `0.5` | % tolerance around fib level |
| `fvg_fib_overlap_tolerance_pct` | `0.5` | FVG/Fib overlap tolerance |
| `impulse_lookback` | `50` | Bars to search for impulse |
| `fvg_lookback` | `20` | Bars to search for active FVG |
| `pivot_lookback` | `5` | Window for swing pivot detection |

## Local Validation

### Run Tests

```powershell
conda activate chili-env
python -m pytest tests/test_fibonacci.py tests/test_fvg.py tests/test_cross_timeframe.py tests/test_rsi_fib_fvg_pullback.py -v
```

### Verify Pattern Seeding

After starting the server, the pattern appears in `scan_patterns`:

```sql
SELECT name, origin, timeframe, rules_json
FROM scan_patterns
WHERE name LIKE '%Fib 0.382%';
```

### Verify in Brain UI

Navigate to the Brain page (`/brain`). The pattern should appear in the
learned-patterns list after a scan or learning cycle processes it. The
evidence modal shows the structural evidence (Fib levels, FVG bounds,
cross-TF RSI values).

### Test Detection Manually

```python
from app.services.trading.pullback_detector import detect_rsi_fib_fvg_pullback
result = detect_rsi_fib_fvg_pullback("AAPL")
print(result)  # None if setup not present, else evidence dict
```
