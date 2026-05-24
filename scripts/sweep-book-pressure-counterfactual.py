"""Replay recent fast_orderbook rows and rank book-pressure variants.

Read-only. By default, the sweep uses the latest ranked active/shadow
fast-path universe and the latest universe rotation timestamp. Candidate
thresholds are derived from observed book-window quantiles, plus the current
scanner settings as a baseline.

Run locally:
    python scripts/sweep-book-pressure-counterfactual.py --limit 20

Run in Docker:
    docker compose exec -T fast-data-worker python /app/scripts/sweep-book-pressure-counterfactual.py --limit 20
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import bindparam, text

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("CHILI_APP_NAME", "chili-book-pressure-counterfactual")

from app.db import engine
from app.services.trading.fast_path.book_pressure_counterfactual import (
    build_windows,
    derive_quantile_variants,
    evaluate_variants,
    evenly_spaced_quantiles,
    observation_from_book_row,
    variant_from_settings,
)
from app.services.trading.fast_path.decay_miner import HORIZONS_S
from app.services.trading.fast_path.fees import fee_bps_for_execution_mode
from app.services.trading.fast_path.settings import load as load_fast_path_settings


def _latest_ranked_universe(conn) -> list[str]:
    rows = conn.execute(text("""
        WITH latest AS (
            SELECT MAX(rotation_at) AS ts FROM fast_path_universe
        )
        SELECT ticker
        FROM fast_path_universe
        WHERE rotation_at = (SELECT ts FROM latest)
          AND (
              status = 'active'
              OR (status = 'shadow' AND rank IS NOT NULL)
          )
        ORDER BY rank ASC NULLS LAST, ticker
    """)).fetchall()
    return [str(row.ticker) for row in rows]


def _latest_rotation_at(conn) -> datetime | None:
    value = conn.execute(text("""
        SELECT MAX(rotation_at) AS rotation_at FROM fast_path_universe
    """)).scalar()
    return value if isinstance(value, datetime) else None


def _fetch_book_rows(
    conn,
    *,
    tickers: list[str],
    since: datetime,
) -> list[dict[str, Any]]:
    stmt = text("""
        SELECT ticker, snapshot_at, bid_levels, ask_levels,
               bid_total_size, ask_total_size, imbalance, spread_bps
        FROM fast_orderbook
        WHERE snapshot_at >= :since
          AND ticker IN :tickers
        ORDER BY ticker, snapshot_at
    """).bindparams(bindparam("tickers", expanding=True))
    return [
        dict(row)
        for row in conn.execute(stmt, {"since": since, "tickers": tickers})
        .mappings()
        .all()
    ]


def _parse_horizons(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return tuple(int(h) for h in HORIZONS_S)
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(max(1, int(part)))
    return tuple(out)


def _print_results(rows, *, limit: int) -> None:
    print("\nTop counterfactual variants")
    print(
        "variant | horizon_s | n | verdict | mean_net_bps | "
        "lower_net_bps | upper_net_bps | gross_mean_bps | tickers"
    )
    print("-" * 118)
    for row in rows[:limit]:
        tickers = ",".join(
            f"{ticker}:{count}"
            for ticker, count in row.triggered_by_ticker.items()
        )
        print(
            f"{row.variant_name} | {row.horizon_s} | {row.sample_count} | "
            f"{row.verdict} | {_fmt(row.mean_net_bps)} | "
            f"{_fmt(row.lower_net_bps)} | {_fmt(row.upper_net_bps)} | "
            f"{_fmt(row.gross_mean_bps)} | {tickers}"
        )


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.4f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--history-minutes",
        type=int,
        default=0,
        help=(
            "Lookback override. Default 0 means use latest universe rotation."
        ),
    )
    parser.add_argument(
        "--tickers",
        default="",
        help="Comma-separated ticker override. Default uses latest ranked universe.",
    )
    parser.add_argument(
        "--grid-points",
        type=int,
        default=0,
        help=(
            "Number of observed-distribution quantile variants. "
            "Default 0 uses scanner_book_pressure_window."
        ),
    )
    parser.add_argument(
        "--horizons",
        default="",
        help="Comma-separated horizon seconds. Default uses decay miner horizons.",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    settings = load_fast_path_settings()
    execution_mode = str(getattr(settings, "execution_mode", "taker") or "taker")
    fee_bps, fee_detail = fee_bps_for_execution_mode(settings, execution_mode)
    horizons = _parse_horizons(args.horizons)
    grid_points = int(args.grid_points or 0)
    if grid_points <= 0:
        grid_points = max(1, int(settings.scanner_book_pressure_window or 1))

    with engine.connect() as conn:
        tickers = [
            t.strip().upper() for t in str(args.tickers or "").split(",")
            if t.strip()
        ]
        if not tickers:
            tickers = _latest_ranked_universe(conn)
        latest_rotation = _latest_rotation_at(conn)
        if args.history_minutes and args.history_minutes > 0:
            since = datetime.utcnow() - timedelta(minutes=args.history_minutes)
            since_basis = f"last_{args.history_minutes}_minutes"
        elif latest_rotation is not None:
            since = latest_rotation
            since_basis = "latest_universe_rotation"
        else:
            since = datetime.utcnow() - timedelta(minutes=60)
            since_basis = "fallback_last_60_minutes"
        book_rows = _fetch_book_rows(conn, tickers=tickers, since=since)

    observations = [
        obs for row in book_rows
        if (obs := observation_from_book_row(row)) is not None
    ]
    windows = build_windows(
        observations,
        window_size=int(settings.scanner_book_pressure_window or 1),
    )
    quantiles = evenly_spaced_quantiles(grid_points)
    variants = [
        variant_from_settings(settings),
        *derive_quantile_variants(
            windows,
            quantiles=quantiles,
            cooldown_s=float(settings.scanner_book_pressure_cooldown_s or 0.0),
        ),
    ]
    results = evaluate_variants(
        observations,
        windows,
        variants,
        horizons_s=horizons,
        fee_bps_per_side=fee_bps,
        min_net_bps=float(settings.live_alpha_min_net_bps or 0.0),
    )
    payload = {
        "settings": {
            "execution_mode": execution_mode,
            **fee_detail,
            "live_alpha_min_net_bps": settings.live_alpha_min_net_bps,
            "window": settings.scanner_book_pressure_window,
            "cooldown_s": settings.scanner_book_pressure_cooldown_s,
        },
        "since": since.isoformat(),
        "since_basis": since_basis,
        "tickers": tickers,
        "book_rows": len(book_rows),
        "observations": len(observations),
        "windows": len(windows),
        "quantiles": quantiles,
        "variants": [asdict(v) for v in variants],
        "results": [asdict(r) for r in results],
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return
    print("Book-pressure counterfactual sweep")
    print(
        f"since={payload['since']} basis={since_basis} "
        f"tickers={','.join(tickers)}"
    )
    print(
        f"rows={len(book_rows)} observations={len(observations)} "
        f"windows={len(windows)} variants={len(variants)} "
        f"horizons={','.join(str(h) for h in horizons)}"
    )
    print(
        f"execution_mode={execution_mode} fee_bps={fee_bps:.4f} "
        f"fee_source={fee_detail.get('fee_source')}"
    )
    positives = [
        row for row in results
        if row.verdict == "positive_edge_candidate"
    ]
    print(f"positive_edge_candidates={len(positives)}")
    _print_results(results, limit=max(1, int(args.limit or 1)))


if __name__ == "__main__":
    main()
