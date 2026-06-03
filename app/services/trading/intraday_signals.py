"""Intraday signal pipeline for the trading brain.

Scans for real-time setups across crypto and stocks:
- Pre-market gap detection
- Opening Range Breakout (ORB)
- Crypto breakout signals
- Momentum continuation after pullback

Signals are scored, filtered, and optionally routed to paper trading
or alert dispatch.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_PREMARKET_GAP_TICKER_LIMIT = 50
DEFAULT_ORB_TICKER_LIMIT = 30
DEFAULT_MOMENTUM_STOCK_TICKER_LIMIT = 30
DEFAULT_MOMENTUM_CRYPTO_TICKER_LIMIT = 10
DEFAULT_PREMARKET_MIN_GAP_PCT = 3.0
DEFAULT_ORB_MINUTES = 30
INTRADAY_BAR_MINUTES = 5
PREMARKET_GAP_MAX_SIGNALS = 15
ORB_MAX_SIGNALS = 10
MOMENTUM_MAX_SIGNALS = 10
INTRADAY_AUTO_PAPER_MAX_SIGNALS = 5
INTRADAY_AUTO_PAPER_MIN_CONFIDENCE = 0.60
INTRADAY_CONFIDENCE_MIN = 0.0
INTRADAY_CONFIDENCE_MAX = 0.95
INTRADAY_CONFIDENCE_EPSILON = 1e-9
PREMARKET_GAP_CONFIDENCE_BASE = 0.56
PREMARKET_GAP_CONFIDENCE_SCALE = 0.07
PREMARKET_GAP_CONFIDENCE_RATIO_CAP = 5.0
ORB_CONFIDENCE_BASE = 0.54
ORB_CONFIDENCE_SCALE = 0.07
ORB_CONFIDENCE_RATIO_CAP = 5.0
ORB_CONFIDENCE_REFERENCE_BREAKOUT_PCT = 1.0
MOMENTUM_CONFIDENCE_BASE = 0.54
MOMENTUM_CONFIDENCE_MOMENTUM_SCALE = 0.04
MOMENTUM_CONFIDENCE_RVOL_SCALE = 0.03
MOMENTUM_CONFIDENCE_PULLBACK_SCALE = 0.02
MOMENTUM_CONFIDENCE_MOMENTUM_CAP = 6.0
MOMENTUM_CONFIDENCE_RVOL_CAP = 5.0
MOMENTUM_PULLBACK_TARGET_PCT = 1.0
SIGNAL_TYPE_AUTO_PAPER_PRIORITY = {
    "momentum_continuation": 3,
    "orb_breakout": 2,
    "orb_breakdown": 2,
    "premarket_gap": 1,
}


def _is_crypto_ticker(ticker: str) -> bool:
    return str(ticker or "").upper().endswith("-USD")


def _bounded_confidence(value: float) -> float:
    return round(
        max(INTRADAY_CONFIDENCE_MIN, min(INTRADAY_CONFIDENCE_MAX, float(value))),
        3,
    )


def _score_premarket_gap_confidence(
    *, gap_pct: float, min_gap_pct: float,
) -> float:
    ratio = min(
        PREMARKET_GAP_CONFIDENCE_RATIO_CAP,
        abs(float(gap_pct)) / max(
            abs(float(min_gap_pct)),
            INTRADAY_CONFIDENCE_EPSILON,
        ),
    )
    return _bounded_confidence(
        PREMARKET_GAP_CONFIDENCE_BASE
        + PREMARKET_GAP_CONFIDENCE_SCALE * ratio
    )


def _score_orb_confidence(*, breakout_pct: float) -> float:
    ratio = min(
        ORB_CONFIDENCE_RATIO_CAP,
        abs(float(breakout_pct)) / ORB_CONFIDENCE_REFERENCE_BREAKOUT_PCT,
    )
    return _bounded_confidence(ORB_CONFIDENCE_BASE + ORB_CONFIDENCE_SCALE * ratio)


def _score_momentum_confidence(
    *,
    momentum_pct: float,
    rvol: float,
    rvol_min: float,
    pullback_pct: float,
) -> float:
    momentum_score = min(MOMENTUM_CONFIDENCE_MOMENTUM_CAP, max(0.0, momentum_pct))
    rvol_score = min(
        MOMENTUM_CONFIDENCE_RVOL_CAP,
        max(
            0.0,
            float(rvol)
            / max(float(rvol_min), INTRADAY_CONFIDENCE_EPSILON),
        ),
    )
    pullback_score = max(
        0.0,
        MOMENTUM_PULLBACK_TARGET_PCT - max(0.0, float(pullback_pct)),
    )
    return _bounded_confidence(
        MOMENTUM_CONFIDENCE_BASE
        + MOMENTUM_CONFIDENCE_MOMENTUM_SCALE * momentum_score
        + MOMENTUM_CONFIDENCE_RVOL_SCALE * rvol_score
        + MOMENTUM_CONFIDENCE_PULLBACK_SCALE * pullback_score
    )


def _stock_auto_paper_session_open() -> bool:
    try:
        from .pattern_imminent_alerts import (
            us_stock_extended_session_open,
            us_stock_session_open,
        )

        return bool(us_stock_session_open() or us_stock_extended_session_open())
    except Exception:
        logger.debug("[intraday] stock session check failed", exc_info=True)
        return False


def _signal_strength(sig: dict[str, Any]) -> float:
    for key in ("momentum_pct", "breakout_pct", "gap_pct"):
        if key not in sig:
            continue
        try:
            return abs(float(sig.get(key) or 0.0))
        except (TypeError, ValueError):
            continue
    return 0.0


def _auto_paper_candidates(
    signals: list[dict[str, Any]],
    *,
    stock_session_open: bool,
    min_confidence: float = INTRADAY_AUTO_PAPER_MIN_CONFIDENCE,
    max_candidates: int = INTRADAY_AUTO_PAPER_MAX_SIGNALS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    skip_reasons: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []

    def _skip(reason: str) -> None:
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    for sig in signals:
        ticker = str(sig.get("ticker") or "").upper()
        try:
            confidence = float(sig.get("confidence"))
        except (TypeError, ValueError):
            _skip("missing_confidence")
            continue
        if confidence < min_confidence:
            _skip("below_confidence_floor")
            continue
        entry = sig.get("entry_price") or sig.get("price")
        try:
            if float(entry) <= 0.0:
                _skip("missing_entry")
                continue
        except (TypeError, ValueError):
            _skip("missing_entry")
            continue
        if ticker and not _is_crypto_ticker(ticker) and not stock_session_open:
            _skip("stock_session_closed")
            continue
        candidates.append(dict(sig))

    candidates.sort(
        key=lambda sig: (
            float(sig.get("confidence") or 0.0),
            SIGNAL_TYPE_AUTO_PAPER_PRIORITY.get(str(sig.get("signal_type")), 0),
            _signal_strength(sig),
        ),
        reverse=True,
    )
    selected = candidates[:max(0, int(max_candidates))]
    return selected, {
        "auto_paper_candidates_considered": len(signals),
        "auto_paper_candidates_eligible": len(candidates),
        "auto_paper_candidates_selected": len(selected),
        "auto_paper_skip_reasons": skip_reasons,
        "auto_paper_min_confidence": min_confidence,
        "auto_paper_max_candidates": max_candidates,
        "equity_paper_session_open": stock_session_open,
    }


def scan_premarket_gaps(
    tickers: list[str] | None = None,
    min_gap_pct: float = DEFAULT_PREMARKET_MIN_GAP_PCT,
) -> list[dict[str, Any]]:
    """Detect pre-market gaps by comparing yesterday's close to current price."""
    from .market_data import fetch_quote, DEFAULT_SCAN_TICKERS

    if tickers is None:
        tickers = list(DEFAULT_SCAN_TICKERS)[:DEFAULT_PREMARKET_GAP_TICKER_LIMIT]

    gaps = []
    for ticker in tickers:
        try:
            q = fetch_quote(ticker)
            if not q:
                continue
            price = q.get("price", 0)
            prev_close = q.get("previous_close") or q.get("regularMarketPreviousClose", 0)
            if not price or not prev_close or prev_close <= 0:
                continue
            gap_pct = (price - prev_close) / prev_close * 100
            if abs(gap_pct) >= min_gap_pct:
                confidence = _score_premarket_gap_confidence(
                    gap_pct=gap_pct,
                    min_gap_pct=min_gap_pct,
                )
                gaps.append({
                    "ticker": ticker,
                    "price": round(price, 2),
                    "prev_close": round(prev_close, 2),
                    "gap_pct": round(gap_pct, 2),
                    "direction": "up" if gap_pct > 0 else "down",
                    "signal_type": "premarket_gap",
                    "confidence": confidence,
                    "confidence_source": "premarket_gap_magnitude",
                })
        except Exception:
            continue

    gaps.sort(key=lambda x: abs(x["gap_pct"]), reverse=True)
    return gaps[:PREMARKET_GAP_MAX_SIGNALS]


def scan_opening_range_breakout(
    tickers: list[str] | None = None,
    orb_minutes: int = DEFAULT_ORB_MINUTES,
) -> list[dict[str, Any]]:
    """Detect Opening Range Breakouts — price breaking above/below the first N minutes' range."""
    from .market_data import fetch_ohlcv_df, DEFAULT_SCAN_TICKERS

    if tickers is None:
        tickers = list(DEFAULT_SCAN_TICKERS)[:DEFAULT_ORB_TICKER_LIMIT]

    signals = []
    for ticker in tickers:
        try:
            df = fetch_ohlcv_df(ticker, period="2d", interval="5m")
            if df.empty or len(df) < 12:
                continue

            today = df.index[-1].date() if hasattr(df.index[-1], "date") else None
            if today is None:
                continue

            today_bars = df[df.index.date == today] if hasattr(df.index, "date") else df.tail(78)
            if len(today_bars) < 6:
                continue

            orb_bars = orb_minutes // INTRADAY_BAR_MINUTES
            opening_range = today_bars.head(orb_bars)
            orb_high = float(opening_range["High"].max())
            orb_low = float(opening_range["Low"].min())
            current = float(today_bars["Close"].iloc[-1])

            if current > orb_high:
                breakout_pct = (current - orb_high) / orb_high * 100
                signals.append({
                    "ticker": ticker,
                    "price": round(current, 2),
                    "orb_high": round(orb_high, 2),
                    "orb_low": round(orb_low, 2),
                    "direction": "long",
                    "breakout_pct": round(breakout_pct, 2),
                    "signal_type": "orb_breakout",
                    "stop_price": round(orb_low, 2),
                    "target_price": round(current + (orb_high - orb_low), 2),
                    "confidence": _score_orb_confidence(
                        breakout_pct=breakout_pct,
                    ),
                    "confidence_source": "opening_range_breakout_magnitude",
                })
            elif current < orb_low:
                breakout_pct = (orb_low - current) / orb_low * 100
                signals.append({
                    "ticker": ticker,
                    "price": round(current, 2),
                    "orb_high": round(orb_high, 2),
                    "orb_low": round(orb_low, 2),
                    "direction": "short",
                    "breakout_pct": round(breakout_pct, 2),
                    "signal_type": "orb_breakdown",
                    "stop_price": round(orb_high, 2),
                    "target_price": round(current - (orb_high - orb_low), 2),
                    "confidence": _score_orb_confidence(
                        breakout_pct=breakout_pct,
                    ),
                    "confidence_source": "opening_range_breakdown_magnitude",
                })
        except Exception:
            continue

    signals.sort(key=lambda x: abs(x.get("breakout_pct", 0)), reverse=True)
    return signals[:ORB_MAX_SIGNALS]


def _resolve_momentum_rvol_min(db: Optional[Session]) -> float:
    """Q2 Task J — adaptive rvol_min for momentum_continuation gate.

    Default 0.8 (current behavior). Bounds [0.3, 5.0] keep the learner
    from pushing the gate into nonsense territory: below 0.3 we're
    accepting setups with no relative-volume confirmation; above 5.0 we
    need a five-bagger volume spike to trade, which would silence the
    scanner entirely.
    """
    default = 0.8
    if db is None:
        return default
    try:
        from .strategy_parameter import (
            ParameterSpec, get_parameter, register_parameter,
        )
        register_parameter(
            db,
            ParameterSpec(
                strategy_family="momentum_continuation",
                parameter_key="rvol_min",
                initial_value=default,
                min_value=0.3,
                max_value=5.0,
                description=(
                    "Minimum relative volume to allow a momentum_continuation "
                    "signal. Pull-back-to-EMA setups without a volume "
                    "confirmation tend to fade; the learner adapts this "
                    "from realized 15m signal outcomes."
                ),
            ),
        )
        v = get_parameter(
            db, "momentum_continuation", "rvol_min", default=default,
        )
        if v is None:
            return default
        return float(max(0.3, min(5.0, v)))
    except Exception:
        return default


def scan_momentum_continuation(
    tickers: list[str] | None = None,
    *,
    db: Optional[Session] = None,
) -> list[dict[str, Any]]:
    """Find stocks/crypto in strong intraday momentum pulling back to EMA support.

    Uses 15-minute bars over the last 5 days so the scan reflects actual
    intraday structure rather than daily candles.
    """
    from .market_data import fetch_ohlcv_df, DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS

    if tickers is None:
        tickers = (
            list(DEFAULT_SCAN_TICKERS)[:DEFAULT_MOMENTUM_STOCK_TICKER_LIMIT]
            + list(DEFAULT_CRYPTO_TICKERS)[:DEFAULT_MOMENTUM_CRYPTO_TICKER_LIMIT]
        )

    rvol_min = _resolve_momentum_rvol_min(db)

    signals = []
    for ticker in tickers:
        try:
            df = fetch_ohlcv_df(ticker, period="5d", interval="15m")
            if df.empty or len(df) < 40:
                continue

            close = df["Close"].astype(float)
            volume = df["Volume"].astype(float)
            ema9 = close.ewm(span=9, adjust=False).mean()
            ema21 = close.ewm(span=21, adjust=False).mean()

            last_close = float(close.iloc[-1])
            last_ema9 = float(ema9.iloc[-1])
            last_ema21 = float(ema21.iloc[-1])

            if last_ema9 > last_ema21:
                pullback_to_ema9 = abs(last_close - last_ema9) / last_close * 100
                if pullback_to_ema9 < 1.0 and last_close > last_ema21:
                    # Recent 20-bar momentum (5 hours on 15m)
                    bars_back = min(20, len(close) - 1)
                    mom_pct = (last_close - float(close.iloc[-bars_back])) / float(close.iloc[-bars_back]) * 100

                    # Volume confirmation: recent volume vs prior average
                    recent_vol = float(volume.iloc[-10:].mean()) if len(volume) >= 10 else 0
                    prior_vol = float(volume.iloc[-40:-10].mean()) if len(volume) >= 40 else recent_vol
                    rvol = round(recent_vol / prior_vol, 2) if prior_vol > 0 else 1.0

                    if mom_pct > 1.0 and rvol >= rvol_min:
                        signals.append({
                            "ticker": ticker,
                            "price": round(last_close, 4 if ticker.endswith("-USD") else 2),
                            "ema9": round(last_ema9, 4 if ticker.endswith("-USD") else 2),
                            "ema21": round(last_ema21, 4 if ticker.endswith("-USD") else 2),
                            "pullback_pct": round(pullback_to_ema9, 2),
                            "momentum_pct": round(mom_pct, 2),
                            "rvol": rvol,
                            "rvol_min_gate": rvol_min,
                            "direction": "long",
                            "signal_type": "momentum_continuation",
                            "timeframe": "15m",
                            "stop_price": round(last_ema21 * 0.995, 4 if ticker.endswith("-USD") else 2),
                            "target_price": round(last_close * 1.03, 4 if ticker.endswith("-USD") else 2),
                            "confidence": _score_momentum_confidence(
                                momentum_pct=mom_pct,
                                rvol=rvol,
                                rvol_min=rvol_min,
                                pullback_pct=pullback_to_ema9,
                            ),
                            "confidence_source": "momentum_rvol_pullback",
                        })
        except Exception:
            continue

    signals.sort(key=lambda x: x.get("momentum_pct", 0), reverse=True)
    return signals[:MOMENTUM_MAX_SIGNALS]


def run_intraday_signal_sweep(
    db: Session,
    user_id: int | None = None,
    *,
    auto_paper: bool = False,
) -> dict[str, Any]:
    """Run all intraday signal scanners and optionally paper-trade the best ones."""
    results: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z")
    }

    try:
        gaps = scan_premarket_gaps()
        results["premarket_gaps"] = gaps
    except Exception as e:
        logger.warning("[intraday] Gap scan failed: %s", e)
        results["premarket_gaps"] = []

    try:
        orbs = scan_opening_range_breakout()
        results["orb_signals"] = orbs
    except Exception as e:
        logger.warning("[intraday] ORB scan failed: %s", e)
        results["orb_signals"] = []

    try:
        momentum = scan_momentum_continuation(db=db)
        results["momentum_signals"] = momentum
    except Exception as e:
        logger.warning("[intraday] Momentum scan failed: %s", e)
        results["momentum_signals"] = []

    all_signals = results.get("premarket_gaps", []) + results.get("orb_signals", []) + results.get("momentum_signals", [])
    results["total_signals"] = len(all_signals)
    results["auto_paper_requested"] = bool(auto_paper)
    results["paper_entered"] = 0

    if auto_paper and all_signals:
        stock_session_open = _stock_auto_paper_session_open()
        paper_candidates, paper_diag = _auto_paper_candidates(
            all_signals,
            stock_session_open=stock_session_open,
        )
        results.update(paper_diag)
        if paper_candidates:
            try:
                from .paper_trading import auto_enter_from_signals_detailed
                entry_diag = auto_enter_from_signals_detailed(
                    db,
                    user_id,
                    paper_candidates,
                )
                results["paper_entered"] = int(entry_diag.get("entered") or 0)
                results["auto_paper_entry_attempted"] = int(
                    entry_diag.get("attempted") or 0
                )
                results["auto_paper_entry_blocked"] = int(
                    entry_diag.get("blocked") or 0
                )
                results["auto_paper_entry_block_reasons"] = dict(
                    entry_diag.get("block_reasons") or {}
                )
                results["auto_paper_entered_signals"] = list(
                    entry_diag.get("entered_signals") or []
                )
                results["auto_paper_blocked_signals"] = list(
                    entry_diag.get("blocked_signals") or []
                )[:10]
            except Exception as e:
                logger.warning("[intraday] Paper entry failed: %s", e)
                results["auto_paper_error"] = str(e)
    elif auto_paper:
        results.update({
            "auto_paper_candidates_considered": 0,
            "auto_paper_candidates_eligible": 0,
            "auto_paper_candidates_selected": 0,
            "auto_paper_skip_reasons": {},
            "auto_paper_min_confidence": INTRADAY_AUTO_PAPER_MIN_CONFIDENCE,
            "auto_paper_max_candidates": INTRADAY_AUTO_PAPER_MAX_SIGNALS,
            "equity_paper_session_open": _stock_auto_paper_session_open(),
        })

    results["ok"] = True
    return results
