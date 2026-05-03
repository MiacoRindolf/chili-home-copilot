# NEXT_TASK: f8b-pullback-allowlist-and-calibrate

STATUS: DONE

## Goal

Convert the F8a-evaluation-rerun-2 finding ("subset-supported on {BTC, SOL}") into actionable signal scope:

1. **Add a ticker-allowlist gate** that permits `volume_breakout_pullback_long` paper fills only on `{BTC-USD, SOL-USD}` and blocks the rest with a clear gate-reject reason. Static gate, no calibration dependency.
2. **Derive per-ticker `VOL_BREAKOUT_PULLBACK_DELAY_S` from counterfactual analysis** of existing `fast_orderbook` data — sweep candidate delay values, score by counterfactual realized-equivalent return, pick the delay that maximizes per-ticker. **No magic number; data-derived.**
3. **Apply the calibrated per-ticker delays** as a gate-time-of-eval lookup, not a code constant. Future re-calibration just re-runs the counterfactual.

This is the first F-series task that moves toward production-eligible signal scope. **Default mode stays paper** — no live placement enabled. The 8 safety belts stay intact.

After this task:

1. **Pullback fills land only on the verdict-supported subset** {BTC, SOL}. ETH/DOGE/AVAX get gate-rejected with reason `pullback_ticker_not_allowed`.
2. **Per-ticker DELAY_S is data-derived** from counterfactual optimization, not hand-set. Output committed as a config artifact (e.g., `app/services/trading/fast_path/_calibrated/pullback_delay_per_ticker.json`) that the gate reads.
3. **The next 24h+ soak produces realized P/L exclusively on the positive subset.** F8b's verification: realized aggregate should be unambiguously positive (BTC-USD: +5.66 bps was the prior baseline; expected to maintain or improve).

Up to 3 commits: gate, calibration script, calibration output.

## Why now

- F8a-evaluation-rerun-2 verdict: realized P/L is verdict-grade at n=43; bimodal split is structural (BTC/SOL+ vs ETH/DOGE−). The fade hypothesis is *subset-supported*, not blanket-supported.
- F-hygiene-4 closed cleanly. Hypothesis C (price-column mismatch) fixed for DOGE. Hypothesis B (horizon mismatch) deferred to f-hygiene-5; **doesn't block F8b** because F8b operates on the realized-P/L lens (post-exit truth), not the miner-mean lens (the predictor with the 30 bps systematic disagreement).
- Continued soak on the full ticker set produces more negative-edge data on ETH/DOGE without changing strategic information. Better to scope down and sharpen.
- Operator's stated discipline ("no magic numbers"): hard-coding DELAY_S=30 is a magic number. Counterfactual calibration derives it from data.

## Architectural commitments

- **Static gate + data-derived calibration.** The allowlist gate is a static condition (ticker IN allowlist). The per-ticker DELAY_S is read from a calibration artifact that's regenerated on demand.
- **Default mode stays paper. No live-placement enable.** The 8 safety belts continue gating any future live transition.
- **No new strategy thresholds beyond what the calibration produces.** Per-ticker DELAY_S replaces the global constant; that's a simplification, not addition.
- **No miner / scanner / executor / gate refactor** beyond the explicit additions.
- **No migrations.**
- **Counterfactual analysis is offline, repeatable, and committed as a script.** Not a one-off operator computation.

## Scope

### Commit 1: Ticker-allowlist gate for `volume_breakout_pullback_long`

**File:** `app/services/trading/fast_path/gates.py`

**What:**

Add a new gate (or extend an existing one) that checks whether the alert's ticker is in the `volume_breakout_pullback_long` allowlist. Order it AFTER calibrated gates (`gate_negative_edge_excluded`, `gate_calibrated_tradeability`) but BEFORE the executor-side decision.

```python
# F8b: per-signal-class ticker allowlist
PULLBACK_LONG_ALLOWLIST: frozenset[str] = frozenset({"BTC-USD", "SOL-USD"})

def gate_pullback_ticker_allowed(alert: AlertContext) -> GateResult:
    """Block volume_breakout_pullback_long for tickers not in F8b's allowlist.

    F8a-evaluation-rerun-2 verified n=43 realized exits with bimodal per-ticker
    edge: BTC/SOL positive (+4.22 bps avg, n=21), ETH/DOGE negative (-10 bps avg,
    n=22). Restricting to the positive subset is necessary to make the signal
    class production-eligible.
    """
    if alert.alert_type != "volume_breakout_pullback_long":
        return GateResult(passed=True, reason="not_pullback_long_signal")
    if alert.ticker in PULLBACK_LONG_ALLOWLIST:
        return GateResult(passed=True, reason="ticker_allowed")
    return GateResult(
        passed=False,
        reason=f"pullback_ticker_not_allowed:{alert.ticker}",
    )
```

Wire into the gate stack at the right position (verify against existing order; should be near `gate_calibrated_tradeability` or after `gate_negative_edge_excluded`).

**Constraint:** Don't extend the allowlist to other tickers without explicit Cowork direction. Don't make the allowlist parameterizable via config in this commit (premature). Hard-coded set is fine for now; if/when we add more signal classes with different allowlists, extract to config.

**Verification:**

After deploy + 30 min of soak:

```sql
-- Pullback alerts vs paper fills, per ticker
WITH pullback_eids AS (
  SELECT e.id FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker
                    AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long'
    AND a.fired_at > NOW() - INTERVAL '30 minutes'
)
SELECT a.ticker,
       COUNT(*) AS alerts,
       COUNT(DISTINCT e.id) AS paper_fills
FROM fast_alerts a
LEFT JOIN fast_executions e ON e.id IN (SELECT id FROM pullback_eids)
                            AND e.ticker=a.ticker
WHERE a.alert_type='volume_breakout_pullback_long'
  AND a.fired_at > NOW() - INTERVAL '30 minutes'
GROUP BY a.ticker;
```

Expected: BTC-USD and SOL-USD have alerts > 0 AND paper_fills > 0; ETH/DOGE/AVAX have alerts > 0 AND paper_fills = 0. The gate-reject log lines should show `reason=pullback_ticker_not_allowed:ETH-USD` (etc.) for blocked alerts.

### Commit 2: Counterfactual DELAY_S sweep script

**File:** `scripts/calibrate-pullback-delay.py` (new)

**What:**

Offline analysis script that:
- Reads `fast_alerts` for a specific ticker + signal class over a configurable history window (default: last 14 days).
- For each alert, looks up `fast_orderbook` at multiple candidate delay values (e.g., `{5, 10, 15, 20, 25, 30, 45, 60, 90, 120}` seconds).
- Computes the counterfactual entry price (best_bid for long) at each delay.
- Looks up the realized exit price (best_bid at hold-period-after-fire) for actual closed trades.
- For alerts that didn't get filled (gate-rejected), simulates a hypothetical fill at the candidate delay's entry price and uses the same hold-period-distribution from actual trades to estimate realized return.
- Per-ticker, finds the delay value that maximizes mean realized-equivalent return weighted by sample count.
- Outputs the optimum to `app/services/trading/fast_path/_calibrated/pullback_delay_per_ticker.json`.

```python
# Pseudocode
PULLBACK_DELAY_CANDIDATES_S = [5, 10, 15, 20, 25, 30, 45, 60, 90, 120]
HISTORY_WINDOW_DAYS = 14

def calibrate(ticker: str) -> int:
    alerts = fetch_alerts(ticker, alert_type="volume_breakout_pullback_long")
    by_delay: dict[int, list[float]] = {d: [] for d in PULLBACK_DELAY_CANDIDATES_S}
    for alert in alerts:
        for delay_s in PULLBACK_DELAY_CANDIDATES_S:
            entry_price = book_lookup(alert.ticker, alert.fired_at + delay_s, "best_bid")
            exit_price = synthesize_exit_price(alert)  # uses actual hold-period dist
            if entry_price and exit_price:
                ret = (exit_price - entry_price) / entry_price
                by_delay[delay_s].append(ret)
    # Pick delay with highest n-weighted mean return
    optimum_s = max(
        by_delay.keys(),
        key=lambda d: (mean(by_delay[d]) if by_delay[d] else float("-inf")),
    )
    return optimum_s

def main() -> None:
    out = {ticker: calibrate(ticker) for ticker in ALLOWLIST_TICKERS}
    save_calibration_artifact(out)
```

**Constraint:** No magic candidate set. The candidate list `[5, 10, 15, ...]` is *itself* a hyperparameter — but it's grid-search granularity, not a strategy threshold. Document it inline; if the optimum lands at the boundary (5 or 120), expand the search.

**Verification:**

Run the script on a copy of production data. Output should produce a JSON with per-ticker optima:

```json
{
  "BTC-USD": 30,
  "SOL-USD": 45,
  "_metadata": {
    "calibrated_at": "2026-05-03T...",
    "history_window_days": 14,
    "candidates_s": [5, 10, 15, 20, 25, 30, 45, 60, 90, 120],
    "samples_per_ticker": {"BTC-USD": 47, "SOL-USD": 62}
  }
}
```

Numbers will differ; the actual optima are the artifact.

### Commit 3: Wire the calibration artifact into the executor

**File:** `app/services/trading/fast_path/executor.py` (or wherever DELAY_S is consumed)

**What:**

Replace the global `VOL_BREAKOUT_PULLBACK_DELAY_S` constant with a per-ticker lookup that reads the calibration artifact at startup:

```python
import json
from pathlib import Path

CALIBRATION_PATH = Path(__file__).parent / "_calibrated" / "pullback_delay_per_ticker.json"

def _load_pullback_delay_calibration() -> dict[str, int]:
    """Load per-ticker pullback delay from calibration artifact."""
    if not CALIBRATION_PATH.exists():
        logger.warning(
            "[fast_path] pullback delay calibration artifact missing; "
            "using fallback constant 30s for all tickers"
        )
        return {}
    with CALIBRATION_PATH.open() as f:
        data = json.load(f)
    return {k: int(v) for k, v in data.items() if not k.startswith("_")}

_PULLBACK_DELAY_PER_TICKER = _load_pullback_delay_calibration()

def get_pullback_delay_s(ticker: str) -> float:
    """Per-ticker pullback delay. Falls back to 30s if not calibrated."""
    return float(_PULLBACK_DELAY_PER_TICKER.get(ticker, 30))
```

Replace direct uses of `VOL_BREAKOUT_PULLBACK_DELAY_S` with `get_pullback_delay_s(ticker)` in the scanner / executor.

**Constraint:** Fallback to 30s is the only static number; everything else is data-derived. The fallback exists so the system doesn't crash if the artifact is missing — but it shouldn't be the operative value in normal operation.

**Verification:**

Restart fast-data-worker. First scanner tick should log:

```
[fast_path] pullback delay loaded: BTC-USD=30s SOL-USD=45s (artifact=...)
```

Subsequent pullback alerts use the per-ticker delay.

## Brain integration (reuse, don't rewrite)

- Existing gate stack — extend in place; add new gate at the right ordering position.
- `fast_orderbook` table — read-only for the counterfactual sweep.
- `fast_alerts` + `fast_executions` + `fast_exits` — read-only for the actual-trade reference.
- Scanner / executor — single substitution of constant lookup → function lookup.
- Calibration artifact pattern — establishes the convention for future signal-class calibrations.

## Constraints / do not touch

- **All 8 live-placement safety belts.** Untouched. Default mode stays paper.
- **No live-mode flip.** This task does not enable live placement on {BTC, SOL} or anyone else.
- **No threshold tuning beyond DELAY_S calibration.** `VOL_BREAKOUT_MULT`, MIN_SAMPLES, score-bucket cutoffs, etc. all unchanged.
- **No producer-side change to `fast_alerts`.** Catchup-batch dups stay.
- **No miner code changes.** F-hygiene-5 (structural B fix) is a separate task; can run in parallel.
- **No migrations.** No schema work.
- **No global state.** Calibration artifact is read at startup; if regenerated, requires container restart (acceptable cost).
- **`models/trading.py`, `.env.example`.** Continue to leave alone.
- **Don't extend allowlist to ETH/DOGE/AVAX.** F8a-evaluation-rerun-2 explicitly named those as negative-edge.

## Out of scope

- Live placement enablement. Separate task with its own approval gate.
- f-hygiene-5 (structural B fix). Can run in parallel; doesn't conflict.
- f-leak-3. Still conditional on next OOM event.
- F9 signal redesign. After F8b's verification.
- Cross-pair correlation analysis.
- Auto-recalibration on a schedule. Manual re-run is fine for now.
- A `re-run calibration` API endpoint. Manual script invocation suffices.
- Adjusting `VOL_BREAKOUT_MULT` per-ticker. Different lever; separate decision.

## Success criteria

1. `git log --oneline -5` shows up to 3 new commits, pushed: gate, calibration script, calibration artifact + executor wire-up. The calibration artifact JSON is committed (under `_calibrated/`) so future sessions can read its history.
2. After deploy + 30 min: ETH/DOGE/AVAX pullback alerts have 0 paper fills (all gate-rejected with `pullback_ticker_not_allowed:<ticker>`); BTC/SOL pullback alerts produce paper fills as before.
3. Calibration artifact exists at `app/services/trading/fast_path/_calibrated/pullback_delay_per_ticker.json` with at minimum `BTC-USD` and `SOL-USD` entries.
4. The first scanner tick after restart logs the per-ticker delays loaded from the artifact.
5. F8a soak interruption is documented (~30s for the restart).
6. `docs/STRATEGY/CC_REPORTS/<date>_f8b-pullback-allowlist-and-calibrate.md` written with:
   - Per-ticker calibration optima from the script.
   - Sample-count and mean-return at each candidate delay (the sweep table).
   - First 30 min of post-deploy gate-reject lines for ETH/DOGE/AVAX.
   - Recommendation for next NEXT_TASK (verify via 24h soak, then maybe live-eligibility decision).

## Open questions for Cowork (surface in your report only if relevant)

1. **If the calibrated optimum differs significantly per ticker** (e.g., BTC=15s, SOL=90s), that's strategically informative — different microstructure absorbs the breakout at different rates. Surface explicitly with the magnitude of the difference.

2. **If the calibration produces a very small sample count per ticker** (e.g., n=3 for SOL), the optimum is unreliable. Default to keeping the 30s fallback for that ticker until more samples accumulate. Don't fabricate certainty.

3. **If the optimum lands at the search-grid boundary** (5s or 120s), expand the search and re-run. The grid-search bounds are educated guesses; if reality is outside them, follow the data.

4. **If counterfactual optimization shows that even the optimum has negative or near-zero realized-equivalent return**, that means {BTC, SOL} aren't actually a positive subset — the realized P/L pattern was noise at low n. Surface explicitly; F9 becomes more attractive than continued F8b iteration.

5. **The gate-reject reason `pullback_ticker_not_allowed:<ticker>`** could be made more informative (e.g., include the realized P/L it was filtered out for). Premature optimization; basic reason is fine for now.

## Rollback plan

- Commit 1 (gate): single localized change. Revert removes the allowlist; pullback fills resume on all 5 tickers.
- Commit 2 (calibration script): purely additive; revert removes the script.
- Commit 3 (artifact + wire-up): two parts. Revert the artifact alone falls back to the 30s default for all tickers; revert the wire-up restores the global constant. Both revertible without data loss.
- F8a soak interruption ~30s; resumes immediately.
- No migrations. No data migrations. No schema changes.
- No live-placement risk: gate is restrictive (blocks tickers); calibration changes a delay value within paper mode.
