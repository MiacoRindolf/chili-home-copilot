"""Setup vitals: indicator trajectories, zones, and composite health from snapshot history.

Primary path: last N ``MarketSnapshot`` rows (indicator_data JSON) — no provider OHLCV.
Fallback: ``fetch_ohlcv_df`` + ``indicator_core.compute_all_from_df`` when history is thin.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
from sqlalchemy import desc
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SNAPSHOT_LOOKBACK = 10  # marker-q2j
STALE_SECONDS_DEFAULT = 7200
MIN_SNAPS_FOR_TRAJECTORY = 3

# Default RSI overbought threshold; can be overridden via the
# StrategyParameter registry (family="setup_vitals", key="rsi_overbought").
# Q2 Task J — adaptive threshold. Reads through get_parameter so the
# learner can nudge it when realized outcomes show 70 is too tight or
# too loose for the current regime. The 95.0 ceiling and 50.0 floor
# guard against the learner pushing RSI into nonsense territory.
_DEFAULT_RSI_OVERBOUGHT = 70.0
_RSI_OVERBOUGHT_BOUNDS = (50.0, 95.0)


@dataclass
class SetupVitals:
    momentum_score: float = 0.0
    volume_score: float = 0.0
    trend_score: float = 0.0
    overextension_risk: float = 0.0
    composite_health: float = 0.5
    divergences: list[dict[str, Any]] = field(default_factory=list)
    trajectory_details: dict[str, Any] = field(default_factory=dict)
    degradation_signals: list[str] = field(default_factory=list)
    source: str = "snapshots"  # snapshots | ohlcv

    def to_dict(self) -> dict[str, Any]:
        return {
            "momentum_score": round(self.momentum_score, 4),
            "volume_score": round(self.volume_score, 4),
            "trend_score": round(self.trend_score, 4),
            "overextension_risk": round(self.overextension_risk, 4),
            "composite_health": round(self.composite_health, 4),
            "divergences": self.divergences,
            "trajectory_details": self.trajectory_details,
            "degradation_signals": self.degradation_signals,
            "source": self.source,
        }


def _safe_float(x: Any) -> float | None:
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_indicator_data(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def _flat_from_snap(ind_data: dict[str, Any], close_price: float) -> dict[str, Any]:
    from .learning_predictions import _indicator_data_to_flat_snapshot

    return _indicator_data_to_flat_snapshot(ind_data, close_price)


def _normalized_slope(values: list[float | None]) -> float:
    """Linear slope of last segment, normalized to roughly -1..1."""
    ys = [v for v in values if v is not None and not math.isnan(v)]
    if len(ys) < 2:
        return 0.0
    arr = np.array(ys, dtype=float)
    x = np.arange(len(arr), dtype=float)
    if np.std(arr) < 1e-12:
        return 0.0
    try:
        slope, _ = np.polyfit(x, arr, 1)
    except (np.linalg.LinAlgError, ValueError):
        return 0.0
    scale = max(np.std(arr), 1e-6)
    return float(np.clip(slope * len(arr) / (scale * max(len(arr), 1)), -1.0, 1.0))


def _resolve_rsi_overbought(db: Optional[Session]) -> float:
    """Resolve adaptive RSI overbought threshold via StrategyParameter, default 70.

    Best-effort; on any failure returns the static default so the vitals
    pipeline never breaks because of a config-table read miss.
    """
    if db is None:
        return _DEFAULT_RSI_OVERBOUGHT
    try:
        from .strategy_parameter import (
            ParameterSpec, get_parameter, register_parameter,
        )
        register_parameter(
            db,
            ParameterSpec(
                strategy_family="setup_vitals",
                parameter_key="rsi_overbought",
                initial_value=_DEFAULT_RSI_OVERBOUGHT,
                min_value=_RSI_OVERBOUGHT_BOUNDS[0],
                max_value=_RSI_OVERBOUGHT_BOUNDS[1],
                description=(
                    "RSI(14) threshold above which a setup is flagged as "
                    "overbought / overextension-prone. Adapts when realized "
                    "outcomes show 70 is mis-tuned for the current regime."
                ),
            ),
        )
        v = get_parameter(
            db, "setup_vitals", "rsi_overbought",
            default=_DEFAULT_RSI_OVERBOUGHT,
        )
        if v is None:
            return _DEFAULT_RSI_OVERBOUGHT
        # Defensive clamp — never trust the DB to obey its own bounds.
        return float(max(_RSI_OVERBOUGHT_BOUNDS[0],
                         min(_RSI_OVERBOUGHT_BOUNDS[1], v)))
    except Exception:
        return _DEFAULT_RSI_OVERBOUGHT


def _rsi_zone(rsi: float | None, overbought: float = _DEFAULT_RSI_OVERBOUGHT) -> str:
    if rsi is None:
        return "unknown"
    if rsi >= overbought:
        return "overbought"
    if rsi <= 30:
        return "oversold"
    return "neutral"


def _compute_vitals_from_flats(
    flats: list[dict[str, Any]],
    *,
    source: str,
    db: Optional[Session] = None,
) -> SetupVitals:
    """Derive scores from ordered list of flat indicator dicts (oldest -> newest)."""
    if not flats:
        return SetupVitals(source=source)

    last = flats[-1]
    rsi_series = [_safe_float(f.get("rsi_14")) for f in flats]
    macd_h = [_safe_float(f.get("macd_hist")) for f in flats]
    obv = [_safe_float(f.get("obv")) for f in flats]
    stoch_k = [_safe_float(f.get("stoch_k")) for f in flats]
    price = [_safe_float(f.get("price")) for f in flats]
    ema20 = [_safe_float(f.get("ema_20")) for f in flats]
    ema50 = [_safe_float(f.get("ema_50")) for f in flats]

    rsi_slope = _normalized_slope(rsi_series)
    macd_slope = _normalized_slope(macd_h)
    stoch_slope = _normalized_slope(stoch_k)

    # Momentum: blend oscillator slopes (long bias: falling RSI from OB can be caution)
    momentum_score = float(np.clip((rsi_slope + macd_slope + stoch_slope) / 3.0, -1.0, 1.0))

    # Volume: OBV slope + last vol z if present
    obv_slope = _normalized_slope(obv)
    vz = _safe_float(last.get("vol_z_20"))
    vz_norm = 0.0
    if vz is not None:
        vz_norm = float(np.sign(vz) * min(abs(vz) / 3.0, 1.0))
    volume_score = float(np.clip(obv_slope * 0.7 + vz_norm * 0.3, -1.0, 1.0))

    # Trend: EMA stack + price vs ema20
    e20 = _safe_float(last.get("ema_20"))
    e50 = _safe_float(last.get("ema_50"))
    px = _safe_float(last.get("price"))
    trend_score = 0.0
    if e20 and e50:
        trend_score += 0.4 if e20 > e50 else -0.4
    if px and e20:
        trend_score += 0.35 if px > e20 else -0.35
    trend_score = float(np.clip(trend_score + _normalized_slope(ema20) * 0.25, -1.0, 1.0))

    rsi_last = _safe_float(last.get("rsi_14"))
    bb_pb = _safe_float(last.get("bb_pct_b"))
    overextension_risk = 0.0
    if rsi_last is not None:
        if rsi_last >= 75:
            overextension_risk += 0.45 + min((rsi_last - 75) / 25.0, 1.0) * 0.35
        elif rsi_last >= 65:
            overextension_risk += 0.2
    if bb_pb is not None and bb_pb > 0.95:
        overextension_risk += 0.2
    overextension_risk = float(np.clip(overextension_risk, 0.0, 1.0))

    # Simple divergence: price making higher high last 5 vs RSI lower high
    divergences: list[dict[str, Any]] = []
    if len(flats) >= 5:
        ps = [p for p in price[-5:] if p is not None]
        rs = [r for r in rsi_series[-5:] if r is not None]
        if len(ps) >= 3 and len(rs) >= 3:
            if ps[-1] > ps[0] and rs[-1] < rs[0] - 2:
                divergences.append({"type": "bearish", "pair": "price_rsi", "note": "price up, RSI weaker"})
            elif ps[-1] < ps[0] and rs[-1] > rs[0] + 2:
                divergences.append({"type": "bullish", "pair": "price_rsi", "note": "price down, RSI stronger"})

    rsi_overbought_threshold = _resolve_rsi_overbought(db)
    trajectory_details: dict[str, Any] = {
        "rsi_14": {
            "slope": rsi_slope,
            "zone": _rsi_zone(rsi_last, overbought=rsi_overbought_threshold),
            "direction": "falling" if rsi_slope < -0.15 else "rising" if rsi_slope > 0.15 else "flat",
            "overbought_threshold": rsi_overbought_threshold,
        },
        "macd_hist": {
            "slope": macd_slope,
            "direction": "falling" if macd_slope < -0.15 else "rising" if macd_slope > 0.15 else "flat",
        },
        "obv": {
            "slope": obv_slope,
            "direction": "falling" if obv_slope < -0.15 else "rising" if obv_slope > 0.15 else "flat",
        },
        "stoch_k": {
            "slope": stoch_slope,
            "direction": "falling" if stoch_slope < -0.15 else "rising" if stoch_slope > 0.15 else "flat",
        },
    }

    degradation_signals: list[str] = []
    if rsi_last and rsi_last > rsi_overbought_threshold and rsi_slope < -0.1:
        degradation_signals.append("rsi_falling_from_overbought")
    if macd_slope < -0.2 and momentum_score < 0:
        degradation_signals.append("macd_hist_weakening")
    if obv_slope < -0.15 and volume_score < 0:
        degradation_signals.append("obv_distribution")

    w_m, w_v, w_t, w_o = 0.35, 0.25, 0.25, 0.15
    composite = (
        w_m * (0.5 + 0.5 * momentum_score)
        + w_v * (0.5 + 0.5 * volume_score)
        + w_t * (0.5 + 0.5 * trend_score)
        + w_o * (1.0 - overextension_risk)
    )
    composite_health = float(np.clip(composite, 0.0, 1.0))

    return SetupVitals(
        momentum_score=momentum_score,
        volume_score=volume_score,
        trend_score=trend_score,
        overextension_risk=overextension_risk,
        composite_health=composite_health,
        divergences=divergences,
        trajectory_details=trajectory_details,
        degradation_signals=degradation_signals,
        source=source,
    )


def load_snapshot_flats_chronological(
    db: Session,
    ticker: str,
    bar_interval: str,
    *,
    limit: int = SNAPSHOT_LOOKBACK,
) -> list[dict[str, Any]]:
    """Load flat indicator dicts from recent MarketSnapshot rows (oldest first)."""
    from ...models.trading import MarketSnapshot

    rows = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.ticker == ticker)
        .filter(MarketSnapshot.bar_interval == bar_interval)
        .filter(MarketSnapshot.indicator_data.isnot(None))
        .order_by(desc(MarketSnapshot.snapshot_date))
        .limit(limit)
        .all()
    )
    if not rows:
        return []
    rows.reverse()
    out: list[dict[str, Any]] = []
    for s in rows:
        ind = _parse_indicator_data(s.indicator_data)
        cp = float(s.close_price or 0) or 0.0
        out.append(_flat_from_snap(ind, cp))
    return out


def compute_vitals_from_ohlcv(
    ticker: str,
    bar_interval: str,
    *,
    db: Optional[Session] = None,
) -> SetupVitals:
    """Fallback: full OHLCV + indicator_core arrays."""
    from .market_data import fetch_ohlcv_df
    from .indicator_core import compute_all_from_df

    period = "3mo" if bar_interval == "1d" else "60d"
    df = fetch_ohlcv_df(ticker, period=period, interval=bar_interval)
    if df is None or len(df) < 20:
        return SetupVitals(source="ohlcv")

    needed = {
        "rsi_14",
        "macd_histogram",
        "ema_20",
        "ema_50",
        "stochastic_k",
        "obv",
        "bb_pct",
        "price",
    }
    bundle = compute_all_from_df(df, needed=needed)
    n = min(15, len(df))
    flats: list[dict[str, Any]] = []
    idxs = list(range(len(df) - n, len(df)))
    for i in idxs:
        px = _safe_float(bundle.get("price", [None] * len(df))[i]) if "price" in bundle else _safe_float(
            float(df["Close"].iloc[i])
        )
        bbp = _safe_float(bundle["bb_pct"][i]) if "bb_pct" in bundle else None
        flat = {
            "price": px,
            "rsi_14": _safe_float(bundle["rsi_14"][i]) if "rsi_14" in bundle else None,
            "macd_hist": _safe_float(bundle["macd_histogram"][i]) if "macd_histogram" in bundle else None,
            "ema_20": _safe_float(bundle["ema_20"][i]) if "ema_20" in bundle else None,
            "ema_50": _safe_float(bundle["ema_50"][i]) if "ema_50" in bundle else None,
            "stoch_k": _safe_float(bundle["stochastic_k"][i]) if "stochastic_k" in bundle else None,
            "obv": _safe_float(bundle["obv"][i]) if "obv" in bundle else None,
            "bb_pct_b": bbp,
        }
        flats.append(flat)
    return _compute_vitals_from_flats(flats, source="ohlcv", db=db)


def compute_setup_vitals(db: Session, ticker: str, bar_interval: str) -> SetupVitals:
    """Compute vitals: prefer snapshot history; fallback to OHLCV."""
    flats = load_snapshot_flats_chronological(db, ticker, bar_interval, limit=SNAPSHOT_LOOKBACK)
    if len(flats) >= MIN_SNAPS_FOR_TRAJECTORY:
        return _compute_vitals_from_flats(flats, source="snapshots", db=db)
    return compute_vitals_from_ohlcv(ticker, bar_interval, db=db)


def merge_direction_into_flat(flat: dict[str, Any], vitals: SetupVitals) -> dict[str, Any]:
    """Inject ``{indicator}_direction`` keys for trade-plan monitoring_signal evaluation."""
    merged = dict(flat)
    td = vitals.trajectory_details or {}
    for key, meta in td.items():
        if isinstance(meta, dict) and "direction" in meta:
            merged[f"{key}_direction"] = meta["direction"]
    # Common aliases
    if "rsi_14" in td and "direction" in td["rsi_14"]:
        merged["rsi_direction"] = td["rsi_14"]["direction"]
    return merged


def _ticker_vitals_row_to_setup(row: Any) -> SetupVitals:
    tj = row.trajectory_json or {}
    return SetupVitals(
        momentum_score=float(row.momentum_score or 0),
        volume_score=float(row.volume_score or 0),
        trend_score=float(row.trend_score or 0),
        overextension_risk=float(row.overextension_risk or 0),
        composite_health=float(row.composite_health or 0.5),
        divergences=list(row.divergences_json or []),
        trajectory_details=dict(tj) if isinstance(tj, dict) else {},
        degradation_signals=[],
        source="cache",
    )


def get_or_compute_ticker_vitals(
    db: Session,
    ticker: str,
    bar_interval: str,
    *,
    max_age_seconds: float = STALE_SECONDS_DEFAULT,
    force_refresh: bool = False,
) -> SetupVitals:
    """Return cached TickerVitals row if fresh; else compute and upsert."""
    from ...models.trading import TickerVitals

    now = datetime.utcnow()
    if not force_refresh:
        row = (
            db.query(TickerVitals)
            .filter(TickerVitals.ticker == ticker, TickerVitals.bar_interval == bar_interval)
            .first()
        )
        if row and row.computed_at and (now - row.computed_at).total_seconds() < max_age_seconds:
            return _ticker_vitals_row_to_setup(row)

    vitals = compute_setup_vitals(db, ticker, bar_interval)
    upsert_ticker_vitals_row(db, ticker, bar_interval, vitals)
    try:
        db.flush()
    except Exception:
        logger.warning("[setup_vitals] upsert flush failed for %s %s", ticker, bar_interval, exc_info=True)
    return vitals


def upsert_ticker_vitals_row(db: Session, ticker: str, bar_interval: str, vitals: SetupVitals) -> None:
    from ...models.trading import TickerVitals

    row = (
        db.query(TickerVitals)
        .filter(TickerVitals.ticker == ticker, TickerVitals.bar_interval == bar_interval)
        .first()
    )
    payload = {
        "momentum_score": vitals.momentum_score,
        "volume_score": vitals.volume_score,
        "trend_score": vitals.trend_score,
        "overextension_risk": vitals.overextension_risk,
        "composite_health": vitals.composite_health,
        "trajectory_json": vitals.trajectory_details,
        "divergences_json": vitals.divergences,
        "computed_at": datetime.utcnow(),
    }
    if row:
        for k, v in payload.items():
            setattr(row, k, v)
    else:
        db.add(
            TickerVitals(
                ticker=ticker,
                bar_interval=bar_interval,
                **{k: v for k, v in payload.items()},
            )
        )


def refresh_ticker_vitals_batch(
    db: Session,
    tickers: list[str],
    bar_interval: str,
) -> dict[str, Any]:
    """Compute and upsert vitals for each ticker (e.g. after market snapshots)."""
    ok = 0
    err = 0
    for t in tickers:
        t = (t or "").strip().upper()
        if not t:
            continue
        try:
            vitals = compute_setup_vitals(db, t, bar_interval)
            upsert_ticker_vitals_row(db, t, bar_interval, vitals)
            ok += 1
        except Exception:
            err += 1
            logger.debug("[setup_vitals] refresh failed for %s", t, exc_info=True)
    try:
        db.commit()
    except Exception:
        db.rollback()
    return {"ok": ok, "errors": err, "interval": bar_interval}


def monitored_tickers_for_vitals(db: Session) -> list[str]:
    """Tickers with open trades or pending breakout alerts worth refreshing."""
    from ...models.trading import BreakoutAlert, Trade

    tickers: set[str] = set()
    try:
        for (t,) in db.query(Trade.ticker).filter(Trade.status == "open").distinct():
            if t:
                tickers.add(str(t).strip().upper())
    except Exception:
        pass
    try:
        q = db.query(BreakoutAlert.ticker).filter(BreakoutAlert.outcome == "pending")
        for (t,) in q.distinct().limit(200):
            if t:
                tickers.add(str(t).strip().upper())
    except Exception:
        pass
    return sorted(tickers)


def record_setup_vitals_history(
    db: Session,
    *,
    trade_id: int | None,
    breakout_alert_id: int | None,
    vitals: SetupVitals,
    price: float,
    degradation_flags: dict[str, Any] | None,
) -> None:
    from ...models.trading import SetupVitalsHistory

    db.add(
        SetupVitalsHistory(
            trade_id=trade_id,
            breakout_alert_id=breakout_alert_id,
            momentum_score=vitals.momentum_score,
            volume_score=vitals.volume_score,
            trend_score=vitals.trend_score,
            overextension_risk=vitals.overextension_risk,
            composite_health=vitals.composite_health,
            price_at_check=price,
            degradation_flags=degradation_flags or {},
        )
    )


def load_recent_vitals_history_for_trade(
    db: Session,
    trade_id: int,
    *,
    limit: int = 12,
) -> list[Any]:
    from ...models.trading import SetupVitalsHistory

    return (
        db.query(SetupVitalsHistory)
        .filter(SetupVitalsHistory.trade_id == trade_id)
        .order_by(desc(SetupVitalsHistory.created_at))
        .limit(limit)
        .all()
    )


def momentum_drop_urgent(db: Session, trade_id: int, current_momentum: float) -> bool:
    """True if momentum_score fell by more than 0.4 vs last recorded check."""
    rows = load_recent_vitals_history_for_trade(db, trade_id, limit=1)
    if not rows:
        return False
    prev = float(rows[0].momentum_score or 0)
    return (prev - current_momentum) > 0.4


def detect_multi_check_degradation(
    db: Session,
    trade_id: int,
    current_momentum: float,
    current_volume: float,
) -> dict[str, Any]:
    """Compare to prior history rows for consecutive deterioration."""
    rows = load_recent_vitals_history_for_trade(db, trade_id, limit=8)
    flags: dict[str, Any] = {"consecutive_momentum_down": 0, "volume_flip_negative": False}
    if len(rows) < 1:
        return flags
    # rows: newest first -> chronological oldest first
    chron = list(reversed(rows))
    streak = 0
    max_streak = 0
    for i in range(1, len(chron)):
        a = float(chron[i - 1].momentum_score or 0)
        b = float(chron[i].momentum_score or 0)
        if b < a - 0.05:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    flags["consecutive_momentum_down"] = max_streak
    if chron:
        last_vol = float(chron[-1].volume_score or 0)
        if last_vol > 0 and current_volume < 0:
            flags["volume_flip_negative"] = True
    if max_streak >= 3:
        flags["degraded_3plus"] = True
    return flags
