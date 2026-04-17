"""Phase L.17 - persistence layer for the macro regime snapshot.

Runs the pure macro regime model against:

* The existing equity composite (SPY / VIX) produced by
  :func:`app.services.trading.market_data.get_market_regime`.
* Per-ETF OHLCV trends for IEF/SHY/TLT/HYG/LQD/UUP fetched via
  :func:`app.services.trading.market_data.fetch_ohlcv_df`.

Design
------

* **Single public entry-point per mode.** :func:`compute_and_persist`
  (one sweep, one row) and :func:`macro_regime_summary` (diagnostics).
* **Refuses authoritative.** Until Phase L.17.2 opens explicitly the
  service raises :class:`RuntimeError` on ``mode_override="authoritative"``
  or ``brain_macro_regime_mode="authoritative"``.
* **Append-only.** Every sweep appends a new row; the deterministic
  ``regime_id`` lets callers dedupe.
* **Off-mode short-circuit.** When
  ``brain_macro_regime_mode == "off"``
  :func:`compute_and_persist` is a no-op and returns ``None``.
* **Additive-only.** :func:`market_data.get_market_regime` is never
  modified; the equity block is a read-only echo.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.macro_regime_ops_log import (
    format_macro_regime_ops_line,
)
from .macro_regime_model import (
    ALL_SYMBOLS,
    AssetReading,
    EquityRegimeInput,
    MacroRegimeConfig,
    MacroRegimeInput,
    MacroRegimeOutput,
    SYMBOL_HYG,
    SYMBOL_IEF,
    SYMBOL_LQD,
    SYMBOL_SHY,
    SYMBOL_TLT,
    SYMBOL_UUP,
    TREND_MISSING,
    classify_trend,
    compute_macro_regime,
    compute_regime_id,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------


def _effective_mode(override: str | None = None) -> str:
    m = (
        override
        or getattr(settings, "brain_macro_regime_mode", "off")
        or "off"
    ).lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(
        getattr(settings, "brain_macro_regime_ops_log_enabled", True)
    )


def _config_from_settings() -> MacroRegimeConfig:
    return MacroRegimeConfig(
        trend_up_threshold=float(getattr(
            settings, "brain_macro_regime_trend_up_threshold", 0.01,
        )),
        strong_trend_threshold=float(getattr(
            settings, "brain_macro_regime_strong_trend_threshold", 0.03,
        )),
        min_coverage_score=float(getattr(
            settings, "brain_macro_regime_min_coverage_score", 0.5,
        )),
        weight_rates=float(getattr(
            settings, "brain_macro_regime_weight_rates", 0.45,
        )),
        weight_credit=float(getattr(
            settings, "brain_macro_regime_weight_credit", 0.35,
        )),
        weight_usd=float(getattr(
            settings, "brain_macro_regime_weight_usd", 0.20,
        )),
        promote_threshold=float(getattr(
            settings, "brain_macro_regime_promote_threshold", 0.35,
        )),
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MacroRegimeRow:
    """Thin reference to a persisted snapshot."""

    snapshot_id: int
    regime_id: str
    as_of_date: date
    macro_label: str
    macro_numeric: int
    coverage_score: float
    mode: str


# ---------------------------------------------------------------------------
# ETF fetching / reading assembly
# ---------------------------------------------------------------------------


def _build_asset_reading(symbol: str, *, lookback_days: int = 45) -> AssetReading:
    """Fetch OHLCV for one ETF and derive trend + momentum.

    Defensive: any provider failure returns a ``missing=True`` reading.
    Equity lookback of ~45 trading days is enough for 20d momentum plus
    some safety margin; we never raise here.
    """
    try:
        # Local import so unit tests can monkey-patch market_data cheaply.
        from .market_data import fetch_ohlcv_df  # noqa: WPS433

        df = fetch_ohlcv_df(symbol, interval="1d", period="3mo")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[macro_regime] fetch_ohlcv_df(%s) raised: %s", symbol, exc,
        )
        return AssetReading(symbol=symbol, missing=True, trend=TREND_MISSING)

    if df is None or df.empty or "Close" not in getattr(df, "columns", []):
        return AssetReading(symbol=symbol, missing=True, trend=TREND_MISSING)

    try:
        closes = df["Close"].dropna()
        if len(closes) < 21:
            return AssetReading(symbol=symbol, missing=True, trend=TREND_MISSING)
        last = float(closes.iloc[-1])
        prior_20 = float(closes.iloc[-21])
        prior_5 = float(closes.iloc[-6]) if len(closes) >= 6 else None
        momentum_20d = (
            (last / prior_20 - 1.0) if prior_20 > 0 else None
        )
        momentum_5d = (
            (last / prior_5 - 1.0)
            if (prior_5 is not None and prior_5 > 0)
            else None
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[macro_regime] parse_ohlcv(%s) raised: %s", symbol, exc,
        )
        return AssetReading(symbol=symbol, missing=True, trend=TREND_MISSING)

    cfg = _config_from_settings()
    trend = classify_trend(momentum_20d, cfg=cfg)
    return AssetReading(
        symbol=symbol,
        missing=False,
        last_close=float(last),
        momentum_20d=(
            None if momentum_20d is None else float(momentum_20d)
        ),
        momentum_5d=(
            None if momentum_5d is None else float(momentum_5d)
        ),
        trend=trend,
    )


def gather_asset_readings() -> list[AssetReading]:
    """Fetch per-symbol readings for every ETF in :data:`ALL_SYMBOLS`.

    Never raises. Missing providers produce ``missing=True`` entries so
    the pure model's coverage-score logic is accurate.
    """
    readings: list[AssetReading] = []
    for sym in ALL_SYMBOLS:
        readings.append(_build_asset_reading(sym))
    return readings


def _build_equity_input() -> EquityRegimeInput:
    """Echo ``market_data.get_market_regime()`` into the pure input.

    Swallows exceptions defensively: the pure model handles ``None``
    fields without raising.
    """
    try:
        from .market_data import get_market_regime  # noqa: WPS433

        reg = get_market_regime() or {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[macro_regime] get_market_regime() raised: %s", exc)
        reg = {}

    def _str_or_none(v: Any) -> str | None:
        if v is None:
            return None
        s = str(v)
        return s if s else None

    def _float_or_none(v: Any) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _int_or_none(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return EquityRegimeInput(
        spy_direction=_str_or_none(reg.get("spy_direction")),
        spy_momentum_5d=_float_or_none(reg.get("spy_momentum_5d")),
        vix=_float_or_none(reg.get("vix")),
        vix_regime=_str_or_none(reg.get("vix_regime")),
        volatility_percentile=_float_or_none(reg.get("volatility_percentile")),
        composite=_str_or_none(reg.get("regime")),  # get_market_regime uses "regime"
        regime_numeric=_int_or_none(reg.get("regime_numeric")),
    )


# ---------------------------------------------------------------------------
# Persist one sweep
# ---------------------------------------------------------------------------


def _today_utc_date() -> date:
    return datetime.now(tz=timezone.utc).date()


def compute_and_persist(
    db: Session,
    *,
    as_of_date: date | None = None,
    mode_override: str | None = None,
    readings_override: Sequence[AssetReading] | None = None,
    equity_override: EquityRegimeInput | None = None,
) -> MacroRegimeRow | None:
    """Compute the snapshot and persist one row in shadow / compare mode.

    Off-mode: returns ``None`` and emits a ``macro_regime_skipped`` line.
    Authoritative-mode: raises :class:`RuntimeError`.

    ``readings_override`` / ``equity_override`` let tests + the soak
    script drive the service without the network.
    """
    mode = _effective_mode(mode_override)
    as_of = as_of_date or _today_utc_date()

    if mode == "off":
        if _ops_log_enabled():
            logger.info(format_macro_regime_ops_line(
                event="macro_regime_skipped",
                mode=mode,
                as_of_date=as_of.isoformat(),
                reason="mode_off",
            ))
        return None

    if mode == "authoritative":
        if _ops_log_enabled():
            logger.warning(format_macro_regime_ops_line(
                event="macro_regime_refused_authoritative",
                mode=mode,
                as_of_date=as_of.isoformat(),
                reason="L.17.1_shadow_only",
            ))
        raise RuntimeError(
            "macro_regime authoritative mode is not permitted until "
            "Phase L.17.2 is explicitly opened"
        )

    # Gather inputs.
    equity = equity_override if equity_override is not None else _build_equity_input()
    readings = (
        list(readings_override)
        if readings_override is not None
        else gather_asset_readings()
    )

    # Pure compute.
    out: MacroRegimeOutput = compute_macro_regime(
        MacroRegimeInput(
            as_of_date=as_of, equity=equity, readings=readings,
        ),
        config=_config_from_settings(),
    )

    if _ops_log_enabled():
        logger.info(format_macro_regime_ops_line(
            event="macro_regime_computed",
            mode=mode,
            regime_id=out.regime_id,
            as_of_date=as_of.isoformat(),
            macro_label=out.macro_label,
            macro_numeric=out.macro_numeric,
            rates_regime=out.rates_regime,
            credit_regime=out.credit_regime,
            usd_regime=out.usd_regime,
            symbols_sampled=out.symbols_sampled,
            symbols_missing=out.symbols_missing,
            coverage_score=float(out.coverage_score),
        ))

    # Coverage gate: skip persistence when the sweep is too thin to
    # be actionable. This still logs a computed/skipped event so
    # operators can see coverage decayed.
    cfg = _config_from_settings()
    if out.coverage_score < cfg.min_coverage_score:
        if _ops_log_enabled():
            logger.warning(format_macro_regime_ops_line(
                event="macro_regime_skipped",
                mode=mode,
                regime_id=out.regime_id,
                as_of_date=as_of.isoformat(),
                macro_label=out.macro_label,
                coverage_score=float(out.coverage_score),
                reason="coverage_below_min",
            ))
        return None

    payload_json = json.dumps(out.payload, default=str)
    now = datetime.utcnow()

    row = db.execute(text("""
        INSERT INTO trading_macro_regime_snapshots (
            regime_id, as_of_date,
            spy_direction, spy_momentum_5d, vix, vix_regime,
            volatility_percentile, composite, regime_numeric,
            ief_trend, shy_trend, tlt_trend,
            yield_curve_slope_proxy, rates_regime,
            hyg_trend, lqd_trend, credit_spread_proxy, credit_regime,
            uup_trend, uup_momentum_20d, usd_regime,
            macro_numeric, macro_label,
            symbols_sampled, symbols_missing, coverage_score,
            payload_json, mode, computed_at, observed_at
        ) VALUES (
            :regime_id, :as_of_date,
            :spy_direction, :spy_momentum_5d, :vix, :vix_regime,
            :volatility_percentile, :composite, :regime_numeric,
            :ief_trend, :shy_trend, :tlt_trend,
            :yield_curve_slope_proxy, :rates_regime,
            :hyg_trend, :lqd_trend, :credit_spread_proxy, :credit_regime,
            :uup_trend, :uup_momentum_20d, :usd_regime,
            :macro_numeric, :macro_label,
            :symbols_sampled, :symbols_missing, :coverage_score,
            CAST(:payload_json AS JSONB), :mode, :computed_at, :observed_at
        ) RETURNING id
    """), {
        "regime_id": out.regime_id,
        "as_of_date": out.as_of_date,
        "spy_direction": out.spy_direction,
        "spy_momentum_5d": out.spy_momentum_5d,
        "vix": out.vix,
        "vix_regime": out.vix_regime,
        "volatility_percentile": out.volatility_percentile,
        "composite": out.composite,
        "regime_numeric": out.regime_numeric,
        "ief_trend": out.ief_trend,
        "shy_trend": out.shy_trend,
        "tlt_trend": out.tlt_trend,
        "yield_curve_slope_proxy": out.yield_curve_slope_proxy,
        "rates_regime": out.rates_regime,
        "hyg_trend": out.hyg_trend,
        "lqd_trend": out.lqd_trend,
        "credit_spread_proxy": out.credit_spread_proxy,
        "credit_regime": out.credit_regime,
        "uup_trend": out.uup_trend,
        "uup_momentum_20d": out.uup_momentum_20d,
        "usd_regime": out.usd_regime,
        "macro_numeric": int(out.macro_numeric),
        "macro_label": out.macro_label,
        "symbols_sampled": int(out.symbols_sampled),
        "symbols_missing": int(out.symbols_missing),
        "coverage_score": float(out.coverage_score),
        "payload_json": payload_json,
        "mode": mode,
        "computed_at": now,
        "observed_at": now,
    }).fetchone()
    db.commit()

    snapshot_id = int(row[0]) if row else 0

    if _ops_log_enabled():
        logger.info(format_macro_regime_ops_line(
            event="macro_regime_persisted",
            mode=mode,
            regime_id=out.regime_id,
            as_of_date=as_of.isoformat(),
            macro_label=out.macro_label,
            macro_numeric=out.macro_numeric,
            rates_regime=out.rates_regime,
            credit_regime=out.credit_regime,
            usd_regime=out.usd_regime,
            symbols_sampled=out.symbols_sampled,
            symbols_missing=out.symbols_missing,
            coverage_score=float(out.coverage_score),
            snapshot_id=snapshot_id,
        ))

    return MacroRegimeRow(
        snapshot_id=snapshot_id,
        regime_id=out.regime_id,
        as_of_date=out.as_of_date,
        macro_label=out.macro_label,
        macro_numeric=int(out.macro_numeric),
        coverage_score=float(out.coverage_score),
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def get_latest_snapshot(db: Session) -> dict[str, Any] | None:
    """Read the most recent snapshot as a plain dict.

    Read-only helper used by the diagnostics endpoint. Not yet wired
    into any hot-path consumer (that is L.17.2).
    """
    row = db.execute(text("""
        SELECT id, regime_id, as_of_date, macro_label, macro_numeric,
               rates_regime, credit_regime, usd_regime,
               symbols_sampled, symbols_missing, coverage_score,
               mode, computed_at, observed_at
          FROM trading_macro_regime_snapshots
         ORDER BY computed_at DESC
         LIMIT 1
    """)).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "regime_id": str(row[1]),
        "as_of_date": row[2].isoformat() if row[2] is not None else None,
        "macro_label": str(row[3]),
        "macro_numeric": int(row[4] or 0),
        "rates_regime": (str(row[5]) if row[5] is not None else None),
        "credit_regime": (str(row[6]) if row[6] is not None else None),
        "usd_regime": (str(row[7]) if row[7] is not None else None),
        "symbols_sampled": int(row[8] or 0),
        "symbols_missing": int(row[9] or 0),
        "coverage_score": float(row[10] or 0.0),
        "mode": str(row[11]),
        "computed_at": row[12].isoformat() if row[12] is not None else None,
        "observed_at": row[13].isoformat() if row[13] is not None else None,
    }


def macro_regime_summary(
    db: Session,
    *,
    lookback_days: int = 14,
) -> dict[str, Any]:
    """Frozen-shape diagnostics summary for the macro regime panel.

    Keys (stable, order-preserving):

    * ``mode``
    * ``lookback_days``
    * ``snapshots_total``
    * ``by_label`` - ``{risk_on, cautious, risk_off}``
    * ``by_rates_regime``
    * ``by_credit_regime``
    * ``by_usd_regime``
    * ``mean_coverage_score``
    * ``latest_snapshot`` (or ``None`` when empty)
    """
    mode = _effective_mode()

    total = int(db.execute(text("""
        SELECT COUNT(*) FROM trading_macro_regime_snapshots
        WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0)

    label_rows = db.execute(text("""
        SELECT macro_label, COUNT(*) FROM trading_macro_regime_snapshots
        WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
        GROUP BY macro_label
    """), {"ld": int(lookback_days)}).fetchall()
    by_label = {"risk_on": 0, "cautious": 0, "risk_off": 0}
    for lbl, cnt in label_rows:
        key = str(lbl)
        if key in by_label:
            by_label[key] = int(cnt or 0)

    def _count_by(col: str) -> dict[str, int]:
        rows = db.execute(text(f"""
            SELECT {col}, COUNT(*) FROM trading_macro_regime_snapshots
            WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
              AND {col} IS NOT NULL
            GROUP BY {col}
        """), {"ld": int(lookback_days)}).fetchall()
        return {str(k): int(v or 0) for k, v in rows}

    mean_coverage = float(db.execute(text("""
        SELECT AVG(coverage_score) FROM trading_macro_regime_snapshots
         WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0.0)

    latest = get_latest_snapshot(db)

    return {
        "mode": mode,
        "lookback_days": int(lookback_days),
        "snapshots_total": int(total),
        "by_label": by_label,
        "by_rates_regime": _count_by("rates_regime"),
        "by_credit_regime": _count_by("credit_regime"),
        "by_usd_regime": _count_by("usd_regime"),
        "mean_coverage_score": round(float(mean_coverage), 6),
        "latest_snapshot": latest,
    }


__all__ = [
    "MacroRegimeRow",
    "_effective_mode",
    "mode_is_active",
    "mode_is_authoritative",
    "gather_asset_readings",
    "compute_and_persist",
    "get_latest_snapshot",
    "macro_regime_summary",
]
