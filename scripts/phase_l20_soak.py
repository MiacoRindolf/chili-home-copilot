"""Phase L.20 Docker soak - per-ticker regime snapshot (shadow).

Verifies inside the running ``chili`` container that:

  1. Migration 141 applied (``trading_ticker_regime_snapshots`` exists).
  2. ``BRAIN_TICKER_REGIME_MODE`` is visible on ``settings`` (and all
     related tuning thresholds).
  3. ``compute_ticker_regime`` pure classifier returns the expected
     labels for trending / mean-reverting / random-walk synthetic
     series.
  4. ``compute_and_persist_sweep`` writes multiple rows when forced to
     shadow mode with synthetic universe + series overrides.
  5. ``compute_and_persist_sweep`` is a no-op when the current mode is
     ``off`` and hard-refuses ``authoritative``.
  6. Coverage handling: rows with coverage below
     ``brain_ticker_regime_min_coverage_score`` are persisted (for ops
     visibility) but do NOT contribute to the per-label rollup.
  7. Determinism: same ``(as_of_date, ticker)`` yields identical
     ``snapshot_id``; a subsequent sweep with the same date is
     append-only (no silent overwrite or dedupe on the snapshot_id).
  8. ``ticker_regime_summary`` returns the frozen wire shape.
  9. Additive-only: L.17/L.18/L.19 tables are not mutated by any L.20
     write (row counts stable around the sweep).
"""
from __future__ import annotations

import math
import os
import random
import sys
from datetime import date

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.services.trading.ticker_regime_model import (  # noqa: E402
    OHLCVSeries,
    TICKER_REGIME_CHOPPY,
    TICKER_REGIME_MEAN_REVERT,
    TICKER_REGIME_NEUTRAL,
    TICKER_REGIME_TREND_DOWN,
    TICKER_REGIME_TREND_UP,
    TickerRegimeConfig,
    TickerRegimeInput,
    compute_snapshot_id,
    compute_ticker_regime,
)
from app.services.trading.ticker_regime_service import (  # noqa: E402
    compute_and_persist_sweep,
    ticker_regime_summary,
)

SOAK_AS_OF = date(2026, 4, 17)
SOAK_AS_OF_2 = date(2026, 4, 18)
SOAK_TICKERS = ("UPSOAK", "DNSOAK", "MRSOAK", "RWSOAK", "LCSOAK")


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"[phase_l20_soak] FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[phase_l20_soak] OK  : {msg}")


def _cleanup(db) -> None:
    db.execute(text(
        "DELETE FROM trading_ticker_regime_snapshots "
        "WHERE ticker = ANY(:tks) OR as_of_date IN (:a, :b)"
    ), {
        "tks": list(SOAK_TICKERS),
        "a": SOAK_AS_OF,
        "b": SOAK_AS_OF_2,
    })
    db.commit()


# ---------------------------------------------------------------------------
# Synthetic series helpers
# ---------------------------------------------------------------------------


def _noisy_trend(drift: float, n: int = 150, sigma: float = 0.012, seed: int = 42) -> list[float]:
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(n):
        closes.append(closes[-1] * math.exp(drift + rng.gauss(0.0, sigma)))
    return closes


def _mean_revert_series(n: int = 120, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    closes = [100.0]
    step = 0.015
    for i in range(n):
        mult = (1.0 + step) if i % 2 == 0 else (1.0 - step)
        jitter = 1.0 + rng.gauss(0.0, 0.001)
        closes.append(closes[-1] * mult * jitter)
    return closes


def _random_walk(n: int = 250, seed: int = 11, sigma: float = 0.012) -> list[float]:
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(n):
        closes.append(closes[-1] * math.exp(rng.gauss(0.0, sigma)))
    return closes


def _build_ohlcv(
    ticker: str, closes: list[float], asset_class: str = "equity",
) -> OHLCVSeries:
    return OHLCVSeries(
        ticker=ticker,
        asset_class=asset_class,
        closes=tuple(closes),
        highs=tuple(c * 1.01 for c in closes),
        lows=tuple(c * 0.99 for c in closes),
    )


def _series_overrides() -> dict[str, OHLCVSeries]:
    return {
        "UPSOAK": _build_ohlcv(
            "UPSOAK", _noisy_trend(drift=0.015, n=150, sigma=0.012, seed=42),
        ),
        "DNSOAK": _build_ohlcv(
            "DNSOAK", _noisy_trend(drift=-0.015, n=150, sigma=0.012, seed=99),
        ),
        "MRSOAK": _build_ohlcv("MRSOAK", _mean_revert_series(n=120, seed=7)),
        "RWSOAK": _build_ohlcv(
            "RWSOAK", _random_walk(n=250, seed=11, sigma=0.012),
        ),
        # Low-coverage: too-short history → neutral + coverage=0.0
        "LCSOAK": _build_ohlcv(
            "LCSOAK", [100.0 * (1.0 + 0.002 * i) for i in range(20)],
        ),
    }


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_schema_and_settings(db) -> None:
    row = db.execute(text(
        "SELECT to_regclass('public.trading_ticker_regime_snapshots')"
    )).scalar_one()
    _assert(
        row is not None,
        "trading_ticker_regime_snapshots exists",
    )

    row = db.execute(text(
        "SELECT version_id FROM schema_version "
        "WHERE version_id = '141_ticker_regime_snapshot'"
    )).fetchone()
    _assert(
        row is not None,
        "migration 141_ticker_regime_snapshot recorded",
    )

    for attr in (
        "brain_ticker_regime_mode",
        "brain_ticker_regime_ops_log_enabled",
        "brain_ticker_regime_cron_hour",
        "brain_ticker_regime_cron_minute",
        "brain_ticker_regime_min_bars",
        "brain_ticker_regime_min_coverage_score",
        "brain_ticker_regime_ac1_trend",
        "brain_ticker_regime_ac1_mean_revert",
        "brain_ticker_regime_hurst_trend",
        "brain_ticker_regime_hurst_mean_revert",
        "brain_ticker_regime_vr_trend",
        "brain_ticker_regime_vr_mean_revert",
        "brain_ticker_regime_adx_trend",
        "brain_ticker_regime_atr_period",
        "brain_ticker_regime_max_tickers",
        "brain_ticker_regime_lookback_days",
    ):
        _assert(hasattr(settings, attr), f"settings.{attr} exists")


def _check_pure_model() -> None:
    ov = _series_overrides()
    cfg = TickerRegimeConfig()

    up = compute_ticker_regime(TickerRegimeInput(
        as_of_date=SOAK_AS_OF, series=ov["UPSOAK"], config=cfg,
    ))
    _assert(
        up.ticker_regime_label == TICKER_REGIME_TREND_UP,
        f"UPSOAK → trend_up (got {up.ticker_regime_label!r})",
    )
    _assert(up.ticker_regime_numeric == 1, "UPSOAK numeric=1")
    _assert(
        up.snapshot_id == compute_snapshot_id(SOAK_AS_OF, "UPSOAK"),
        "UPSOAK snapshot_id deterministic",
    )

    dn = compute_ticker_regime(TickerRegimeInput(
        as_of_date=SOAK_AS_OF, series=ov["DNSOAK"], config=cfg,
    ))
    _assert(
        dn.ticker_regime_label == TICKER_REGIME_TREND_DOWN,
        f"DNSOAK → trend_down (got {dn.ticker_regime_label!r})",
    )
    _assert(dn.ticker_regime_numeric == -1, "DNSOAK numeric=-1")

    mr = compute_ticker_regime(TickerRegimeInput(
        as_of_date=SOAK_AS_OF, series=ov["MRSOAK"], config=cfg,
    ))
    _assert(
        mr.ticker_regime_label == TICKER_REGIME_MEAN_REVERT,
        f"MRSOAK → mean_revert (got {mr.ticker_regime_label!r})",
    )
    _assert(mr.ac1 is not None and mr.ac1 < -0.05, "MRSOAK ac1 <= -0.05")

    rw = compute_ticker_regime(TickerRegimeInput(
        as_of_date=SOAK_AS_OF, series=ov["RWSOAK"], config=cfg,
    ))
    _assert(
        rw.ticker_regime_label in (TICKER_REGIME_CHOPPY, TICKER_REGIME_NEUTRAL),
        f"RWSOAK → choppy/neutral (got {rw.ticker_regime_label!r})",
    )

    lc = compute_ticker_regime(TickerRegimeInput(
        as_of_date=SOAK_AS_OF, series=ov["LCSOAK"], config=cfg,
    ))
    _assert(
        lc.ticker_regime_label == TICKER_REGIME_NEUTRAL,
        "LCSOAK (20 bars) → neutral",
    )
    _assert(
        lc.coverage_score == 0.0,
        "LCSOAK coverage=0 (below min_bars)",
    )


def _check_persist_shadow_sweep(db) -> None:
    result = compute_and_persist_sweep(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="shadow",
        universe_override=SOAK_TICKERS,
        series_overrides=_series_overrides(),
    )
    _assert(result.mode == "shadow", "sweep mode=shadow")
    _assert(
        result.tickers_attempted == len(SOAK_TICKERS),
        f"tickers_attempted={len(SOAK_TICKERS)}",
    )
    _assert(
        result.tickers_persisted == len(SOAK_TICKERS),
        f"tickers_persisted={len(SOAK_TICKERS)}",
    )
    _assert(
        result.tickers_skipped == 0,
        "no tickers skipped (all have valid series)",
    )
    # by_label excludes LCSOAK (coverage=0 < min), so 4 of 5 count.
    labels_sum = sum(result.by_label.values())
    _assert(
        labels_sum == 4,
        f"by_label counts only full-coverage rows (sum={labels_sum})",
    )
    _assert(
        result.by_label[TICKER_REGIME_TREND_UP] == 1,
        "by_label.trend_up=1",
    )
    _assert(
        result.by_label[TICKER_REGIME_TREND_DOWN] == 1,
        "by_label.trend_down=1",
    )
    _assert(
        result.by_label[TICKER_REGIME_MEAN_REVERT] == 1,
        "by_label.mean_revert=1",
    )

    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_ticker_regime_snapshots "
        "WHERE ticker = ANY(:tks) AND as_of_date = :d"
    ), {"tks": list(SOAK_TICKERS), "d": SOAK_AS_OF}).scalar_one()
    _assert(
        int(count) == len(SOAK_TICKERS),
        f"{len(SOAK_TICKERS)} rows persisted for soak as_of",
    )

    # Verify low-coverage LCSOAK was still persisted.
    lc_row = db.execute(text(
        "SELECT ticker_regime_label, coverage_score "
        "FROM trading_ticker_regime_snapshots "
        "WHERE ticker='LCSOAK' AND as_of_date=:d"
    ), {"d": SOAK_AS_OF}).fetchone()
    _assert(lc_row is not None, "LCSOAK row present")
    _assert(
        str(lc_row[0]) == TICKER_REGIME_NEUTRAL and float(lc_row[1]) == 0.0,
        "LCSOAK persisted as neutral/coverage=0",
    )


def _check_off_and_authoritative(db) -> None:
    result = compute_and_persist_sweep(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="off",
        universe_override=SOAK_TICKERS,
        series_overrides=_series_overrides(),
    )
    _assert(result.mode == "off", "off mode result.mode=off")
    _assert(result.tickers_attempted == 0, "off mode is a no-op (0 attempted)")
    _assert(result.tickers_persisted == 0, "off mode wrote 0 rows")

    raised = False
    try:
        compute_and_persist_sweep(
            db,
            as_of_date=SOAK_AS_OF,
            mode_override="authoritative",
            universe_override=SOAK_TICKERS,
            series_overrides=_series_overrides(),
        )
    except RuntimeError:
        raised = True
    _assert(raised, "authoritative mode raises RuntimeError (refused)")


def _check_determinism_and_append(db) -> None:
    # Second sweep with same as_of + same tickers -> same snapshot_ids,
    # append-only (rows double).
    before = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_ticker_regime_snapshots "
        "WHERE ticker = ANY(:tks) AND as_of_date = :d"
    ), {"tks": list(SOAK_TICKERS), "d": SOAK_AS_OF}).scalar_one() or 0)

    result = compute_and_persist_sweep(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="shadow",
        universe_override=SOAK_TICKERS,
        series_overrides=_series_overrides(),
    )
    _assert(result.tickers_persisted == len(SOAK_TICKERS), "second sweep writes rows")

    after = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_ticker_regime_snapshots "
        "WHERE ticker = ANY(:tks) AND as_of_date = :d"
    ), {"tks": list(SOAK_TICKERS), "d": SOAK_AS_OF}).scalar_one() or 0)
    _assert(
        after == before + len(SOAK_TICKERS),
        f"append-only: {before}+{len(SOAK_TICKERS)}={after}",
    )

    # Snapshot_id must match compute_snapshot_id(as_of, ticker) for each
    # ticker, and must be stable across duplicate rows.
    for t in SOAK_TICKERS:
        expected = compute_snapshot_id(SOAK_AS_OF, t)
        rows = db.execute(text(
            "SELECT snapshot_id FROM trading_ticker_regime_snapshots "
            "WHERE ticker=:t AND as_of_date=:d"
        ), {"t": t, "d": SOAK_AS_OF}).fetchall()
        ids = {str(r[0]) for r in rows}
        _assert(
            ids == {expected},
            f"snapshot_id stable for {t} across duplicate rows",
        )


def _check_summary(db) -> None:
    payload = ticker_regime_summary(
        db, lookback_days=7, latest_tickers_limit=20,
    )
    expected_keys = {
        "mode",
        "lookback_days",
        "snapshots_total",
        "distinct_tickers",
        "by_ticker_regime_label",
        "by_asset_class",
        "mean_coverage_score",
        "mean_trend_score",
        "mean_mean_revert_score",
        "latest_tickers",
    }
    _assert(
        set(payload.keys()) == expected_keys,
        "ticker_regime_summary frozen top-level shape",
    )
    _assert(
        set(payload["by_ticker_regime_label"].keys()) == {
            TICKER_REGIME_TREND_UP, TICKER_REGIME_TREND_DOWN,
            TICKER_REGIME_MEAN_REVERT, TICKER_REGIME_CHOPPY,
            TICKER_REGIME_NEUTRAL,
        },
        "by_ticker_regime_label has all 5 composite labels",
    )
    _assert(payload["lookback_days"] == 7, "lookback_days echoes arg")
    _assert(
        isinstance(payload["latest_tickers"], list),
        "latest_tickers is a list",
    )
    _assert(
        isinstance(payload["by_asset_class"], dict),
        "by_asset_class is a dict",
    )


def _check_l17_l18_l19_additive(db) -> None:
    mr_pre = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_macro_regime_snapshots"
    )).scalar_one() or 0)
    br_pre = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_breadth_relstr_snapshots"
    )).scalar_one() or 0)
    ca_pre = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_cross_asset_snapshots"
    )).scalar_one() or 0)

    compute_and_persist_sweep(
        db,
        as_of_date=SOAK_AS_OF_2,
        mode_override="shadow",
        universe_override=SOAK_TICKERS,
        series_overrides=_series_overrides(),
    )

    mr_post = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_macro_regime_snapshots"
    )).scalar_one() or 0)
    br_post = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_breadth_relstr_snapshots"
    )).scalar_one() or 0)
    ca_post = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_cross_asset_snapshots"
    )).scalar_one() or 0)
    _assert(
        mr_post == mr_pre,
        f"L.17 macro snapshots unchanged by L.20 writes "
        f"(pre={mr_pre} post={mr_post})",
    )
    _assert(
        br_post == br_pre,
        f"L.18 breadth_relstr snapshots unchanged by L.20 writes "
        f"(pre={br_pre} post={br_post})",
    )
    _assert(
        ca_post == ca_pre,
        f"L.19 cross_asset snapshots unchanged by L.20 writes "
        f"(pre={ca_pre} post={ca_post})",
    )


def main() -> int:
    print("[phase_l20_soak] starting")
    db = SessionLocal()
    try:
        _cleanup(db)
        _check_schema_and_settings(db)
        _check_pure_model()
        _check_persist_shadow_sweep(db)
        _check_off_and_authoritative(db)
        _check_determinism_and_append(db)
        _check_summary(db)
        _check_l17_l18_l19_additive(db)
        _cleanup(db)
        print("[phase_l20_soak] ALL GREEN")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
