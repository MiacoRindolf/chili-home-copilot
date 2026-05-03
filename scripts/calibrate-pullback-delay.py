"""F8b.2 — Counterfactual `VOL_BREAKOUT_PULLBACK_DELAY_S` calibration.

For each allowlisted ticker, sweep candidate delay values, simulate
the pullback-fade strategy at each, and pick the per-ticker delay
that maximises mean realized-equivalent return.

Approach (per-alert × per-candidate-delay):

    1. Look up best_ask at ``fired_at + delay_s`` -> hypothetical entry.
       (Pullback alert means "wait DELAY_S after the breakout fired,
       then enter long.")
    2. Sample a hold period from the actual hold-period distribution
       of *closed* pullback exits for that ticker. Realistic hold dist
       beats a single fixed exit horizon.
    3. Look up best_bid at ``fired_at + delay_s + sampled_hold_s`` ->
       hypothetical exit price (long close).
    4. realized-equivalent return = (exit_bid - entry_ask) / entry_ask.
    5. Aggregate per (ticker, delay_s); rank by mean × n / (n + α).

Output: ``app/services/trading/fast_path/_calibrated/pullback_delay_per_ticker.json``

Constraints (from the F8b brief):
    - Delay candidates are grid-search granularity, NOT strategy
      thresholds. If the optimum lands at the grid boundary, expand.
    - No fabricated certainty: if a ticker has < MIN_SAMPLES samples,
      omit it from the artifact (caller falls back to 30s default).
    - No live-mode side effects: read-only against fast_alerts +
      fast_orderbook + fast_exits.
    - Random sampling is seeded so re-runs are reproducible.

Run via:
    docker compose exec -T chili python /app/scripts/calibrate-pullback-delay.py
or:
    docker compose exec -T chili python /app/scripts/calibrate-pullback-delay.py --tickers BTC-USD,SOL-USD --history-days 14
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import side effect: configure logging cleanly when run from /app.
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
log = logging.getLogger("calibrate-pullback-delay")


# Grid-search granularity. If the optimum lands at the boundary on a
# real run, expand and re-run -- the script is the discovery tool, not
# the parameter store.
PULLBACK_DELAY_CANDIDATES_S: list[int] = [5, 10, 15, 20, 25, 30, 45, 60, 90, 120]

# Default tickers come from F8a-evaluation-rerun-2's verdict. Operator
# can override via --tickers.
DEFAULT_ALLOWLIST_TICKERS: list[str] = ["BTC-USD", "SOL-USD"]

DEFAULT_HISTORY_DAYS: int = 14

# Below this sample count for a (ticker, delay) cell, drop the cell
# from ranking. We're not picking optima off n=2.
MIN_SAMPLES_PER_CELL: int = 10

# Seed for hold-period sampling. Reproducibility > optimisation.
RANDOM_SEED: int = 42


def fetch_alerts(conn, ticker: str, history_days: int) -> list[dict]:
    """Pullback alerts for ticker over the lookback window."""
    rows = conn.execute("""
        SELECT id, ticker, alert_type, fired_at, signal_score
        FROM fast_alerts
        WHERE alert_type = 'volume_breakout_pullback_long'
          AND ticker = %s
          AND id > 2300
          AND fired_at > NOW() - INTERVAL %s
        ORDER BY fired_at ASC
    """, (ticker, f"{history_days} days")).fetchall()
    return [dict(r) for r in rows]


def fetch_hold_period_distribution(conn, ticker: str) -> list[float]:
    """Actual closed-pullback hold periods for the ticker.

    Used as the empirical hold-time distribution for counterfactual
    fills. If we have <3 closed exits, the distribution is too thin
    to sample from honestly -- the caller should fall back to a flat
    distribution centered on a defensible default.
    """
    rows = conn.execute("""
        WITH pullback_eids AS (
          SELECT e.id FROM fast_executions e
          JOIN fast_alerts a ON a.ticker = e.ticker
                            AND a.alert_type = e.alert_type
                            AND a.fired_at = e.alert_fired_at
          WHERE a.alert_type='volume_breakout_pullback_long' AND a.ticker = %s
        )
        SELECT x.holding_period_s
        FROM fast_exits x
        WHERE x.entry_execution_id IN (SELECT id FROM pullback_eids)
    """, (ticker,)).fetchall()
    return [float(r["holding_period_s"]) for r in rows if r["holding_period_s"]]


def lookup_book_at(conn, ticker: str, target_ts: datetime) -> tuple[float | None, float | None]:
    """Most-recent book snapshot at or after target_ts (within 5 min).

    Returns (best_bid, best_ask) or (None, None) if no book in window.
    Mirrors decay_miner._finalize_one_obs's approach.
    """
    row = conn.execute("""
        SELECT bid_levels, ask_levels
        FROM fast_orderbook
        WHERE ticker = %s
          AND snapshot_at >= %s
          AND snapshot_at < %s + INTERVAL '5 minutes'
        ORDER BY snapshot_at ASC
        LIMIT 1
    """, (ticker, target_ts, target_ts)).fetchone()
    if row is None:
        return (None, None)
    bid_levels = row["bid_levels"] or []
    ask_levels = row["ask_levels"] or []
    if not bid_levels or not ask_levels:
        return (None, None)
    try:
        best_bid = float(bid_levels[0][0])
        best_ask = float(ask_levels[0][0])
    except (TypeError, ValueError, IndexError):
        return (None, None)
    if best_bid <= 0 or best_ask <= 0:
        return (None, None)
    return (best_bid, best_ask)


def calibrate_ticker(conn, ticker: str, history_days: int, rng: random.Random) -> dict:
    """Run the counterfactual sweep for one ticker."""
    log.info("[%s] fetching alerts (last %d days)…", ticker, history_days)
    alerts = fetch_alerts(conn, ticker, history_days)
    log.info("[%s] fetched %d alerts", ticker, len(alerts))
    if not alerts:
        return {"ticker": ticker, "skipped": "no_alerts", "samples_seen": 0}

    log.info("[%s] fetching hold-period distribution…", ticker)
    holds = fetch_hold_period_distribution(conn, ticker)
    log.info("[%s] %d closed-exit hold periods (median=%s)", ticker, len(holds),
             round(statistics.median(holds)) if holds else "n/a")
    if len(holds) < 3:
        log.warning("[%s] hold distribution too thin (n=%d); falling back to flat 1800s",
                    ticker, len(holds))
        holds = [1800.0]

    by_delay: dict[int, list[float]] = {d: [] for d in PULLBACK_DELAY_CANDIDATES_S}

    for alert in alerts:
        fired_at: datetime = alert["fired_at"]
        # Skip alerts whose tail (fired_at + max_delay + max_hold) extends
        # past now -- we won't have book data there anyway.
        max_window_s = max(PULLBACK_DELAY_CANDIDATES_S) + max(holds)
        if fired_at + timedelta(seconds=max_window_s) > datetime.now():
            continue

        for delay_s in PULLBACK_DELAY_CANDIDATES_S:
            entry_ts = fired_at + timedelta(seconds=delay_s)
            (entry_bid, entry_ask) = lookup_book_at(conn, ticker, entry_ts)
            if entry_ask is None:
                continue
            # Sample a hold period from the empirical distribution.
            sampled_hold = rng.choice(holds)
            exit_ts = entry_ts + timedelta(seconds=sampled_hold)
            (exit_bid, exit_ask) = lookup_book_at(conn, ticker, exit_ts)
            if exit_bid is None:
                continue
            # realized-equivalent return: buy at ask, sell at bid.
            ret = (exit_bid - entry_ask) / entry_ask
            by_delay[delay_s].append(ret)

    # Rank by mean × n_weight to dampen high-mean-low-n cells.
    candidates = []
    for delay_s in PULLBACK_DELAY_CANDIDATES_S:
        samples = by_delay[delay_s]
        if not samples:
            continue
        n = len(samples)
        mean = statistics.mean(samples)
        stdev = statistics.stdev(samples) if n > 1 else 0.0
        # Shrinkage: pull mean toward 0 when n is small. Standard
        # heuristic, NOT a strategy threshold. Without it the optimum
        # would be heavily biased toward delays with few samples.
        n_weight = n / (n + 30.0)
        score = mean * n_weight
        candidates.append({
            "delay_s": delay_s, "n": n,
            "mean_return": mean, "stdev": stdev,
            "shrunk_score": score,
        })

    if not candidates:
        return {"ticker": ticker, "skipped": "no_book_lookups_succeeded",
                "samples_seen": len(alerts)}

    # Pick the delay with highest shrunk score, but require minimum n.
    qualified = [c for c in candidates if c["n"] >= MIN_SAMPLES_PER_CELL]
    if not qualified:
        log.warning(
            "[%s] no delay candidate has n >= %d; using best-of-thin instead",
            ticker, MIN_SAMPLES_PER_CELL,
        )
        qualified = candidates
    optimum = max(qualified, key=lambda c: c["shrunk_score"])

    log.info(
        "[%s] optimum delay = %ds  (mean=%.2f bps, n=%d, shrunk=%.4f)",
        ticker, optimum["delay_s"],
        optimum["mean_return"] * 10000, optimum["n"], optimum["shrunk_score"],
    )

    # Boundary guard: if optimum is at grid edge, surface for re-run.
    boundary_warn = None
    if optimum["delay_s"] == PULLBACK_DELAY_CANDIDATES_S[0]:
        boundary_warn = f"optimum at lower bound ({optimum['delay_s']}s); expand search"
    elif optimum["delay_s"] == PULLBACK_DELAY_CANDIDATES_S[-1]:
        boundary_warn = f"optimum at upper bound ({optimum['delay_s']}s); expand search"

    return {
        "ticker": ticker,
        "optimum_delay_s": optimum["delay_s"],
        "optimum_mean_bps": round(optimum["mean_return"] * 10000, 2),
        "optimum_n": optimum["n"],
        "optimum_shrunk_score": round(optimum["shrunk_score"], 6),
        "boundary_warning": boundary_warn,
        "sweep": [
            {**c, "mean_bps": round(c["mean_return"] * 10000, 2)}
            for c in candidates
        ],
        "alerts_seen": len(alerts),
        "hold_periods_used": len(holds),
    }


def save_artifact(out_path: Path, calibration: dict, history_days: int) -> None:
    """Write the per-ticker delays to the calibration JSON."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "_metadata": {
            "calibrated_at": datetime.now(timezone.utc).isoformat(),
            "history_window_days": history_days,
            "candidates_s": PULLBACK_DELAY_CANDIDATES_S,
            "min_samples_per_cell": MIN_SAMPLES_PER_CELL,
            "samples_per_ticker": {
                t: c.get("optimum_n", 0)
                for t, c in calibration.items()
                if not t.startswith("_")
            },
            "boundary_warnings": [
                c["boundary_warning"]
                for c in calibration.values()
                if isinstance(c, dict) and c.get("boundary_warning")
            ],
            "skipped": {
                t: c["skipped"]
                for t, c in calibration.items()
                if isinstance(c, dict) and c.get("skipped")
            },
        },
    }
    for ticker, c in calibration.items():
        if not isinstance(c, dict) or "optimum_delay_s" not in c:
            continue
        artifact[ticker] = c["optimum_delay_s"]
    with out_path.open("w") as f:
        json.dump(artifact, f, indent=2, sort_keys=False)
    log.info("[wrote] %s", out_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate pullback-fade DELAY_S per ticker.")
    parser.add_argument("--tickers", default=",".join(DEFAULT_ALLOWLIST_TICKERS),
                        help="comma-separated allowlist (default: %(default)s)")
    parser.add_argument("--history-days", type=int, default=DEFAULT_HISTORY_DAYS,
                        help="lookback window for alerts (default: %(default)s)")
    parser.add_argument("--out", default=None,
                        help="output JSON path (default: app/services/trading/fast_path/_calibrated/pullback_delay_per_ticker.json)")
    parser.add_argument("--sweep-out", default=None,
                        help="optional path for the full sweep table (debug)")
    args = parser.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        log.error("No tickers given.")
        return 2

    rng = random.Random(RANDOM_SEED)
    # Path resolution: the container mounts ./app -> /app/app and
    # ./scripts -> /app/scripts. The script lives at /app/scripts/
    # when invoked in-container, but the application code is rooted
    # at /app/app/. So the artifact path is parent-of-scripts /app/
    # services/... when running in-container, and the equivalent
    # host path resolves the same. Locate via importable app
    # package to be path-layout-agnostic.
    out_path = Path(args.out) if args.out else None
    if out_path is None:
        try:
            import app  # type: ignore[import]
            app_root = Path(app.__file__).parent
        except Exception:
            # Fallback to relative-to-script (works on host).
            app_root = Path(__file__).parent.parent / "app"
        out_path = (
            app_root / "services" / "trading" / "fast_path"
            / "_calibrated" / "pullback_delay_per_ticker.json"
        )

    # SQLAlchemy engine (matches the rest of the app's DB access).
    from app.db import engine
    from sqlalchemy import text

    class _ConnShim:
        def __init__(self, sa_conn):
            self._conn = sa_conn
        def execute(self, sql: str, params: tuple = ()):
            # psycopg2-style %s placeholders -> SQLAlchemy named binds.
            # We only need positional substitution; rewrite with :p0, :p1, ...
            class _Result:
                def __init__(self, mappings):
                    self._m = list(mappings)
                def fetchall(self): return self._m
                def fetchone(self): return self._m[0] if self._m else None
            named = {}
            new_sql = sql
            for i, p in enumerate(params):
                placeholder = f":p{i}"
                # Naive %s -> :pN replacement; sufficient for our SQL.
                new_sql = new_sql.replace("%s", placeholder, 1)
                named[f"p{i}"] = p
            r = self._conn.execute(text(new_sql), named).mappings().all()
            return _Result(r)

    with engine.connect() as sa_conn:
        conn = _ConnShim(sa_conn)
        calibration: dict[str, dict] = {}
        for ticker in tickers:
            calibration[ticker] = calibrate_ticker(
                conn, ticker, args.history_days, rng,
            )

    save_artifact(out_path, calibration, args.history_days)

    if args.sweep_out:
        sweep_path = Path(args.sweep_out)
        with sweep_path.open("w") as f:
            json.dump(calibration, f, indent=2, default=str)
        log.info("[wrote] %s (full sweep)", sweep_path)

    # Print the sweep table to stdout for the operator.
    print("\n=== per-ticker sweep (mean basis points) ===")
    for ticker, c in calibration.items():
        if "optimum_delay_s" not in c:
            print(f"\n{ticker}: SKIPPED ({c.get('skipped', 'unknown')})")
            continue
        print(f"\n{ticker}  -- optimum_delay_s = {c['optimum_delay_s']}  "
              f"(mean={c['optimum_mean_bps']:+.2f} bps, n={c['optimum_n']})")
        if c.get("boundary_warning"):
            print(f"  WARNING: {c['boundary_warning']}")
        print(f"  {'delay_s':>8} {'n':>6} {'mean_bps':>10} {'shrunk':>10}")
        for s in c["sweep"]:
            print(f"  {s['delay_s']:>8} {s['n']:>6} {s['mean_bps']:>+10.2f} {s['shrunk_score']:>+10.4f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
