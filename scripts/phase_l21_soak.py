"""Phase L.21 Docker soak - vol term structure + cross-sectional dispersion.

Verifies inside the running ``chili`` container that:

  1. Migration 142 applied (``trading_vol_dispersion_snapshots`` exists).
  2. ``BRAIN_VOL_DISPERSION_MODE`` is visible on ``settings`` (and all
     related tuning thresholds).
  3. ``compute_vol_dispersion`` pure classifier returns the expected
     composite labels for synthetic compressed / spike / dispersion
     scenarios.
  4. ``compute_and_persist`` writes one row when forced to shadow mode
     with synthetic term / sector / universe overrides and a healthy
     coverage score.
  5. ``compute_and_persist`` is a no-op when the current mode is
     ``off`` and hard-refuses ``authoritative``.
  6. Coverage handling: rows with coverage below
     ``brain_vol_dispersion_min_coverage_score`` are still persisted
     (for ops visibility) but ``compute_and_persist`` returns ``None``.
  7. Determinism: same ``as_of_date`` yields identical ``snapshot_id``;
     a second shadow call with the same date is append-only.
  8. ``vol_dispersion_summary`` returns the frozen wire shape.
  9. Additive-only: L.17 / L.18 / L.19 / L.20 tables are not mutated by
     any L.21 write (row counts stable around the sweep).
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
from app.services.trading.volatility_dispersion_model import (  # noqa: E402
    CORRELATION_LOW,
    CORRELATION_NORMAL,
    CORRELATION_SPIKE,
    DISPERSION_HIGH,
    DISPERSION_LOW,
    DISPERSION_NORMAL,
    TermLeg,
    UniverseTicker,
    VOL_REGIME_COMPRESSED,
    VOL_REGIME_EXPANDED,
    VOL_REGIME_NORMAL,
    VOL_REGIME_SPIKE,
    VolatilityDispersionConfig,
    VolatilityDispersionInput,
    compute_snapshot_id,
    compute_vol_dispersion,
)
from app.services.trading.vol_dispersion_service import (  # noqa: E402
    SECTOR_SYMBOLS,
    compute_and_persist,
    vol_dispersion_summary,
)

SOAK_AS_OF = date(2026, 4, 17)
SOAK_AS_OF_2 = date(2026, 4, 18)
SOAK_AS_OF_LOW = date(2026, 4, 19)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"[phase_l21_soak] FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[phase_l21_soak] OK  : {msg}")


def _cleanup(db) -> None:
    db.execute(text(
        "DELETE FROM trading_vol_dispersion_snapshots "
        "WHERE as_of_date IN (:a, :b, :c)"
    ), {
        "a": SOAK_AS_OF,
        "b": SOAK_AS_OF_2,
        "c": SOAK_AS_OF_LOW,
    })
    db.commit()


# ---------------------------------------------------------------------------
# Synthetic series helpers
# ---------------------------------------------------------------------------


def _gbm(n: int, drift: float, sigma: float, seed: int, start: float = 100.0) -> list[float]:
    rng = random.Random(seed)
    closes = [start]
    for _ in range(n):
        closes.append(closes[-1] * math.exp(drift + rng.gauss(0.0, sigma)))
    return closes


def _constant(n: int, value: float) -> list[float]:
    return [value] * n


def _leg(symbol: str, closes: list[float]) -> TermLeg:
    return TermLeg(
        symbol=symbol,
        closes=tuple(closes),
        highs=tuple(c * 1.01 for c in closes),
        lows=tuple(c * 0.99 for c in closes),
    )


def _term_legs_compressed() -> dict[str, TermLeg]:
    """VIXY low (12), VIXM higher (16), VXZ higher (18) -> contango;
    SPY low realised vol."""
    n = 200
    return {
        "vixy": _leg("VIXY", _constant(n, 12.0)),
        "vixm": _leg("VIXM", _constant(n, 16.0)),
        "vxz": _leg("VXZ", _constant(n, 18.0)),
        "spy": _leg("SPY", _gbm(n, drift=0.0003, sigma=0.005, seed=42)),
    }


def _term_legs_spike() -> dict[str, TermLeg]:
    """VIXY spiking (35), VIXM lower (28), VXZ lower (24) -> backwardation."""
    n = 200
    return {
        "vixy": _leg("VIXY", _constant(n, 35.0)),
        "vixm": _leg("VIXM", _constant(n, 28.0)),
        "vxz": _leg("VXZ", _constant(n, 24.0)),
        "spy": _leg("SPY", _gbm(n, drift=-0.001, sigma=0.025, seed=99)),
    }


def _sector_legs_static() -> dict[str, TermLeg]:
    out: dict[str, TermLeg] = {}
    for i, sym in enumerate(SECTOR_SYMBOLS):
        out[sym] = _leg(
            sym,
            _gbm(200, drift=0.0002 + 0.00005 * i, sigma=0.012, seed=100 + i),
        )
    return out


def _universe_low_dispersion(n_tickers: int = 20) -> list[UniverseTicker]:
    """All move together: same drift, same seed offset pattern -> low
    cross-sectional std + high correlation."""
    out: list[UniverseTicker] = []
    # Shared driver, slight noise added per-ticker to avoid zero variance
    shared = _gbm(200, drift=0.0005, sigma=0.012, seed=777)
    for i in range(n_tickers):
        rng = random.Random(1000 + i)
        closes = []
        for c in shared:
            closes.append(c * (1.0 + rng.gauss(0.0, 0.0005)))
        out.append(UniverseTicker(symbol=f"LOW{i:02d}", closes=tuple(closes)))
    return out


def _universe_high_dispersion(n_tickers: int = 20) -> list[UniverseTicker]:
    """Each ticker gets its own drift + sigma -> high cross-sectional
    std and low pairwise correlation."""
    out: list[UniverseTicker] = []
    for i in range(n_tickers):
        drift = 0.001 * ((i % 5) - 2)
        sigma = 0.015 + 0.005 * (i % 4)
        closes = _gbm(200, drift=drift, sigma=sigma, seed=2000 + i)
        out.append(UniverseTicker(symbol=f"HI{i:02d}", closes=tuple(closes)))
    return out


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_schema_and_settings(db) -> None:
    row = db.execute(text(
        "SELECT to_regclass('public.trading_vol_dispersion_snapshots')"
    )).scalar_one()
    _assert(
        row is not None,
        "trading_vol_dispersion_snapshots exists",
    )

    row = db.execute(text(
        "SELECT version_id FROM schema_version "
        "WHERE version_id = '142_vol_dispersion_snapshot'"
    )).fetchone()
    _assert(
        row is not None,
        "migration 142_vol_dispersion_snapshot recorded",
    )

    for attr in (
        "brain_vol_dispersion_mode",
        "brain_vol_dispersion_ops_log_enabled",
        "brain_vol_dispersion_cron_hour",
        "brain_vol_dispersion_cron_minute",
        "brain_vol_dispersion_min_bars",
        "brain_vol_dispersion_min_coverage_score",
        "brain_vol_dispersion_universe_cap",
        "brain_vol_dispersion_corr_sample_size",
        "brain_vol_dispersion_vixy_low",
        "brain_vol_dispersion_vixy_high",
        "brain_vol_dispersion_vixy_spike",
        "brain_vol_dispersion_realized_vol_low",
        "brain_vol_dispersion_realized_vol_high",
        "brain_vol_dispersion_cs_std_low",
        "brain_vol_dispersion_cs_std_high",
        "brain_vol_dispersion_corr_low",
        "brain_vol_dispersion_corr_high",
        "brain_vol_dispersion_lookback_days",
    ):
        _assert(hasattr(settings, attr), f"settings.{attr} exists")


def _check_pure_model() -> None:
    cfg = VolatilityDispersionConfig()

    # Compressed scenario: VIXY 12 (<low=14), contango slope +4 positive,
    # SPY low realised vol -> vol_compressed.
    out_compressed = compute_vol_dispersion(VolatilityDispersionInput(
        as_of_date=SOAK_AS_OF,
        term_legs=_term_legs_compressed(),
        sector_legs=_sector_legs_static(),
        universe_tickers=_universe_low_dispersion(20),
        config=cfg,
    ))
    _assert(
        out_compressed.snapshot_id == compute_snapshot_id(SOAK_AS_OF),
        "snapshot_id deterministic for SOAK_AS_OF",
    )
    _assert(
        out_compressed.vol_regime_label == VOL_REGIME_COMPRESSED,
        f"compressed scenario -> vol_compressed "
        f"(got {out_compressed.vol_regime_label!r})",
    )
    _assert(
        out_compressed.vol_regime_numeric == -1,
        "compressed numeric=-1",
    )

    # Spike scenario: VIXY 35 (>=spike=30), slope negative (backwardation)
    # -> vol_spike.
    out_spike = compute_vol_dispersion(VolatilityDispersionInput(
        as_of_date=SOAK_AS_OF,
        term_legs=_term_legs_spike(),
        sector_legs=_sector_legs_static(),
        universe_tickers=_universe_high_dispersion(20),
        config=cfg,
    ))
    _assert(
        out_spike.vol_regime_label == VOL_REGIME_SPIKE,
        f"spike scenario -> vol_spike (got {out_spike.vol_regime_label!r})",
    )
    _assert(
        out_spike.vol_regime_numeric == 2,
        "spike numeric=2",
    )

    # Dispersion label families must be in the valid set.
    _assert(
        out_compressed.dispersion_label in {
            DISPERSION_LOW, DISPERSION_NORMAL, DISPERSION_HIGH,
        },
        f"compressed dispersion label valid "
        f"({out_compressed.dispersion_label!r})",
    )
    _assert(
        out_spike.correlation_label in {
            CORRELATION_LOW, CORRELATION_NORMAL, CORRELATION_SPIKE,
        },
        f"spike correlation label valid "
        f"({out_spike.correlation_label!r})",
    )


def _check_persist_shadow(db) -> None:
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="shadow",
        term_overrides=_term_legs_compressed(),
        sector_overrides=_sector_legs_static(),
        universe_override=_universe_low_dispersion(20),
    )
    _assert(row is not None, "shadow compute_and_persist returns row")
    _assert(row.mode == "shadow", "row.mode == shadow")
    _assert(
        row.snapshot_id == compute_snapshot_id(SOAK_AS_OF),
        "row.snapshot_id matches pure compute_snapshot_id",
    )
    _assert(
        row.vol_regime_label == VOL_REGIME_COMPRESSED,
        f"row vol label == vol_compressed (got {row.vol_regime_label!r})",
    )
    _assert(
        row.coverage_score >= 0.5,
        f"coverage_score >= 0.5 (got {row.coverage_score})",
    )

    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_vol_dispersion_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF}).scalar_one()
    _assert(int(count) == 1, "exactly 1 row persisted for SOAK_AS_OF")


def _check_off_and_authoritative(db) -> None:
    result_off = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="off",
        term_overrides=_term_legs_compressed(),
        sector_overrides=_sector_legs_static(),
        universe_override=_universe_low_dispersion(20),
    )
    _assert(result_off is None, "off mode is a no-op (returns None)")

    count_off = db.execute(text(
        "SELECT COUNT(*) FROM trading_vol_dispersion_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF}).scalar_one()
    _assert(int(count_off) == 1, "off mode did not append any row")

    raised = False
    try:
        compute_and_persist(
            db,
            as_of_date=SOAK_AS_OF,
            mode_override="authoritative",
            term_overrides=_term_legs_compressed(),
            sector_overrides=_sector_legs_static(),
            universe_override=_universe_low_dispersion(20),
        )
    except RuntimeError:
        raised = True
    _assert(raised, "authoritative mode raises RuntimeError (refused)")


def _check_determinism_and_append(db) -> None:
    before = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_vol_dispersion_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF}).scalar_one() or 0)

    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="shadow",
        term_overrides=_term_legs_compressed(),
        sector_overrides=_sector_legs_static(),
        universe_override=_universe_low_dispersion(20),
    )
    _assert(row is not None, "second shadow call writes a row")
    _assert(
        row.snapshot_id == compute_snapshot_id(SOAK_AS_OF),
        "snapshot_id deterministic across calls",
    )

    after = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_vol_dispersion_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF}).scalar_one() or 0)
    _assert(
        after == before + 1,
        f"append-only: {before}+1={after}",
    )

    # snapshot_id stable across the duplicated rows.
    rows = db.execute(text(
        "SELECT DISTINCT snapshot_id FROM trading_vol_dispersion_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF}).fetchall()
    ids = {str(r[0]) for r in rows}
    _assert(
        ids == {compute_snapshot_id(SOAK_AS_OF)},
        "snapshot_id stable across duplicate rows",
    )


def _check_low_coverage(db) -> None:
    """Insufficient bars -> coverage_score < min_coverage_score -> row
    persisted for post-mortem but compute_and_persist returns None."""
    # Short series (below min_bars=60) in term + sector + universe.
    short_n = 10
    short_term: dict[str, TermLeg] = {
        "vixy": _leg("VIXY", _constant(short_n, 12.0)),
        "vixm": _leg("VIXM", _constant(short_n, 16.0)),
        "vxz": _leg("VXZ", _constant(short_n, 18.0)),
        "spy": _leg("SPY", _constant(short_n, 400.0)),
    }
    short_sectors: dict[str, TermLeg] = {
        sym: _leg(sym, _constant(short_n, 100.0)) for sym in SECTOR_SYMBOLS
    }
    short_universe = [
        UniverseTicker(
            symbol=f"SHORT{i:02d}",
            closes=tuple(_constant(short_n, 50.0)),
        )
        for i in range(10)
    ]

    before = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_vol_dispersion_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF_LOW}).scalar_one() or 0)

    result = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF_LOW,
        mode_override="shadow",
        term_overrides=short_term,
        sector_overrides=short_sectors,
        universe_override=short_universe,
    )
    _assert(
        result is None,
        "low-coverage shadow call returns None",
    )

    after = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_vol_dispersion_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF_LOW}).scalar_one() or 0)
    _assert(
        after == before + 1,
        f"low-coverage row still persisted for post-mortem "
        f"(before={before} after={after})",
    )

    cov_row = db.execute(text(
        "SELECT coverage_score FROM trading_vol_dispersion_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF_LOW}).fetchone()
    _assert(
        cov_row is not None and float(cov_row[0]) < 0.5,
        "low-coverage row has coverage_score < 0.5",
    )


def _check_summary(db) -> None:
    payload = vol_dispersion_summary(db, lookback_days=14)
    expected_keys = {
        "mode",
        "lookback_days",
        "snapshots_total",
        "by_vol_regime_label",
        "by_dispersion_label",
        "by_correlation_label",
        "mean_vixy_close",
        "mean_vix_slope_4m_1m",
        "mean_cross_section_return_std_20d",
        "mean_abs_corr_20d",
        "mean_sector_leadership_churn_20d",
        "mean_coverage_score",
        "latest_snapshot",
    }
    _assert(
        set(payload.keys()) == expected_keys,
        "vol_dispersion_summary frozen top-level shape",
    )
    _assert(
        set(payload["by_vol_regime_label"].keys()) == {
            VOL_REGIME_COMPRESSED, VOL_REGIME_NORMAL,
            VOL_REGIME_EXPANDED, VOL_REGIME_SPIKE,
        },
        "by_vol_regime_label has all 4 labels",
    )
    _assert(
        set(payload["by_dispersion_label"].keys()) == {
            DISPERSION_LOW, DISPERSION_NORMAL, DISPERSION_HIGH,
        },
        "by_dispersion_label has all 3 labels",
    )
    _assert(
        set(payload["by_correlation_label"].keys()) == {
            CORRELATION_LOW, CORRELATION_NORMAL, CORRELATION_SPIKE,
        },
        "by_correlation_label has all 3 labels",
    )
    _assert(payload["lookback_days"] == 14, "lookback_days echoes arg")


def _check_additive(db) -> None:
    mr_pre = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_macro_regime_snapshots"
    )).scalar_one() or 0)
    br_pre = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_breadth_relstr_snapshots"
    )).scalar_one() or 0)
    ca_pre = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_cross_asset_snapshots"
    )).scalar_one() or 0)
    tr_pre = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_ticker_regime_snapshots"
    )).scalar_one() or 0)

    compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF_2,
        mode_override="shadow",
        term_overrides=_term_legs_spike(),
        sector_overrides=_sector_legs_static(),
        universe_override=_universe_high_dispersion(20),
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
    tr_post = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_ticker_regime_snapshots"
    )).scalar_one() or 0)
    _assert(
        mr_post == mr_pre,
        f"L.17 macro snapshots unchanged by L.21 writes "
        f"(pre={mr_pre} post={mr_post})",
    )
    _assert(
        br_post == br_pre,
        f"L.18 breadth_relstr snapshots unchanged by L.21 writes "
        f"(pre={br_pre} post={br_post})",
    )
    _assert(
        ca_post == ca_pre,
        f"L.19 cross_asset snapshots unchanged by L.21 writes "
        f"(pre={ca_pre} post={ca_post})",
    )
    _assert(
        tr_post == tr_pre,
        f"L.20 ticker_regime snapshots unchanged by L.21 writes "
        f"(pre={tr_pre} post={tr_post})",
    )


def main() -> int:
    print("[phase_l21_soak] starting")
    db = SessionLocal()
    try:
        _cleanup(db)
        _check_schema_and_settings(db)
        _check_pure_model()
        _check_persist_shadow(db)
        _check_off_and_authoritative(db)
        _check_determinism_and_append(db)
        _check_low_coverage(db)
        _check_summary(db)
        _check_additive(db)
        _cleanup(db)
        print("[phase_l21_soak] ALL GREEN")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
