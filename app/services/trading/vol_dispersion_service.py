"""Phase L.21 - persistence layer for the volatility / dispersion snapshot.

Runs the pure :mod:`volatility_dispersion_model` against:

* 4 term legs (``VIXY``, ``VIXM``, ``VXZ``, ``SPY``) fetched via
  :func:`app.services.trading.market_data.fetch_ohlcv_df`;
* 11 sector SPDRs (same symbols L.18 uses);
* a capped slice of the snapshot universe via
  :func:`app.services.trading.scanner.build_snapshot_ticker_universe`.

Design
------
* **Two public entry-points.** :func:`compute_and_persist` writes at
  most one row per ``as_of_date`` and emits exactly one ops line;
  :func:`vol_dispersion_summary` returns the diagnostics dict for the
  FastAPI route.
* **Refuses authoritative.** Until Phase L.21.2 opens explicitly the
  service raises :class:`RuntimeError` on
  ``mode_override="authoritative"`` or
  ``brain_vol_dispersion_mode="authoritative"``. A refusal ops line
  is emitted before the raise so ops / release blockers can see the
  attempt.
* **Append-only.** Every call appends a row. The deterministic
  ``snapshot_id`` (keyed on ``as_of_date``) lets callers dedupe.
* **Off-mode short-circuit.** When
  ``brain_vol_dispersion_mode == "off"`` :func:`compute_and_persist`
  emits a single skip line and returns ``None``.
* **Additive-only.** No downstream consumer (scanner, promotion,
  sizing, playbook, alerts) reads from this table in L.21.1.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.vol_dispersion_ops_log import (
    format_vol_dispersion_ops_line,
)
from .volatility_dispersion_model import (
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
    VolatilityDispersionOutput,
    compute_snapshot_id,
    compute_vol_dispersion,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")

# Canonical term legs (VIX term structure + SPY for realised-vol gap)
TERM_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("vixy", "VIXY"),
    ("vixm", "VIXM"),
    ("vxz", "VXZ"),
    ("spy", "SPY"),
)

# Canonical 11 sector SPDRs (same set used by L.18)
SECTOR_SYMBOLS: tuple[str, ...] = (
    "XLK",
    "XLF",
    "XLE",
    "XLV",
    "XLY",
    "XLP",
    "XLI",
    "XLU",
    "XLRE",
    "XLB",
    "XLC",
)


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------


def _effective_mode(override: str | None = None) -> str:
    m = (
        override
        or getattr(settings, "brain_vol_dispersion_mode", "off")
        or "off"
    ).lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(
        getattr(settings, "brain_vol_dispersion_ops_log_enabled", True)
    )


def _config_from_settings() -> VolatilityDispersionConfig:
    return VolatilityDispersionConfig(
        min_bars=int(getattr(settings, "brain_vol_dispersion_min_bars", 60)),
        min_coverage_score=float(getattr(
            settings, "brain_vol_dispersion_min_coverage_score", 0.5,
        )),
        universe_cap=int(getattr(
            settings, "brain_vol_dispersion_universe_cap", 60,
        )),
        corr_sample_size=int(getattr(
            settings, "brain_vol_dispersion_corr_sample_size", 30,
        )),
        vixy_low=float(getattr(
            settings, "brain_vol_dispersion_vixy_low", 14.0,
        )),
        vixy_high=float(getattr(
            settings, "brain_vol_dispersion_vixy_high", 22.0,
        )),
        vixy_spike=float(getattr(
            settings, "brain_vol_dispersion_vixy_spike", 30.0,
        )),
        realized_vol_low=float(getattr(
            settings, "brain_vol_dispersion_realized_vol_low", 0.12,
        )),
        realized_vol_high=float(getattr(
            settings, "brain_vol_dispersion_realized_vol_high", 0.30,
        )),
        cs_std_low=float(getattr(
            settings, "brain_vol_dispersion_cs_std_low", 0.012,
        )),
        cs_std_high=float(getattr(
            settings, "brain_vol_dispersion_cs_std_high", 0.025,
        )),
        corr_low=float(getattr(
            settings, "brain_vol_dispersion_corr_low", 0.35,
        )),
        corr_high=float(getattr(
            settings, "brain_vol_dispersion_corr_high", 0.65,
        )),
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolDispersionRow:
    """Thin reference to a persisted row."""

    pk_id: int
    snapshot_id: str
    as_of_date: date
    vol_regime_label: str
    dispersion_label: str
    correlation_label: str
    coverage_score: float
    mode: str


# ---------------------------------------------------------------------------
# OHLCV assembly
# ---------------------------------------------------------------------------


def _today_utc_date() -> date:
    return datetime.now(tz=timezone.utc).date()


def _fetch_leg(symbol: str) -> TermLeg | None:
    """Fetch daily closes for ``symbol`` and return a :class:`TermLeg`."""
    try:
        from .market_data import fetch_ohlcv_df  # noqa: WPS433

        df = fetch_ohlcv_df(symbol, interval="1d", period="9mo")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[vol_dispersion] fetch_ohlcv_df(%s) raised: %s", symbol, exc,
        )
        return None
    if df is None or getattr(df, "empty", True):
        return None
    cols = set(getattr(df, "columns", []))
    need = {"Open", "High", "Low", "Close"}
    if not need.issubset(cols):
        return None
    try:
        closes_s = df["Close"].dropna()
        highs_s = df["High"].reindex(closes_s.index).dropna()
        lows_s = df["Low"].reindex(closes_s.index).dropna()
        common = closes_s.index.intersection(highs_s.index).intersection(
            lows_s.index
        )
        if len(common) < 1:
            return None
        common = common.sort_values()
        if len(common) > 260:
            common = common[-260:]
        closes = tuple(float(x) for x in closes_s.loc[common].tolist())
        highs = tuple(float(x) for x in highs_s.loc[common].tolist())
        lows = tuple(float(x) for x in lows_s.loc[common].tolist())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[vol_dispersion] parse_ohlcv(%s) raised: %s", symbol, exc,
        )
        return None
    return TermLeg(symbol=symbol, closes=closes, highs=highs, lows=lows)


def _fetch_universe_ticker(symbol: str) -> UniverseTicker | None:
    try:
        from .market_data import fetch_ohlcv_df  # noqa: WPS433

        df = fetch_ohlcv_df(symbol, interval="1d", period="9mo")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[vol_dispersion] fetch_ohlcv_df(%s) raised: %s", symbol, exc,
        )
        return None
    if df is None or getattr(df, "empty", True):
        return None
    try:
        closes_s = df["Close"].dropna().sort_index()
        if len(closes_s) > 260:
            closes_s = closes_s.iloc[-260:]
        closes = tuple(float(x) for x in closes_s.tolist())
    except Exception:  # pragma: no cover - defensive
        return None
    if not closes:
        return None
    return UniverseTicker(symbol=symbol, closes=closes)


def _load_universe(
    db: Session, *, cap: int, universe_override: Sequence[str] | None,
) -> list[str]:
    if universe_override is not None:
        return [str(t).strip().upper() for t in universe_override if t]
    try:
        from .scanner import build_snapshot_ticker_universe  # noqa: WPS433

        tickers, _meta = build_snapshot_ticker_universe(
            db, user_id=None, limit=max(cap * 3, cap + 10),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[vol_dispersion] universe build failed: %s", exc)
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in tickers:
        if not t:
            continue
        key = str(t).strip().upper()
        if key in seen:
            continue
        # Skip crypto - dispersion is an equities-only primitive here.
        if key.endswith("-USD"):
            continue
        if (
            key.endswith("USD")
            and 5 <= len(key) <= 18
            and key[:-3].isalnum()
        ):
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# Persist one row
# ---------------------------------------------------------------------------


def _insert_row(
    db: Session, *, out: VolatilityDispersionOutput, mode: str,
) -> int:
    payload_json = json.dumps(out.payload, default=str)
    now = datetime.utcnow()
    row = db.execute(text("""
        INSERT INTO trading_vol_dispersion_snapshots (
            snapshot_id, as_of_date,
            vixy_close, vixm_close, vxz_close,
            vix_slope_4m_1m, vix_slope_7m_1m,
            spy_realized_vol_5d, spy_realized_vol_20d, spy_realized_vol_60d,
            vix_realized_gap,
            cross_section_return_std_5d, cross_section_return_std_20d,
            mean_abs_corr_20d, corr_sample_size,
            sector_leadership_churn_20d,
            vol_regime_numeric, vol_regime_label,
            dispersion_numeric, dispersion_label,
            correlation_numeric, correlation_label,
            universe_size, tickers_missing, coverage_score,
            payload_json, mode, computed_at, observed_at
        ) VALUES (
            :snapshot_id, :as_of_date,
            :vixy_close, :vixm_close, :vxz_close,
            :slope_4m_1m, :slope_7m_1m,
            :rv5, :rv20, :rv60,
            :gap,
            :cs5, :cs20,
            :corr20, :corr_n,
            :churn,
            :vol_num, :vol_label,
            :disp_num, :disp_label,
            :corr_num, :corr_label,
            :univ_size, :miss, :coverage,
            CAST(:payload_json AS JSONB), :mode, :computed_at, :observed_at
        ) RETURNING id
    """), {
        "snapshot_id": out.snapshot_id,
        "as_of_date": out.as_of_date,
        "vixy_close": out.vixy_close,
        "vixm_close": out.vixm_close,
        "vxz_close": out.vxz_close,
        "slope_4m_1m": out.vix_slope_4m_1m,
        "slope_7m_1m": out.vix_slope_7m_1m,
        "rv5": out.spy_realized_vol_5d,
        "rv20": out.spy_realized_vol_20d,
        "rv60": out.spy_realized_vol_60d,
        "gap": out.vix_realized_gap,
        "cs5": out.cross_section_return_std_5d,
        "cs20": out.cross_section_return_std_20d,
        "corr20": out.mean_abs_corr_20d,
        "corr_n": int(out.corr_sample_size),
        "churn": out.sector_leadership_churn_20d,
        "vol_num": int(out.vol_regime_numeric),
        "vol_label": out.vol_regime_label,
        "disp_num": int(out.dispersion_numeric),
        "disp_label": out.dispersion_label,
        "corr_num": int(out.correlation_numeric),
        "corr_label": out.correlation_label,
        "univ_size": int(out.universe_size),
        "miss": int(out.tickers_missing),
        "coverage": float(out.coverage_score),
        "payload_json": payload_json,
        "mode": mode,
        "computed_at": now,
        "observed_at": now,
    }).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def compute_and_persist(
    db: Session,
    *,
    as_of_date: date | None = None,
    mode_override: str | None = None,
    term_overrides: Mapping[str, TermLeg] | None = None,
    sector_overrides: Mapping[str, TermLeg] | None = None,
    universe_override: Sequence[UniverseTicker] | None = None,
) -> VolDispersionRow | None:
    """Run one daily snapshot computation and persist it.

    ``term_overrides`` / ``sector_overrides`` / ``universe_override``
    are used by the Docker soak to feed deterministic synthetic
    inputs. In production all three are ``None`` and the service pulls
    live OHLCV.
    """
    mode = _effective_mode(mode_override)
    as_of = as_of_date or _today_utc_date()

    if mode == "off":
        if _ops_log_enabled():
            logger.info(format_vol_dispersion_ops_line(
                event="vol_dispersion_skipped",
                mode=mode,
                as_of_date=as_of.isoformat(),
                reason="mode_off",
            ))
        return None

    if mode == "authoritative":
        if _ops_log_enabled():
            logger.warning(format_vol_dispersion_ops_line(
                event="vol_dispersion_refused_authoritative",
                mode=mode,
                as_of_date=as_of.isoformat(),
                reason="L.21.1_shadow_only",
            ))
        raise RuntimeError(
            "vol_dispersion authoritative mode is not permitted until "
            "Phase L.21.2 is explicitly opened"
        )

    cfg = _config_from_settings()

    # --- Term legs ---------------------------------------------------------
    term_legs: dict[str, TermLeg] = {}
    if term_overrides is not None:
        term_legs.update({k.lower(): v for k, v in term_overrides.items()})
    else:
        for key, sym in TERM_SYMBOLS:
            leg = _fetch_leg(sym)
            if leg is not None:
                term_legs[key] = leg

    # --- Sector legs -------------------------------------------------------
    sector_legs: dict[str, TermLeg] = {}
    if sector_overrides is not None:
        sector_legs.update({str(k).upper(): v for k, v in sector_overrides.items()})
    else:
        for sym in SECTOR_SYMBOLS:
            leg = _fetch_leg(sym)
            if leg is not None:
                sector_legs[sym] = leg

    # --- Universe ----------------------------------------------------------
    universe_list: list[UniverseTicker] = []
    if universe_override is not None:
        universe_list = list(universe_override)
    else:
        syms = _load_universe(
            db, cap=cfg.universe_cap, universe_override=None,
        )
        for sym in syms:
            u = _fetch_universe_ticker(sym)
            if u is not None:
                universe_list.append(u)

    # --- Pure model --------------------------------------------------------
    inp = VolatilityDispersionInput(
        as_of_date=as_of,
        term_legs=term_legs,
        sector_legs=sector_legs,
        universe_tickers=universe_list,
        config=cfg,
    )
    out = compute_vol_dispersion(inp)

    if _ops_log_enabled():
        logger.info(format_vol_dispersion_ops_line(
            event="vol_dispersion_computed",
            mode=mode,
            snapshot_id=out.snapshot_id,
            as_of_date=as_of.isoformat(),
            vixy_close=out.vixy_close,
            vixm_close=out.vixm_close,
            vxz_close=out.vxz_close,
            vix_slope_4m_1m=out.vix_slope_4m_1m,
            vix_slope_7m_1m=out.vix_slope_7m_1m,
            spy_realized_vol_5d=out.spy_realized_vol_5d,
            spy_realized_vol_20d=out.spy_realized_vol_20d,
            spy_realized_vol_60d=out.spy_realized_vol_60d,
            vix_realized_gap=out.vix_realized_gap,
            cross_section_return_std_5d=out.cross_section_return_std_5d,
            cross_section_return_std_20d=out.cross_section_return_std_20d,
            mean_abs_corr_20d=out.mean_abs_corr_20d,
            corr_sample_size=int(out.corr_sample_size),
            sector_leadership_churn_20d=out.sector_leadership_churn_20d,
            vol_regime_label=out.vol_regime_label,
            vol_regime_numeric=int(out.vol_regime_numeric),
            dispersion_label=out.dispersion_label,
            dispersion_numeric=int(out.dispersion_numeric),
            correlation_label=out.correlation_label,
            correlation_numeric=int(out.correlation_numeric),
            universe_size=int(out.universe_size),
            tickers_missing=int(out.tickers_missing),
            coverage_score=float(out.coverage_score),
        ))

    if out.coverage_score < cfg.min_coverage_score:
        if _ops_log_enabled():
            logger.info(format_vol_dispersion_ops_line(
                event="vol_dispersion_skipped",
                mode=mode,
                snapshot_id=out.snapshot_id,
                as_of_date=as_of.isoformat(),
                coverage_score=float(out.coverage_score),
                reason="below_coverage",
            ))
        # Still persist for post-mortem but return None to signal no
        # confident snapshot available.
        try:
            _insert_row(db, out=out, mode=mode)
            db.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[vol_dispersion] low-coverage persist failed: %s", exc)
            db.rollback()
        return None

    try:
        pk = _insert_row(db, out=out, mode=mode)
        db.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[vol_dispersion] insert failed: %s", exc)
        db.rollback()
        return None

    if _ops_log_enabled():
        logger.info(format_vol_dispersion_ops_line(
            event="vol_dispersion_persisted",
            mode=mode,
            snapshot_id=out.snapshot_id,
            as_of_date=as_of.isoformat(),
            vol_regime_label=out.vol_regime_label,
            vol_regime_numeric=int(out.vol_regime_numeric),
            dispersion_label=out.dispersion_label,
            dispersion_numeric=int(out.dispersion_numeric),
            correlation_label=out.correlation_label,
            correlation_numeric=int(out.correlation_numeric),
            coverage_score=float(out.coverage_score),
            pk_id=pk,
        ))

    return VolDispersionRow(
        pk_id=pk,
        snapshot_id=out.snapshot_id,
        as_of_date=out.as_of_date,
        vol_regime_label=out.vol_regime_label,
        dispersion_label=out.dispersion_label,
        correlation_label=out.correlation_label,
        coverage_score=float(out.coverage_score),
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Read helpers + diagnostics summary
# ---------------------------------------------------------------------------


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "snapshot_id": str(row[1]),
        "as_of_date": row[2].isoformat() if row[2] is not None else None,
        "vixy_close": (float(row[3]) if row[3] is not None else None),
        "vixm_close": (float(row[4]) if row[4] is not None else None),
        "vxz_close": (float(row[5]) if row[5] is not None else None),
        "vix_slope_4m_1m": (float(row[6]) if row[6] is not None else None),
        "vix_slope_7m_1m": (float(row[7]) if row[7] is not None else None),
        "spy_realized_vol_5d": (float(row[8]) if row[8] is not None else None),
        "spy_realized_vol_20d": (float(row[9]) if row[9] is not None else None),
        "spy_realized_vol_60d": (float(row[10]) if row[10] is not None else None),
        "vix_realized_gap": (float(row[11]) if row[11] is not None else None),
        "cross_section_return_std_5d": (
            float(row[12]) if row[12] is not None else None
        ),
        "cross_section_return_std_20d": (
            float(row[13]) if row[13] is not None else None
        ),
        "mean_abs_corr_20d": (float(row[14]) if row[14] is not None else None),
        "corr_sample_size": int(row[15] or 0),
        "sector_leadership_churn_20d": (
            float(row[16]) if row[16] is not None else None
        ),
        "vol_regime_numeric": int(row[17] or 0),
        "vol_regime_label": str(row[18]),
        "dispersion_numeric": int(row[19] or 0),
        "dispersion_label": str(row[20]),
        "correlation_numeric": int(row[21] or 0),
        "correlation_label": str(row[22]),
        "universe_size": int(row[23] or 0),
        "tickers_missing": int(row[24] or 0),
        "coverage_score": float(row[25] or 0.0),
        "mode": str(row[26]),
        "computed_at": row[27].isoformat() if row[27] is not None else None,
        "observed_at": row[28].isoformat() if row[28] is not None else None,
    }


_SELECT_COLUMNS = """
    id, snapshot_id, as_of_date,
    vixy_close, vixm_close, vxz_close,
    vix_slope_4m_1m, vix_slope_7m_1m,
    spy_realized_vol_5d, spy_realized_vol_20d, spy_realized_vol_60d,
    vix_realized_gap,
    cross_section_return_std_5d, cross_section_return_std_20d,
    mean_abs_corr_20d, corr_sample_size,
    sector_leadership_churn_20d,
    vol_regime_numeric, vol_regime_label,
    dispersion_numeric, dispersion_label,
    correlation_numeric, correlation_label,
    universe_size, tickers_missing, coverage_score,
    mode, computed_at, observed_at
"""


def get_latest_snapshot(db: Session) -> dict[str, Any] | None:
    row = db.execute(text(f"""
        SELECT {_SELECT_COLUMNS}
          FROM trading_vol_dispersion_snapshots
         ORDER BY computed_at DESC
         LIMIT 1
    """)).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def vol_dispersion_summary(
    db: Session,
    *,
    lookback_days: int = 14,
) -> dict[str, Any]:
    """Frozen-shape diagnostics summary.

    Keys (stable):

    * ``mode``
    * ``lookback_days``
    * ``snapshots_total``
    * ``by_vol_regime_label``
    * ``by_dispersion_label``
    * ``by_correlation_label``
    * ``mean_vixy_close``
    * ``mean_vix_slope_4m_1m``
    * ``mean_cross_section_return_std_20d``
    * ``mean_abs_corr_20d``
    * ``mean_sector_leadership_churn_20d``
    * ``mean_coverage_score``
    * ``latest_snapshot`` - full latest row as a dict (or ``None``).
    """
    mode = _effective_mode()
    ld = int(lookback_days)

    total = int(db.execute(text("""
        SELECT COUNT(*) FROM trading_vol_dispersion_snapshots
         WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": ld}).scalar_one() or 0)

    def _count_by(col: str, valid: set[str]) -> dict[str, int]:
        out = {k: 0 for k in sorted(valid)}
        rows = db.execute(text(f"""
            SELECT {col}, COUNT(*)
              FROM trading_vol_dispersion_snapshots
             WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
               AND {col} IS NOT NULL
             GROUP BY {col}
        """), {"ld": ld}).fetchall()
        for k, v in rows:
            key = str(k)
            if key in out:
                out[key] = int(v or 0)
        return out

    by_vol = _count_by(
        "vol_regime_label",
        {
            VOL_REGIME_COMPRESSED,
            VOL_REGIME_NORMAL,
            VOL_REGIME_EXPANDED,
            VOL_REGIME_SPIKE,
        },
    )
    by_disp = _count_by(
        "dispersion_label",
        {DISPERSION_LOW, DISPERSION_NORMAL, DISPERSION_HIGH},
    )
    by_corr = _count_by(
        "correlation_label",
        {CORRELATION_LOW, CORRELATION_NORMAL, CORRELATION_SPIKE},
    )

    means = db.execute(text("""
        SELECT
            AVG(vixy_close),
            AVG(vix_slope_4m_1m),
            AVG(cross_section_return_std_20d),
            AVG(mean_abs_corr_20d),
            AVG(sector_leadership_churn_20d),
            AVG(coverage_score)
          FROM trading_vol_dispersion_snapshots
         WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": ld}).fetchone()

    def _f(idx: int) -> float | None:
        if means is None:
            return None
        v = means[idx]
        return None if v is None else round(float(v), 6)

    return {
        "mode": mode,
        "lookback_days": ld,
        "snapshots_total": int(total),
        "by_vol_regime_label": by_vol,
        "by_dispersion_label": by_disp,
        "by_correlation_label": by_corr,
        "mean_vixy_close": _f(0),
        "mean_vix_slope_4m_1m": _f(1),
        "mean_cross_section_return_std_20d": _f(2),
        "mean_abs_corr_20d": _f(3),
        "mean_sector_leadership_churn_20d": _f(4),
        "mean_coverage_score": _f(5),
        "latest_snapshot": get_latest_snapshot(db),
    }


__all__ = [
    "VolDispersionRow",
    "TERM_SYMBOLS",
    "SECTOR_SYMBOLS",
    "_effective_mode",
    "mode_is_active",
    "mode_is_authoritative",
    "compute_and_persist",
    "get_latest_snapshot",
    "vol_dispersion_summary",
]
