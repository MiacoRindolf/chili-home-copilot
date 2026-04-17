"""Phase L.20 - persistence layer for the per-ticker regime snapshot.

Runs the pure :mod:`ticker_regime_model` against per-ticker daily OHLCV
fetched via :func:`app.services.trading.market_data.fetch_ohlcv_df` for
the snapshot-universe (scan + watchlist) bounded by
``brain_ticker_regime_max_tickers``.

Design
------
* **Two public entry-points.** :func:`compute_and_persist_sweep` writes
  one row per eligible ticker and emits a
  ``ticker_regime_sweep_summary`` ops line;
  :func:`ticker_regime_summary` returns the diagnostics dict for the
  FastAPI route.
* **Refuses authoritative.** Until Phase L.20.2 opens explicitly the
  service raises :class:`RuntimeError` on
  ``mode_override="authoritative"`` or
  ``brain_ticker_regime_mode="authoritative"``. A refusal ops line is
  emitted before the raise so ops / release blockers can see the
  attempt.
* **Append-only.** Every sweep appends rows; the deterministic
  ``snapshot_id`` (dated by ``(as_of_date, ticker)``) lets callers
  dedupe.
* **Off-mode short-circuit.** When ``brain_ticker_regime_mode == "off"``
  :func:`compute_and_persist_sweep` emits a single skip line and
  returns an empty result.
* **Additive-only.** The existing
  ``momentum_neural.hurst_proxy_from_closes`` helper and every other
  downstream consumer are never invoked from here.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.ticker_regime_ops_log import (
    format_ticker_regime_ops_line,
)
from .ticker_regime_model import (
    OHLCVSeries,
    TICKER_REGIME_CHOPPY,
    TICKER_REGIME_MEAN_REVERT,
    TICKER_REGIME_NEUTRAL,
    TICKER_REGIME_TREND_DOWN,
    TICKER_REGIME_TREND_UP,
    TickerRegimeConfig,
    TickerRegimeInput,
    TickerRegimeOutput,
    compute_snapshot_id,
    compute_ticker_regime,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------


def _effective_mode(override: str | None = None) -> str:
    m = (
        override
        or getattr(settings, "brain_ticker_regime_mode", "off")
        or "off"
    ).lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(
        getattr(settings, "brain_ticker_regime_ops_log_enabled", True)
    )


def _config_from_settings() -> TickerRegimeConfig:
    return TickerRegimeConfig(
        min_bars=int(getattr(settings, "brain_ticker_regime_min_bars", 40)),
        min_coverage_score=float(getattr(
            settings, "brain_ticker_regime_min_coverage_score", 0.5,
        )),
        ac1_mean_revert=float(getattr(
            settings, "brain_ticker_regime_ac1_mean_revert", -0.05,
        )),
        ac1_trend=float(getattr(
            settings, "brain_ticker_regime_ac1_trend", 0.05,
        )),
        hurst_mean_revert=float(getattr(
            settings, "brain_ticker_regime_hurst_mean_revert", 0.45,
        )),
        hurst_trend=float(getattr(
            settings, "brain_ticker_regime_hurst_trend", 0.55,
        )),
        vr_mean_revert=float(getattr(
            settings, "brain_ticker_regime_vr_mean_revert", 0.95,
        )),
        vr_trend=float(getattr(
            settings, "brain_ticker_regime_vr_trend", 1.05,
        )),
        adx_trend=float(getattr(
            settings, "brain_ticker_regime_adx_trend", 20.0,
        )),
        atr_period=int(getattr(
            settings, "brain_ticker_regime_atr_period", 14,
        )),
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickerRegimeRow:
    """Thin reference to a persisted per-ticker row."""

    pk_id: int
    snapshot_id: str
    as_of_date: date
    ticker: str
    ticker_regime_label: str
    ticker_regime_numeric: int
    coverage_score: float
    mode: str


@dataclass(frozen=True)
class TickerRegimeSweepResult:
    """Summary of one ``compute_and_persist_sweep`` invocation."""

    mode: str
    as_of_date: date
    tickers_attempted: int
    tickers_persisted: int
    tickers_skipped: int
    by_label: dict[str, int]
    rows: tuple[TickerRegimeRow, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# OHLCV assembly
# ---------------------------------------------------------------------------


def _build_ohlcv_series(ticker: str) -> OHLCVSeries | None:
    """Fetch daily OHLCV for ``ticker`` (9mo by default, trimmed to 252
    bars). Returns ``None`` on provider failure so the sweep emits a
    ``reason=provider_error`` skip line instead of raising.
    """
    try:
        from .market_data import fetch_ohlcv_df  # noqa: WPS433

        df = fetch_ohlcv_df(ticker, interval="1d", period="9mo")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[ticker_regime] fetch_ohlcv_df(%s) raised: %s", ticker, exc,
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
        # Align to shortest common index.
        common = closes_s.index.intersection(highs_s.index).intersection(
            lows_s.index
        )
        if len(common) < 1:
            return None
        common = common.sort_values()
        # Cap to ~1y of daily bars.
        if len(common) > 260:
            common = common[-260:]
        closes = tuple(float(x) for x in closes_s.loc[common].tolist())
        highs = tuple(float(x) for x in highs_s.loc[common].tolist())
        lows = tuple(float(x) for x in lows_s.loc[common].tolist())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[ticker_regime] parse_ohlcv(%s) raised: %s", ticker, exc,
        )
        return None

    return OHLCVSeries(
        ticker=ticker,
        asset_class=_asset_class_for(ticker),
        closes=closes,
        highs=highs,
        lows=lows,
    )


def _asset_class_for(ticker: str) -> str:
    """Rough asset-class tag from the CHILI symbol convention.

    ``BASE-USD`` / bare ``BASEUSD`` -> ``crypto``; everything else is
    treated as ``equity`` for snapshot-panel purposes. This matches the
    convention used by the L.19 cross-asset builder.
    """
    if not ticker:
        return "equity"
    t = ticker.strip().upper()
    if t.endswith("-USD"):
        return "crypto"
    if t.endswith("USD") and 5 <= len(t) <= 18 and t[:-3].isalnum():
        return "crypto"
    return "equity"


def _load_universe(
    db: Session, *, max_tickers: int, universe_override: Sequence[str] | None,
) -> list[str]:
    """Return the bounded ticker universe for this sweep."""
    if universe_override is not None:
        return [str(t).strip().upper() for t in universe_override if t]
    try:
        from .scanner import build_snapshot_ticker_universe  # noqa: WPS433

        tickers, _meta = build_snapshot_ticker_universe(
            db, user_id=None, limit=max_tickers,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[ticker_regime] universe build failed: %s", exc,
        )
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in tickers:
        if not t:
            continue
        key = str(t).strip().upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= max_tickers:
            break
    return out


# ---------------------------------------------------------------------------
# Persist one row
# ---------------------------------------------------------------------------


def _today_utc_date() -> date:
    return datetime.now(tz=timezone.utc).date()


def _insert_row(
    db: Session,
    *,
    out: TickerRegimeOutput,
    mode: str,
) -> int:
    payload_json = json.dumps(out.payload, default=str)
    now = datetime.utcnow()
    row = db.execute(text("""
        INSERT INTO trading_ticker_regime_snapshots (
            snapshot_id, as_of_date, ticker, asset_class,
            last_close, sigma_20d, ac1, vr_5, vr_20, hurst, adx_proxy,
            trend_score, mean_revert_score,
            ticker_regime_numeric, ticker_regime_label,
            bars_used, bars_missing, coverage_score,
            payload_json, mode, computed_at, observed_at
        ) VALUES (
            :snapshot_id, :as_of_date, :ticker, :asset_class,
            :last_close, :sigma_20d, :ac1, :vr_5, :vr_20, :hurst, :adx_proxy,
            :trend_score, :mean_revert_score,
            :tr_num, :tr_label,
            :bars_used, :bars_missing, :coverage,
            CAST(:payload_json AS JSONB), :mode, :computed_at, :observed_at
        ) RETURNING id
    """), {
        "snapshot_id": out.snapshot_id,
        "as_of_date": out.as_of_date,
        "ticker": out.ticker,
        "asset_class": out.asset_class,
        "last_close": out.last_close,
        "sigma_20d": out.sigma_20d,
        "ac1": out.ac1,
        "vr_5": out.vr_5,
        "vr_20": out.vr_20,
        "hurst": out.hurst,
        "adx_proxy": out.adx_proxy,
        "trend_score": float(out.trend_score),
        "mean_revert_score": float(out.mean_revert_score),
        "tr_num": int(out.ticker_regime_numeric),
        "tr_label": out.ticker_regime_label,
        "bars_used": int(out.bars_used),
        "bars_missing": int(out.bars_missing),
        "coverage": float(out.coverage_score),
        "payload_json": payload_json,
        "mode": mode,
        "computed_at": now,
        "observed_at": now,
    }).fetchone()
    return int(row[0]) if row else 0


def _compute_one(
    ticker: str,
    *,
    as_of: date,
    cfg: TickerRegimeConfig,
    series_override: OHLCVSeries | None,
) -> TickerRegimeOutput:
    series = series_override if series_override is not None else _build_ohlcv_series(ticker)
    if series is None:
        # Produce a neutral output with ``coverage_score=0`` so callers
        # can distinguish provider errors from zero-variance or
        # short-history cases.
        return TickerRegimeOutput(
            snapshot_id=compute_snapshot_id(as_of, ticker),
            as_of_date=as_of,
            ticker=ticker.strip().upper(),
            asset_class=_asset_class_for(ticker),
            last_close=None,
            sigma_20d=None,
            ac1=None,
            vr_5=None,
            vr_20=None,
            hurst=None,
            adx_proxy=None,
            trend_score=0.0,
            mean_revert_score=0.0,
            ticker_regime_numeric=0,
            ticker_regime_label=TICKER_REGIME_NEUTRAL,
            bars_used=0,
            bars_missing=cfg.min_bars,
            coverage_score=0.0,
            payload={"reason": "provider_error"},
        )
    inp = TickerRegimeInput(as_of_date=as_of, series=series, config=cfg)
    return compute_ticker_regime(inp)


def compute_and_persist_sweep(
    db: Session,
    *,
    as_of_date: date | None = None,
    mode_override: str | None = None,
    universe_override: Sequence[str] | None = None,
    series_overrides: dict[str, OHLCVSeries] | None = None,
    max_tickers: int | None = None,
) -> TickerRegimeSweepResult:
    """Run one daily sweep.

    Off-mode emits a single ``ticker_regime_skipped`` line and returns
    an empty result. Authoritative-mode emits a
    ``ticker_regime_refused_authoritative`` line and raises
    :class:`RuntimeError`.

    Rows whose ``coverage_score`` sits below
    ``brain_ticker_regime_min_coverage_score`` are **still persisted**
    (so the ops log / release blocker can see them) but are excluded
    from the per-label rollup returned to the sweep-summary ops line.
    """
    mode = _effective_mode(mode_override)
    as_of = as_of_date or _today_utc_date()

    if mode == "off":
        if _ops_log_enabled():
            logger.info(format_ticker_regime_ops_line(
                event="ticker_regime_skipped",
                mode=mode,
                as_of_date=as_of.isoformat(),
                reason="mode_off",
            ))
        return TickerRegimeSweepResult(
            mode=mode,
            as_of_date=as_of,
            tickers_attempted=0,
            tickers_persisted=0,
            tickers_skipped=0,
            by_label={},
            rows=(),
        )

    if mode == "authoritative":
        if _ops_log_enabled():
            logger.warning(format_ticker_regime_ops_line(
                event="ticker_regime_refused_authoritative",
                mode=mode,
                as_of_date=as_of.isoformat(),
                reason="L.20.1_shadow_only",
            ))
        raise RuntimeError(
            "ticker_regime authoritative mode is not permitted until "
            "Phase L.20.2 is explicitly opened"
        )

    cap = int(
        max_tickers
        if max_tickers is not None
        else getattr(settings, "brain_ticker_regime_max_tickers", 250)
    )
    cap = max(1, min(cap, 5000))
    universe = _load_universe(
        db, max_tickers=cap, universe_override=universe_override,
    )
    cfg = _config_from_settings()

    rows: list[TickerRegimeRow] = []
    by_label: dict[str, int] = {
        TICKER_REGIME_TREND_UP: 0,
        TICKER_REGIME_TREND_DOWN: 0,
        TICKER_REGIME_MEAN_REVERT: 0,
        TICKER_REGIME_CHOPPY: 0,
        TICKER_REGIME_NEUTRAL: 0,
    }
    persisted = 0
    skipped = 0

    for ticker in universe:
        series_override = (
            series_overrides.get(ticker.upper()) if series_overrides else None
        )
        out = _compute_one(
            ticker, as_of=as_of, cfg=cfg, series_override=series_override,
        )
        if _ops_log_enabled():
            logger.info(format_ticker_regime_ops_line(
                event="ticker_regime_computed",
                mode=mode,
                snapshot_id=out.snapshot_id,
                as_of_date=as_of.isoformat(),
                ticker=out.ticker,
                asset_class=out.asset_class,
                ticker_regime_label=out.ticker_regime_label,
                ticker_regime_numeric=int(out.ticker_regime_numeric),
                ac1=out.ac1,
                vr_5=out.vr_5,
                vr_20=out.vr_20,
                hurst=out.hurst,
                adx_proxy=out.adx_proxy,
                sigma_20d=out.sigma_20d,
                trend_score=float(out.trend_score),
                mean_revert_score=float(out.mean_revert_score),
                bars_used=int(out.bars_used),
                bars_missing=int(out.bars_missing),
                coverage_score=float(out.coverage_score),
            ))
        try:
            pk = _insert_row(db, out=out, mode=mode)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[ticker_regime] insert failed for %s: %s",
                out.ticker,
                exc,
            )
            if _ops_log_enabled():
                logger.warning(format_ticker_regime_ops_line(
                    event="ticker_regime_skipped",
                    mode=mode,
                    snapshot_id=out.snapshot_id,
                    as_of_date=as_of.isoformat(),
                    ticker=out.ticker,
                    reason="persist_error",
                ))
            skipped += 1
            continue

        rows.append(TickerRegimeRow(
            pk_id=pk,
            snapshot_id=out.snapshot_id,
            as_of_date=out.as_of_date,
            ticker=out.ticker,
            ticker_regime_label=out.ticker_regime_label,
            ticker_regime_numeric=int(out.ticker_regime_numeric),
            coverage_score=float(out.coverage_score),
            mode=mode,
        ))
        persisted += 1
        if _ops_log_enabled():
            logger.info(format_ticker_regime_ops_line(
                event="ticker_regime_persisted",
                mode=mode,
                snapshot_id=out.snapshot_id,
                as_of_date=as_of.isoformat(),
                ticker=out.ticker,
                ticker_regime_label=out.ticker_regime_label,
                ticker_regime_numeric=int(out.ticker_regime_numeric),
                coverage_score=float(out.coverage_score),
                pk_id=pk,
            ))
        # Only tickers with good coverage are reflected in by_label.
        if out.coverage_score >= cfg.min_coverage_score:
            label = out.ticker_regime_label
            if label in by_label:
                by_label[label] += 1
    try:
        db.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[ticker_regime] commit failed: %s", exc)
        db.rollback()

    if _ops_log_enabled():
        logger.info(format_ticker_regime_ops_line(
            event="ticker_regime_sweep_summary",
            mode=mode,
            as_of_date=as_of.isoformat(),
            tickers_attempted=len(universe),
            tickers_persisted=persisted,
            tickers_skipped=skipped,
            trend_up_count=by_label[TICKER_REGIME_TREND_UP],
            trend_down_count=by_label[TICKER_REGIME_TREND_DOWN],
            mean_revert_count=by_label[TICKER_REGIME_MEAN_REVERT],
            choppy_count=by_label[TICKER_REGIME_CHOPPY],
            neutral_count=by_label[TICKER_REGIME_NEUTRAL],
        ))

    return TickerRegimeSweepResult(
        mode=mode,
        as_of_date=as_of,
        tickers_attempted=len(universe),
        tickers_persisted=persisted,
        tickers_skipped=skipped,
        by_label=dict(by_label),
        rows=tuple(rows),
    )


# ---------------------------------------------------------------------------
# Read helpers + summary
# ---------------------------------------------------------------------------


def get_latest_snapshot_for_ticker(
    db: Session, ticker: str,
) -> dict[str, Any] | None:
    """Read the most-recent snapshot for one ticker as a plain dict."""
    row = db.execute(text("""
        SELECT id, snapshot_id, as_of_date, ticker, asset_class,
               last_close, sigma_20d, ac1, vr_5, vr_20, hurst, adx_proxy,
               trend_score, mean_revert_score,
               ticker_regime_numeric, ticker_regime_label,
               bars_used, bars_missing, coverage_score,
               mode, computed_at, observed_at
          FROM trading_ticker_regime_snapshots
         WHERE ticker = :t
         ORDER BY computed_at DESC
         LIMIT 1
    """), {"t": ticker.strip().upper()}).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "snapshot_id": str(row[1]),
        "as_of_date": row[2].isoformat() if row[2] is not None else None,
        "ticker": str(row[3]),
        "asset_class": (str(row[4]) if row[4] is not None else None),
        "last_close": (float(row[5]) if row[5] is not None else None),
        "sigma_20d": (float(row[6]) if row[6] is not None else None),
        "ac1": (float(row[7]) if row[7] is not None else None),
        "vr_5": (float(row[8]) if row[8] is not None else None),
        "vr_20": (float(row[9]) if row[9] is not None else None),
        "hurst": (float(row[10]) if row[10] is not None else None),
        "adx_proxy": (float(row[11]) if row[11] is not None else None),
        "trend_score": (float(row[12]) if row[12] is not None else None),
        "mean_revert_score": (float(row[13]) if row[13] is not None else None),
        "ticker_regime_numeric": int(row[14] or 0),
        "ticker_regime_label": str(row[15]),
        "bars_used": int(row[16] or 0),
        "bars_missing": int(row[17] or 0),
        "coverage_score": float(row[18] or 0.0),
        "mode": str(row[19]),
        "computed_at": row[20].isoformat() if row[20] is not None else None,
        "observed_at": row[21].isoformat() if row[21] is not None else None,
    }


def ticker_regime_summary(
    db: Session,
    *,
    lookback_days: int = 7,
    latest_tickers_limit: int = 20,
) -> dict[str, Any]:
    """Frozen-shape diagnostics summary for the per-ticker panel.

    Keys (stable):

    * ``mode``
    * ``lookback_days``
    * ``snapshots_total``
    * ``distinct_tickers``
    * ``by_ticker_regime_label``
    * ``by_asset_class``
    * ``mean_coverage_score``
    * ``mean_trend_score``
    * ``mean_mean_revert_score``
    * ``latest_tickers`` - most-recent snapshot per distinct ticker,
      capped to ``latest_tickers_limit`` entries.
    """
    mode = _effective_mode()

    total = int(db.execute(text("""
        SELECT COUNT(*) FROM trading_ticker_regime_snapshots
         WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0)

    distinct_n = int(db.execute(text("""
        SELECT COUNT(DISTINCT ticker) FROM trading_ticker_regime_snapshots
         WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0)

    def _count_by(col: str) -> dict[str, int]:
        rows = db.execute(text(f"""
            SELECT {col}, COUNT(*)
              FROM trading_ticker_regime_snapshots
             WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
               AND {col} IS NOT NULL
             GROUP BY {col}
        """), {"ld": int(lookback_days)}).fetchall()
        return {str(k): int(v or 0) for k, v in rows}

    by_label = {
        TICKER_REGIME_TREND_UP: 0,
        TICKER_REGIME_TREND_DOWN: 0,
        TICKER_REGIME_MEAN_REVERT: 0,
        TICKER_REGIME_CHOPPY: 0,
        TICKER_REGIME_NEUTRAL: 0,
    }
    for k, v in _count_by("ticker_regime_label").items():
        if k in by_label:
            by_label[k] = int(v)

    means = db.execute(text("""
        SELECT AVG(coverage_score), AVG(trend_score), AVG(mean_revert_score)
          FROM trading_ticker_regime_snapshots
         WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).fetchone()
    mean_cov = float(means[0] or 0.0) if means else 0.0
    mean_trend = float(means[1] or 0.0) if means else 0.0
    mean_mr = float(means[2] or 0.0) if means else 0.0

    latest_rows = db.execute(text("""
        WITH ranked AS (
            SELECT id, snapshot_id, as_of_date, ticker, asset_class,
                   last_close, sigma_20d, ac1, vr_5, vr_20, hurst, adx_proxy,
                   trend_score, mean_revert_score,
                   ticker_regime_numeric, ticker_regime_label,
                   bars_used, bars_missing, coverage_score,
                   mode, computed_at, observed_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY ticker ORDER BY computed_at DESC
                   ) AS rn
              FROM trading_ticker_regime_snapshots
             WHERE computed_at >= (NOW() - (:ld || ' days')::INTERVAL)
        )
        SELECT id, snapshot_id, as_of_date, ticker, asset_class,
               last_close, sigma_20d, ac1, vr_5, vr_20, hurst, adx_proxy,
               trend_score, mean_revert_score,
               ticker_regime_numeric, ticker_regime_label,
               bars_used, bars_missing, coverage_score,
               mode, computed_at, observed_at
          FROM ranked
         WHERE rn = 1
         ORDER BY computed_at DESC
         LIMIT :lim
    """), {
        "ld": int(lookback_days),
        "lim": int(max(1, min(latest_tickers_limit, 500))),
    }).fetchall()
    latest_tickers = [_row_to_dict(r) for r in latest_rows]

    return {
        "mode": mode,
        "lookback_days": int(lookback_days),
        "snapshots_total": int(total),
        "distinct_tickers": int(distinct_n),
        "by_ticker_regime_label": by_label,
        "by_asset_class": _count_by("asset_class"),
        "mean_coverage_score": round(mean_cov, 6),
        "mean_trend_score": round(mean_trend, 6),
        "mean_mean_revert_score": round(mean_mr, 6),
        "latest_tickers": latest_tickers,
    }


__all__ = [
    "TickerRegimeRow",
    "TickerRegimeSweepResult",
    "_effective_mode",
    "mode_is_active",
    "mode_is_authoritative",
    "compute_and_persist_sweep",
    "get_latest_snapshot_for_ticker",
    "ticker_regime_summary",
]
