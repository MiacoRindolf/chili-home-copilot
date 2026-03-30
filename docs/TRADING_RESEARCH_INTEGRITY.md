# Trading research integrity (CHILI)

## GPL and external tools

**Freqtrade** and similar projects may use **GPL-3.0**. Do **not** copy their source into CHILI unless you intend to meet copyleft obligations for the distributed whole (or obtain legal advice on boundaries). This is not legal advice.

CHILI instead **reads public documentation and ideas** (lookahead bias, data hygiene, walk-forward discipline) and **reimplements** small checks in [`app/services/trading/research_integrity.py`](../app/services/trading/research_integrity.py). Optional reference: [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) (documentation only).

## What runs automatically

- **Pattern backtests** (`run_pattern_backtest` / `backtest_pattern`): before results are returned, CHILI attaches **`data_provenance`** and **`research_integrity`** to the result dict.
- **`save_backtest`**: merges those blobs into `BacktestResult.params` JSON so stored rows stay auditable.
- **Promotion updates** (`learning.py`, `web_pattern_researcher.py`): aggregates integrity summaries into **`oos_validation_json.research_integrity`** on `ScanPattern`.

No manual “turn on integrity” step is required for the learning cycle or queue backtests.

## Configuration (`app/config.py`)

| Setting | Default | Purpose |
|--------|---------|--------|
| `brain_research_integrity_enabled` | `True` | Master switch; off skips checks (faster, no annotations). |
| `brain_research_integrity_strict` | `False` | When **True**, failed lookahead / causality checks **block promotion** to `promoted` (soft failures still log). |
| `brain_research_integrity_max_check_bars` | `48` | Max bar indices sampled for truncation vs precompute comparison. |

## Throughput vs strictness

Default behavior is **non-blocking**: failed checks set flags and warnings; they do **not** remove patterns unless **`brain_research_integrity_strict`** is enabled.

Avoid shipping a release that turns **strict** on at the same time as **tighter** OOS / promotion / proposal floors without recalibrating—otherwise you can starve the pipeline of candidates. Volume of daily setups is still primarily driven by **breadth** (`brain_queue_exploration_*`, universe, intraday miners, queue tiers), not by integrity flags.

## Stored JSON shapes

### `BacktestResult.params`

- **`data_provenance`**: `ticker`, `interval`, `period`, `ohlc_bars`, optional window timestamps, `rules_fingerprint`, `scan_pattern_id`, `provider_hint`.
- **`research_integrity`**: `lookahead_ok`, `causality_checked_bars`, `mismatches` (capped), `recursive_ok`, `warnings`.

Legacy rows may omit these keys; readers should treat absence as **`unknown`**, not invalid.

### `ScanPattern.oos_validation_json.research_integrity`

Aggregate over the last multi-ticker evaluation: e.g. `lookahead_ok_all`, `any_warnings`, `per_ticker` (capped list).

## Backfill

Optional: [`scripts/backfill_backtest_provenance.py`](../scripts/backfill_backtest_provenance.py) sets explicit `data_provenance.status = "unknown"` (and minimal keys) on rows missing provenance so UIs and gates can branch safely.
