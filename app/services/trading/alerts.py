"""Alert engine: price monitoring, strategy proposals, and alert dispatch.

Monitors open positions, breakout candidates, and AI Brain predictions to
send SMS alerts and generate strategy proposals for user review.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Throttle / dedup / quality tracking ───────────────────────────────

_recent_alerts: dict[str, float] = {}
_THROTTLE_WINDOW_SECS = 300  # 5 min minimum between same-type alerts for same ticker
_MAX_ALERTS_PER_HOUR = 20
_hourly_count = 0
_hourly_reset_at = 0.0

# Alert tiers: A = high confidence / promoted pattern, B = standard, C = speculative
TIER_A = "A"
TIER_B = "B"
TIER_C = "C"


def _alert_dedup_key(alert_type: str, ticker: str | None) -> str:
    return f"{alert_type}:{(ticker or '').upper()}"


def _check_throttle(alert_type: str, ticker: str | None) -> tuple[bool, str]:
    """Return (allowed, reason). Enforces per-ticker dedup and hourly cap."""
    import time
    global _hourly_count, _hourly_reset_at

    now = time.time()
    if now > _hourly_reset_at:
        _hourly_count = 0
        _hourly_reset_at = now + 3600

    if _hourly_count >= _MAX_ALERTS_PER_HOUR:
        return False, f"Hourly cap ({_MAX_ALERTS_PER_HOUR}) reached"

    key = _alert_dedup_key(alert_type, ticker)
    last = _recent_alerts.get(key, 0)
    if now - last < _THROTTLE_WINDOW_SECS:
        return False, f"Duplicate suppressed ({key}, {int(now - last)}s ago)"

    return True, "ok"


def _record_alert_sent(alert_type: str, ticker: str | None) -> None:
    import time
    global _hourly_count
    key = _alert_dedup_key(alert_type, ticker)
    _recent_alerts[key] = time.time()
    _hourly_count += 1
    if len(_recent_alerts) > 500:
        cutoff = time.time() - _THROTTLE_WINDOW_SECS * 2
        _recent_alerts.clear()


def classify_alert_tier(
    alert_type: str,
    scan_pattern_id: int | None = None,
    confidence: float = 0.0,
) -> str:
    """Classify alert into A/B/C tiers based on signal source and confidence."""
    if alert_type in (TARGET_HIT, STOP_HIT, POSITION_CLOSED, POSITION_OPENED):
        return TIER_A
    if scan_pattern_id and confidence >= 0.7:
        return TIER_A
    if scan_pattern_id or confidence >= 0.5:
        return TIER_B
    return TIER_C


# Alert type constants
BREAKOUT_TRIGGERED = "breakout_triggered"
CRYPTO_BREAKOUT = "crypto_breakout"
CRYPTO_SQUEEZE_FIRING = "crypto_squeeze_firing"
TARGET_HIT = "target_hit"
STOP_HIT = "stop_hit"
NEW_TOP_PICK = "new_top_pick"
POSITION_OPENED = "position_opened"
POSITION_CLOSED = "position_closed"
STRATEGY_PROPOSED = "strategy_proposed"
WEEKLY_REVIEW = "weekly_review"
PATTERN_BREAKOUT_IMMINENT = "pattern_breakout_imminent"

ALL_ALERT_TYPES = [
    BREAKOUT_TRIGGERED, CRYPTO_BREAKOUT, CRYPTO_SQUEEZE_FIRING,
    TARGET_HIT, STOP_HIT, NEW_TOP_PICK,
    POSITION_OPENED, POSITION_CLOSED, STRATEGY_PROPOSED, WEEKLY_REVIEW,
    PATTERN_BREAKOUT_IMMINENT,
]

_PATTERN_KW = [
    "macd_bullish", "macd_positive", "macd_negative",
    "ema_stack", "ema_stacking", "rsi_oversold", "rsi_overbought",
    "bollinger", "bb_squeeze", "volume_surge", "pullback",
    "breakout", "gap_up", "gap_down", "stoch_oversold", "stoch_overbought",
    "adx_strong", "float_micro", "float_low", "topping_tail",
    "vwap_above", "momentum", "reversal", "divergence",
]


def _extract_pattern_keywords(signals: list[str]) -> list[str]:
    """Extract canonical pattern tags from free-form signal descriptions."""
    found: list[str] = []
    combined = " ".join(s.lower() for s in signals)
    for kw in _PATTERN_KW:
        readable = kw.replace("_", " ")
        if readable in combined or kw in combined:
            found.append(kw)
    if not found and signals:
        found.append("unclassified")
    return found[:10]

_PROPOSAL_EXPIRE_HOURS = 24  # operational, not scoring


def _get_brain_weight(key: str) -> float:
    """Read a scoring weight from the brain's adaptive weight system."""
    from .scanner import get_adaptive_weight
    return get_adaptive_weight(key)

_SPECULATIVE_KEYWORDS = [
    "float_micro", "float_low", "micro", "microcap", "penny",
    "high risk", "high volatility", "leveraged",
]

# Align with ``scanner.py`` adaptive-weight defaults; used by proposal fallback math
# and ``tests/test_position_sizing.py``.
_MAX_RISK_PCT = 2.0
_POS_PCT_HARD_CAP = 10.0
_POS_PCT_RISK_OFF_CAP = 7.0
_POS_PCT_SPECULATIVE_CAP = 5.0


def _compute_position_size(
    price: float,
    stop: float,
    buying_power: float,
    pick: dict[str, Any],
) -> tuple[int | None, float | None]:
    """Risk-based position sizing with regime, volatility, and instrument overlays.

    Returns (quantity, position_size_pct).  Both are None when buying_power
    is zero or inputs are invalid.
    """
    risk_per_share = abs(price - stop)
    if buying_power <= 0 or risk_per_share <= 0 or price <= 0:
        return None, None

    risk_dollars = buying_power * (_get_brain_weight("pos_max_risk_pct") / 100)
    raw_shares = risk_dollars / risk_per_share
    raw_pct = (raw_shares * price) / buying_power * 100

    # ── 1. Market-regime overlay ──────────────────────────────────────
    regime_label = "cautious"
    vix_regime = "normal"
    try:
        from .market_data import get_market_regime
        _mr = get_market_regime()
        regime_label = _mr.get("regime", "cautious")
        vix_regime = _mr.get("vix_regime", "normal")
    except Exception:
        pass

    regime_mult = 1.0
    if regime_label == "risk_off":
        regime_mult = _get_brain_weight("pos_regime_risk_off_mult")
    elif regime_label == "cautious":
        regime_mult = _get_brain_weight("pos_regime_cautious_mult")

    if vix_regime == "elevated":
        regime_mult *= _get_brain_weight("pos_vix_elevated_mult")
    elif vix_regime == "extreme":
        regime_mult *= _get_brain_weight("pos_vix_extreme_mult")

    # ── 2. Volatility overlay (wide stop -> more volatile) ────────────
    stop_dist_pct = risk_per_share / price * 100
    vol_mult = 1.0
    if stop_dist_pct > 10:
        vol_mult = _get_brain_weight("pos_vol_stop_10_mult")
    elif stop_dist_pct > 8:
        vol_mult = _get_brain_weight("pos_vol_stop_8_mult")
    elif stop_dist_pct > 5:
        vol_mult = _get_brain_weight("pos_vol_stop_5_mult")

    # ── 3. Instrument-quality / speculative overlay ───────────────────
    signals_text = " ".join(s.lower() for s in pick.get("signals", []))
    risk_level = (pick.get("risk_level") or "").lower()

    is_speculative = risk_level in ("high", "very_high") or pick.get("is_crypto", False)
    if not is_speculative:
        for kw in _SPECULATIVE_KEYWORDS:
            if kw in signals_text:
                is_speculative = True
                break

    spec_mult = _get_brain_weight("pos_speculative_mult") if is_speculative else 1.0

    # ── 4. Apply multiplicative overlays ──────────────────────────────
    adjusted_pct = raw_pct * regime_mult * vol_mult * spec_mult

    # ── 5. Scanner/brain soft cap -- stay near their suggestion ───────
    pick_pct = pick.get("position_size_pct")
    if pick_pct and pick_pct > 0:
        adjusted_pct = min(adjusted_pct, pick_pct * _get_brain_weight("pos_scanner_cap_mult"))

    # ── 6. Hard caps (regime-aware) ───────────────────────────────────
    cap = _get_brain_weight("pos_pct_hard_cap")
    if regime_label == "risk_off" or vix_regime in ("elevated", "extreme"):
        cap = min(cap, _get_brain_weight("pos_pct_risk_off_cap"))
    if is_speculative:
        cap = min(cap, _get_brain_weight("pos_pct_speculative_cap"))

    final_pct = round(min(adjusted_pct, cap), 2)

    position_dollars = buying_power * (final_pct / 100)
    quantity = max(1, int(position_dollars / price))

    return quantity, final_pct


def _expire_proposals_on_stop(
    db: Session,
    user_id: int | None,
    ticker: str,
) -> None:
    """Expire any active proposals for *ticker* after a stop-hit alert."""
    from ...models.trading import StrategyProposal

    try:
        active = (
            db.query(StrategyProposal)
            .filter(
                StrategyProposal.ticker == ticker,
                StrategyProposal.status.in_(["pending", "approved"]),
            )
        )
        if user_id is not None:
            active = active.filter(StrategyProposal.user_id == user_id)
        proposals = active.all()
        for p in proposals:
            p.status = "expired"
            p.thesis = (p.thesis or "") + " [Auto-expired: stop-loss hit]"
        if proposals:
            db.commit()
            logger.info(
                f"[alerts] Auto-expired {len(proposals)} proposal(s) for {ticker} on stop hit"
            )
    except Exception as e:
        logger.warning(f"[alerts] Failed to expire proposals for {ticker}: {e}")


def dispatch_alert(
    db: Session | None = None,
    user_id: int | None = None,
    alert_type: str = "",
    ticker: str | None = None,
    message: str = "",
    *,
    price: float | None = None,
    trade_type: str | None = None,
    duration_estimate: str | None = None,
    scan_pattern_id: int | None = None,
    confidence: float = 0.0,
    skip_throttle: bool = False,
) -> bool:
    """Log an alert to the DB and optionally send via SMS.

    Enforces throttle/dedup unless *skip_throttle* is True.
    Always persists to DB regardless of SMS outcome.  If SMS is not
    configured, the alert is logged with sent_via='log_only' (no
    error-level log — just info).  Only logs a warning when SMS IS
    configured but delivery fails.
    """
    from ..sms_service import is_configured as sms_is_configured, send_sms

    if not skip_throttle:
        allowed, reason = _check_throttle(alert_type, ticker)
        if not allowed:
            logger.debug("[alerts] Throttled: %s", reason)
            return False

    tier = classify_alert_tier(alert_type, scan_pattern_id, confidence)

    own_session = False
    if db is None:
        from ...db import SessionLocal
        db = SessionLocal()
        own_session = True

    sent = False
    sent_via = "log_only"

    try:
        if sms_is_configured() and tier in (TIER_A, TIER_B):
            sent = send_sms(message, tier=tier)
            sent_via = ("twilio" if sent else "sms_failed")
            if sent:
                logger.info(f"[alerts] Sent {alert_type} alert for {ticker}: {message[:80]}")
            else:
                logger.warning(f"[alerts] SMS delivery failed for {alert_type}/{ticker}")
        else:
            logger.info(f"[alerts] Logged {alert_type} for {ticker} (SMS not configured)")

        _record_alert_sent(alert_type, ticker)

        from ...models.trading import AlertHistory
        record = AlertHistory(
            user_id=user_id,
            alert_type=alert_type,
            ticker=ticker,
            message=message,
            trade_type=trade_type,
            duration_estimate=duration_estimate,
            scan_pattern_id=scan_pattern_id,
            sent_via=sent_via,
            success=sent,
        )
        db.add(record)
        db.commit()

        try:
            from ...routers.trading import _broadcast_alert_sync
            _broadcast_alert_sync({
                "ticker": ticker or "",
                "alert_type": alert_type,
                "price": price,
                "message": message[:200] if message else "",
                "trade_type": trade_type,
                "duration_estimate": duration_estimate,
            })
        except Exception as _bc_err:
            logger.debug(
                "[alerts] broadcast_alert_sync failed: %s",
                _bc_err,
                exc_info=True,
            )
    except Exception as e:
        logger.error(f"[alerts] dispatch_alert DB error: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        if own_session:
            db.close()

    return sent


def get_alert_history(
    db: Session,
    user_id: int | None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    from ...models.trading import AlertHistory
    rows = (
        db.query(AlertHistory)
        .filter(AlertHistory.user_id == user_id)
        .order_by(AlertHistory.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "alert_type": r.alert_type,
            "ticker": r.ticker,
            "message": r.message,
            "trade_type": r.trade_type,
            "duration_estimate": r.duration_estimate,
            "scan_pattern_id": getattr(r, "scan_pattern_id", None),
            "sent_via": r.sent_via,
            "success": r.success,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ── Strategy Proposals ────────────────────────────────────────────────


def _supersede_proposals(db: Session, user_id: int | None, ticker: str) -> int:
    """Reject any existing pending/approved/working proposals for *ticker* so
    only the newest proposal remains active. Returns count of superseded rows."""
    from ...models.trading import StrategyProposal
    from sqlalchemy import or_

    user_filter = (
        or_(StrategyProposal.user_id == user_id, StrategyProposal.user_id.is_(None))
        if user_id is not None
        else StrategyProposal.user_id.is_(None)
    )
    stale = (
        db.query(StrategyProposal)
        .filter(
            user_filter,
            StrategyProposal.ticker == ticker,
            StrategyProposal.status.in_(["pending", "approved", "working"]),
        )
        .all()
    )
    now = datetime.utcnow()
    for p in stale:
        p.status = "rejected"
        p.reviewed_at = now
    return len(stale)


def generate_strategy_proposals(
    db: Session,
    user_id: int | None,
) -> list[dict[str, Any]]:
    """Generate strategy proposals from high-confidence top picks.

    Called after each learning cycle and by the price monitor when
    high-confidence opportunities emerge.
    """
    from collections import Counter

    from ...models.trading import ScanPattern, StrategyProposal, ScanResult
    from .scanner import generate_top_picks
    from .market_data import fetch_quote

    picks = generate_top_picks(db, user_id)
    created: list[dict[str, Any]] = []
    skips: Counter[str] = Counter()
    picks_total = len(picks)

    buying_power = _get_buying_power()

    for pick in picks:
        combined = pick.get("combined_score", 0)
        if combined < _get_brain_weight("alert_min_score_proposal"):
            skips["below_combined_threshold"] += 1
            continue
        if pick.get("signal") != "buy":
            skips["not_buy_signal"] += 1
            continue

        ticker = pick["ticker"]

        spid = pick.get("scan_pattern_id")
        if spid is None:
            logger.info("[proposals] skip %s: no_scan_pattern_id", ticker)
            skips["no_scan_pattern_id"] += 1
            continue
        pat = db.query(ScanPattern).filter(ScanPattern.id == int(spid)).first()
        if pat is None or not pat.active:
            logger.info(
                "[proposals] skip %s: pattern_missing_or_inactive id=%s",
                ticker,
                spid,
            )
            skips["pattern_missing_or_inactive"] += 1
            continue
        if (pat.promotion_status or "").strip().lower() != "promoted":
            logger.info(
                "[proposals] skip %s: pattern_not_promoted id=%s status=%s",
                ticker,
                spid,
                getattr(pat, "promotion_status", None),
            )
            skips["pattern_not_promoted"] += 1
            continue

        if not _proposal_passes_sector_cap(db, user_id, ticker):
            logger.info("[proposals] skip %s: sector_cap", ticker)
            skips["sector_cap"] += 1
            continue

        # Supersede any existing non-executed proposal for this ticker
        _supersede_proposals(db, user_id, ticker)

        # Use latest Massive-backed price for entry
        quote = fetch_quote(ticker) or {}
        price = quote.get("price") or pick.get("price") or pick.get("entry_price") or 0
        stop = pick.get("stop_loss") or pick.get("brain_stop") or 0
        target = pick.get("take_profit") or pick.get("brain_target") or 0

        if not price or price <= 0 or not stop or not target:
            skips["quote_missing_levels"] += 1
            continue

        risk_per_share = abs(price - stop)
        reward_per_share = abs(target - price)

        if risk_per_share <= 0:
            skips["risk_per_share_nonpositive"] += 1
            continue

        rr_ratio = round(reward_per_share / risk_per_share, 2)

        if rr_ratio < _get_brain_weight("alert_min_rr_proposal"):
            skips["risk_reward_below_min"] += 1
            continue
        if price < _get_brain_weight("alert_min_price"):
            skips["price_below_min"] += 1
            continue

        projected_profit_pct = round((target - price) / price * 100, 2)
        projected_loss_pct = round((price - stop) / price * 100, 2)

        quantity, position_size_pct = _compute_position_size(
            price, stop, buying_power, pick,
        )
        # If no buying power (e.g. broker returned 0), use default so we still suggest a size
        if quantity is None and position_size_pct is None:
            quantity, position_size_pct = _compute_position_size(
                price, stop, 10000.0, pick,
            )
        if quantity is None:
            quantity = 1
        if position_size_pct is None:
            position_size_pct = round(min(_get_brain_weight("pos_pct_hard_cap"), (quantity * price) / max(buying_power, 10000.0) * 100), 2)

        confidence = pick.get("brain_confidence") or (combined * 10)

        signals = pick.get("signals", [])
        indicators = pick.get("indicators", {})

        # Classify trade type and estimate hold duration
        from .scanner import _estimate_hold_duration, classify_trade_type
        hold_est = pick.get("hold_estimate") or {}
        if not hold_est.get("label") and indicators.get("atr") and price and target:
            _tf = "15m" if pick.get("is_crypto") else "1d"
            hold_est = _estimate_hold_duration(
                price, target, indicators["atr"], _tf,
                indicators.get("adx"),
            )

        trade_class = classify_trade_type(
            signals, hold_est, indicators,
            is_crypto=pick.get("is_crypto", False),
        )
        trade_type_label = trade_class["label"]
        duration_label = trade_class["duration"] or hold_est.get("label", "")

        timeframe = pick.get("timeframe", "swing")
        if duration_label:
            timeframe = f"{trade_type_label} ({duration_label})"
        else:
            timeframe = trade_type_label

        thesis = pick.get("thesis", "")
        if not thesis:
            thesis = f"AI-identified bullish setup for {ticker} with score {combined:.1f}/10."

        _spid = pick.get("scan_pattern_id")
        proposal = StrategyProposal(
            user_id=user_id,
            ticker=ticker,
            direction="long",
            status="pending",
            entry_price=price,
            stop_loss=stop,
            take_profit=target,
            quantity=quantity,
            position_size_pct=position_size_pct,
            projected_profit_pct=projected_profit_pct,
            projected_loss_pct=projected_loss_pct,
            risk_reward_ratio=rr_ratio,
            confidence=round(confidence, 1),
            timeframe=timeframe,
            thesis=thesis,
            signals_json=json.dumps(signals) if signals else None,
            indicator_json=json.dumps(indicators) if indicators else None,
            brain_score=pick.get("brain_score"),
            ml_probability=pick.get("ml_probability"),
            scan_score=pick.get("score"),
            scan_pattern_id=int(_spid) if _spid is not None else None,
            proposed_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=_PROPOSAL_EXPIRE_HOURS),
        )
        db.add(proposal)
        db.flush()

        # Send SMS about the new proposal
        _cr = ticker.endswith("-USD")
        price_fmt = f"${price:,.6f}" if _cr and price < 1 else f"${price:,.2f}"
        _dur_part = f" | ETA {duration_label}" if duration_label else ""
        sms_msg = (
            f"CHILI {trade_type_label}: BUY {ticker} @ {price_fmt} | "
            f"Stop ${stop:,.2f} | Target ${target:,.2f} | "
            f"R:R {rr_ratio:.1f}:1 | "
            f"+{projected_profit_pct:.1f}% profit | "
            f"Conf {confidence:.0f}%{_dur_part} | "
            f"Review in app"
        )
        dispatch_alert(
            db, user_id, STRATEGY_PROPOSED, ticker, sms_msg,
            trade_type=trade_class["type"],
            duration_estimate=duration_label or None,
        )

        created.append({
            "id": proposal.id,
            "ticker": ticker,
            "confidence": confidence,
            "rr_ratio": rr_ratio,
        })

    try:
        db.commit()
    except Exception:
        db.rollback()

    try:
        from ..brain_worker_signals import persist_last_proposal_skips_json

        persist_last_proposal_skips_json(
            db,
            {
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "user_id": user_id,
                "picks_total": picks_total,
                "created": len(created),
                "skips": dict(skips),
            },
        )
    except Exception as _pse:
        logger.warning("[proposals] persist skip stats failed: %s", _pse)

    logger.info(
        "[alerts] Generated %s strategy proposals (skips=%s)",
        len(created),
        dict(skips),
    )
    return created


def create_proposal_from_pick(
    db: Session,
    user_id: int | None,
    ticker: str,
    pick: dict[str, Any] | None = None,
    override_levels: dict[str, float] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Create (or replace) a single strategy proposal for *ticker* using the
    latest Massive-backed price and the pick's or override stop/target levels.

    override_levels may contain entry_price, stop_loss, take_profit (used when
    ticker is not in top picks cache).

    Returns (proposal_dict, None) on success, (None, error_message) on failure.
    """
    from ...models.trading import StrategyProposal
    from .market_data import fetch_quote
    from .scanner import generate_top_picks

    ticker = ticker.upper()
    override_levels = override_levels or {}

    if pick is None:
        from .scanner import _top_picks_cache
        cached = _top_picks_cache.get("picks") or []
        pick = next((p for p in cached if (p.get("ticker") or "").upper() == ticker), None)
        if pick is None:
            picks = generate_top_picks(db, user_id)
            pick = next((p for p in picks if (p.get("ticker") or "").upper() == ticker), None)
    if not pick and override_levels:
        quote = fetch_quote(ticker) or {}
        entry = override_levels.get("entry_price") or quote.get("price")
        stop = override_levels.get("stop_loss")
        target = override_levels.get("take_profit")
        if stop and target and (entry or quote.get("price")):
            entry = entry or quote.get("price")
            pick = {
                "ticker": ticker,
                "price": entry,
                "entry_price": entry,
                "stop_loss": stop,
                "brain_stop": stop,
                "take_profit": target,
                "brain_target": target,
                "combined_score": 5.0,
                "timeframe": "swing",
                "thesis": f"Proposal from pick for {ticker} (levels from request).",
            }
    if not pick:
        return None, f"{ticker} is not in current top picks. Run a Full Scan or refresh, or the pick may have expired."

    spid_gate = pick.get("scan_pattern_id")
    if spid_gate is None:
        return None, "Proposal requires a linked promoted ScanPattern (scan_pattern_id missing)."
    from ...models.trading import ScanPattern as _SPGate
    _pat_gate = db.query(_SPGate).filter(_SPGate.id == int(spid_gate)).first()
    if _pat_gate is None or not _pat_gate.active:
        return None, "Linked ScanPattern is missing or inactive."
    if (_pat_gate.promotion_status or "").strip().lower() != "promoted":
        return None, "Linked ScanPattern must be promotion_status=promoted."

    combined = pick.get("combined_score", 0)

    quote = fetch_quote(ticker) or {}
    price = quote.get("price") or pick.get("price") or pick.get("entry_price") or 0
    stop = pick.get("stop_loss") or pick.get("brain_stop") or 0
    target = pick.get("take_profit") or pick.get("brain_target") or 0

    if not price or price <= 0:
        return None, "Could not get current price for this ticker."
    if not stop or not target:
        return None, "Missing stop loss or take profit; cannot create proposal."

    risk_per_share = abs(price - stop)
    reward_per_share = abs(target - price)
    if risk_per_share <= 0:
        return None, "Stop loss must be different from entry."

    rr_ratio = round(reward_per_share / risk_per_share, 2)
    min_rr = _get_brain_weight("alert_min_rr_from_pick")
    if rr_ratio < min_rr:
        return None, f"Risk:reward ratio {rr_ratio}:1 is below minimum {min_rr}:1."
    _min_price = _get_brain_weight("alert_min_price")
    if price < _min_price:
        return None, f"Price ${price} is below minimum ${_min_price}."

    _supersede_proposals(db, user_id, ticker)

    projected_profit_pct = round((target - price) / price * 100, 2)
    projected_loss_pct = round((price - stop) / price * 100, 2)

    buying_power = _get_buying_power()
    quantity, position_size_pct = _compute_position_size(price, stop, buying_power, pick)
    # Use default buying power so we never default to 1 share without brain math
    if quantity is None and position_size_pct is None:
        quantity, position_size_pct = _compute_position_size(price, stop, 10000.0, pick)
    if quantity is None:
        quantity = max(1, int((buying_power or 10000.0) * (_MAX_RISK_PCT / 100) / risk_per_share))
    if position_size_pct is None:
        position_size_pct = round(min(_POS_PCT_HARD_CAP, (quantity * price) / max(buying_power or 10000.0, 1) * 100), 2)
    confidence = pick.get("brain_confidence") or (combined * 10)
    signals = pick.get("signals", [])
    indicators = pick.get("indicators", {})

    from .scanner import _estimate_hold_duration, classify_trade_type
    hold_est = pick.get("hold_estimate") or {}
    if not hold_est.get("label") and indicators.get("atr") and price and target:
        _tf = "15m" if pick.get("is_crypto") else "1d"
        hold_est = _estimate_hold_duration(
            price, target, indicators["atr"], _tf,
            indicators.get("adx"),
        )

    trade_class = classify_trade_type(
        signals, hold_est, indicators,
        is_crypto=pick.get("is_crypto", False),
    )
    trade_type_label = trade_class["label"]
    duration_label = trade_class["duration"] or hold_est.get("label", "")

    timeframe = pick.get("timeframe", "swing")
    if duration_label:
        timeframe = f"{trade_type_label} ({duration_label})"
    else:
        timeframe = trade_type_label
    thesis = pick.get("thesis", "")
    if not thesis:
        thesis = f"AI-identified bullish setup for {ticker} with score {combined:.1f}/10."

    _spid_pick = pick.get("scan_pattern_id")
    proposal = StrategyProposal(
        user_id=user_id,
        ticker=ticker,
        direction="long",
        status="pending",
        entry_price=price,
        stop_loss=stop,
        take_profit=target,
        quantity=quantity,
        position_size_pct=position_size_pct,
        projected_profit_pct=projected_profit_pct,
        projected_loss_pct=projected_loss_pct,
        risk_reward_ratio=rr_ratio,
        confidence=round(confidence, 1),
        timeframe=timeframe,
        thesis=thesis,
        signals_json=json.dumps(signals) if signals else None,
        indicator_json=json.dumps(indicators) if indicators else None,
        brain_score=pick.get("brain_score"),
        ml_probability=pick.get("ml_probability"),
        scan_score=pick.get("score"),
        scan_pattern_id=int(_spid_pick) if _spid_pick is not None else None,
        proposed_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=_PROPOSAL_EXPIRE_HOURS),
    )
    db.add(proposal)
    try:
        db.commit()
        db.refresh(proposal)
    except Exception as e:
        db.rollback()
        logger.exception("create_proposal_from_pick commit failed")
        return None, f"Database error: {e!s}"

    logger.info(f"[alerts] Created proposal from pick: {ticker} @ ${price}")
    return _proposal_to_dict(proposal), None


def get_proposals(
    db: Session,
    user_id: int | None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    from ...models.trading import StrategyProposal
    from sqlalchemy import or_

    # Expire old pending proposals
    _expire_proposals(db)

    if user_id is not None:
        q = db.query(StrategyProposal).filter(
            or_(StrategyProposal.user_id == user_id, StrategyProposal.user_id.is_(None))
        )
    else:
        q = db.query(StrategyProposal).filter(StrategyProposal.user_id.is_(None))
    if status:
        q = q.filter(StrategyProposal.status == status)
    else:
        q = q.filter(
            StrategyProposal.status.in_(["pending", "approved", "working", "executed", "rejected"]),
        )

    rows = q.order_by(StrategyProposal.proposed_at.desc()).limit(limit).all()
    return [_proposal_to_dict(r) for r in rows]


def approve_proposal(
    db: Session,
    proposal_id: int,
    user_id: int | None,
    broker: str | None = None,
) -> dict[str, Any]:
    """Approve a proposal and execute via the best available broker."""
    from ...models.trading import StrategyProposal
    from sqlalchemy import or_

    q = db.query(StrategyProposal).filter(StrategyProposal.id == proposal_id)
    if user_id is not None:
        q = q.filter(or_(StrategyProposal.user_id == user_id, StrategyProposal.user_id.is_(None)))
    proposal = q.first()

    if not proposal:
        return {"ok": False, "error": "Proposal not found"}
    if proposal.status != "pending":
        return {"ok": False, "error": f"Proposal is already {proposal.status}"}
    if proposal.expires_at and proposal.expires_at < datetime.utcnow():
        proposal.status = "expired"
        db.commit()
        return {"ok": False, "error": "Proposal has expired"}

    proposal.status = "approved"
    proposal.reviewed_at = datetime.utcnow()
    db.commit()

    execution = _execute_proposal(db, proposal, user_id, broker=broker)

    return {
        "ok": True,
        "proposal": _proposal_to_dict(proposal),
        "execution": execution,
    }


def reject_proposal(
    db: Session,
    proposal_id: int,
    user_id: int | None,
) -> dict[str, Any]:
    from ...models.trading import StrategyProposal
    from sqlalchemy import or_

    q = db.query(StrategyProposal).filter(StrategyProposal.id == proposal_id)
    if user_id is not None:
        q = q.filter(or_(StrategyProposal.user_id == user_id, StrategyProposal.user_id.is_(None)))
    proposal = q.first()

    if not proposal:
        return {"ok": False, "error": "Proposal not found"}

    proposal.status = "rejected"
    proposal.reviewed_at = datetime.utcnow()
    db.commit()

    return {"ok": True, "proposal": _proposal_to_dict(proposal)}


def recheck_proposal(
    db: Session,
    proposal_id: int,
    user_id: int | None,
    *,
    drift_expire_pct: float = 30.0,
) -> dict[str, Any]:
    """Revalidate a proposal with live price. Returns drift info; expires if drifted past threshold."""
    from ...models.trading import StrategyProposal
    from sqlalchemy import or_
    from .market_data import fetch_quote

    q = db.query(StrategyProposal).filter(StrategyProposal.id == proposal_id)
    if user_id is not None:
        q = q.filter(or_(StrategyProposal.user_id == user_id, StrategyProposal.user_id.is_(None)))
    proposal = q.first()

    if not proposal:
        return {"ok": False, "error": "Proposal not found"}

    quote = fetch_quote(proposal.ticker)
    live_price = quote.get("price") if quote else None

    if not live_price or live_price <= 0:
        return {
            "ok": True,
            "proposal": _proposal_to_dict(proposal),
            "live_price": None,
            "drift_pct": None,
            "status": "unavailable",
            "message": "Could not fetch current price.",
        }

    drift_pct = abs(live_price - proposal.entry_price) / proposal.entry_price * 100

    if drift_pct > drift_expire_pct and proposal.status in ("pending", "approved"):
        proposal.status = "expired"
        proposal.thesis = (proposal.thesis or "") + f" [Auto-expired: price drifted {drift_pct:.0f}% from entry]"
        db.commit()
        return {
            "ok": True,
            "proposal": _proposal_to_dict(proposal),
            "live_price": live_price,
            "drift_pct": round(drift_pct, 2),
            "status": "invalidated",
            "expired": True,
        }

    status = "valid" if drift_pct <= 5 else ("moved_but_ok" if drift_pct <= 15 else "invalidated")
    return {
        "ok": True,
        "proposal": _proposal_to_dict(proposal),
        "live_price": live_price,
        "drift_pct": round(drift_pct, 2),
        "status": status,
        "expired": False,
    }


# ── Price Monitor ─────────────────────────────────────────────────────


def run_price_monitor(db: Session, user_id: int | None) -> dict[str, Any]:
    """Check positions, breakouts, and picks for alert-worthy events.

    Designed to run every 5 minutes during market hours.
    """
    results: dict[str, Any] = {
        "targets_hit": 0,
        "stops_hit": 0,
        "breakouts": 0,
        "proposals_generated": 0,
    }

    # 1. Check open positions for target/stop hits
    results.update(_check_open_positions(db, user_id))

    # 2. Check breakout candidates
    results["breakouts"] = _check_breakout_candidates(db, user_id)

    # 3. Check for new high-confidence picks → auto-generate proposals
    proposals = _check_top_picks_for_proposals(db, user_id)
    results["proposals_generated"] = len(proposals)

    logger.info(f"[alerts] Price monitor: {results}")
    return results


def _check_open_positions(db: Session, user_id: int | None) -> dict[str, int]:
    """Check open trades against current prices for stop/target alerts."""
    from ...models.trading import Trade
    from .market_data import fetch_quote

    targets_hit = 0
    stops_hit = 0

    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "open",
    ).all()

    for trade in open_trades:
        try:
            quote = fetch_quote(trade.ticker)
            price = quote.get("price", 0) if quote else 0
            if not price or price <= 0:
                continue

            # Parse stop/target from indicator_snapshot or tags
            snapshot = {}
            if trade.indicator_snapshot:
                try:
                    snapshot = json.loads(trade.indicator_snapshot)
                except Exception:
                    pass

            stop = snapshot.get("stop_loss") or (trade.entry_price * 0.95)
            target = snapshot.get("take_profit") or (trade.entry_price * 1.10)

            if trade.direction == "long":
                # Derive trade type context from the stored snapshot
                _tt_label = ""
                _tt_type = None
                try:
                    _ind = snapshot.get("indicators") or {}
                    _sigs_raw = snapshot.get("signals") or []
                    if _sigs_raw or _ind:
                        from .scanner import classify_trade_type
                        _tc = classify_trade_type(_sigs_raw, None, _ind)
                        _tt_label = f" [{_tc['label']}]"
                        _tt_type = _tc["type"]
                except Exception:
                    pass

                if price >= target:
                    pnl_pct = round((price - trade.entry_price) / trade.entry_price * 100, 2)
                    msg = (
                        f"TARGET HIT{_tt_label}: {trade.ticker} reached ${price:,.2f} "
                        f"(target ${target:,.2f}) | +{pnl_pct:.1f}% | "
                        f"Consider taking profits"
                    )
                    dispatch_alert(db, user_id, TARGET_HIT, trade.ticker, msg, trade_type=_tt_type)
                    targets_hit += 1

                elif price <= stop:
                    pnl_pct = round((price - trade.entry_price) / trade.entry_price * 100, 2)
                    msg = (
                        f"STOP HIT{_tt_label}: {trade.ticker} dropped to ${price:,.2f} "
                        f"(stop ${stop:,.2f}) | {pnl_pct:.1f}% | "
                        f"Consider cutting losses"
                    )
                    dispatch_alert(db, user_id, STOP_HIT, trade.ticker, msg, trade_type=_tt_type)
                    _expire_proposals_on_stop(db, user_id, trade.ticker)
                    stops_hit += 1

        except Exception as e:
            logger.debug(f"[alerts] Error checking {trade.ticker}: {e}")

    return {"targets_hit": targets_hit, "stops_hit": stops_hit}


def _check_breakout_candidates(db: Session, user_id: int | None) -> int:
    """Check recent scan results with breakout signals."""
    from ...models.trading import ScanResult, AlertHistory
    from .market_data import fetch_quote

    cutoff = datetime.utcnow() - timedelta(hours=6)
    recent_scans = (
        db.query(ScanResult)
        .filter(
            ScanResult.scanned_at >= cutoff,
            ScanResult.score >= _get_brain_weight("alert_breakout_min_score"),
        )
        .all()
    )

    breakouts = 0
    for scan in recent_scans:
        try:
            # Only alert if we haven't already alerted for this ticker recently
            recent_alert = (
                db.query(AlertHistory)
                .filter(
                    AlertHistory.ticker == scan.ticker,
                    AlertHistory.alert_type == BREAKOUT_TRIGGERED,
                    AlertHistory.created_at >= datetime.utcnow() - timedelta(hours=4),
                )
                .first()
            )
            if recent_alert:
                continue

            quote = fetch_quote(scan.ticker)
            price = quote.get("price", 0) if quote else 0
            if not price:
                continue

            # Check if price has broken above the resistance (take_profit level as proxy)
            resistance = scan.take_profit or (scan.entry_price * 1.03 if scan.entry_price else 0)
            if resistance and price > resistance and scan.signal == "buy":
                _dur = ""
                _bk_trade_type = "breakout"
                _bk_duration = None
                if scan.entry_price and resistance and scan.entry_price > 0:
                    try:
                        from .scanner import _estimate_hold_duration, classify_trade_type
                        _inds = json.loads(scan.indicator_data) if scan.indicator_data else {}
                        _atr = _inds.get("atr", 0)
                        _adx = _inds.get("adx")
                        _sigs = _inds.get("signals", [])
                        if _atr > 0:
                            _he = _estimate_hold_duration(price, resistance * 1.05, _atr, "1d", _adx)
                            _tc = classify_trade_type(_sigs, _he, _inds)
                            _bk_trade_type = _tc["type"]
                            _bk_duration = _tc["duration"] or None
                            _dur = f" | {_tc['label']}"
                            if _tc["duration"]:
                                _dur += f" ETA {_tc['duration']}"
                    except Exception:
                        pass
                msg = (
                    f"BREAKOUT: {scan.ticker} broke ${resistance:,.2f} "
                    f"now at ${price:,.2f} | Score {scan.score:.1f}/10{_dur} | "
                    f"{scan.rationale[:60] if scan.rationale else ''}"
                )
                dispatch_alert(
                    db, user_id, BREAKOUT_TRIGGERED, scan.ticker, msg,
                    trade_type=_bk_trade_type, duration_estimate=_bk_duration,
                )
                breakouts += 1

        except Exception as e:
            logger.debug(f"[alerts] Error checking breakout {scan.ticker}: {e}")

    return breakouts


def _check_top_picks_for_proposals(
    db: Session,
    user_id: int | None,
) -> list[dict]:
    """If high-confidence picks exist without proposals, create them."""
    from ...models.trading import StrategyProposal
    from .scanner import generate_top_picks

    picks = generate_top_picks(db, user_id)
    new_proposals = []

    for pick in picks:
        if (pick.get("combined_score", 0) < _get_brain_weight("alert_auto_proposal_min_score") or
                pick.get("signal") != "buy"):
            continue

        ticker = pick["ticker"]
        existing = (
            db.query(StrategyProposal)
            .filter(
                StrategyProposal.ticker == ticker,
                StrategyProposal.status.in_(["pending", "approved", "working", "executed"]),
                StrategyProposal.proposed_at >= datetime.utcnow() - timedelta(hours=12),
            )
            .first()
        )
        if existing:
            continue

        # Delegate to the full proposal generator (one at a time)
        proposals = generate_strategy_proposals(db, user_id)
        new_proposals.extend(proposals)
        break  # generate_strategy_proposals handles all qualifying picks

    return new_proposals


# ── Helpers ───────────────────────────────────────────────────────────


def _scan_pattern_id_from_proposal(proposal) -> int | None:
    """Best-effort link closed trades to ScanPattern for live vs research attribution."""
    raw = getattr(proposal, "signals_json", None)
    if not raw:
        return None
    try:
        sig = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if isinstance(sig, list):
        for item in sig:
            if not isinstance(item, dict):
                continue
            for key in ("scan_pattern_id", "pattern_id", "scanPatternId"):
                if item.get(key) is not None:
                    try:
                        return int(item[key])
                    except (TypeError, ValueError):
                        pass
    elif isinstance(sig, dict):
        for key in ("scan_pattern_id", "pattern_id"):
            if sig.get(key) is not None:
                try:
                    return int(sig[key])
                except (TypeError, ValueError):
                    pass
    return None


def _execute_proposal(
    db: Session,
    proposal,
    user_id: int | None,
    broker: str | None = None,
) -> dict[str, Any]:
    """Place a broker order for an approved proposal (or record locally).

    Uses broker_manager to auto-select the best broker (Coinbase for crypto,
    Robinhood for stocks) unless *broker* is explicitly provided.

    When a broker is connected the order is placed and both the proposal
    and trade start in **working** status.  Only the periodic order-sync
    job will flip them to *executed* / *open* once the broker confirms a
    fill — this prevents marking trades as "executed" when the limit order
    is still sitting unfilled.
    """
    from ..broker_manager import place_buy_order, map_status, get_best_broker_for, is_any_connected
    from ...models.trading import Trade

    _prop_spid = _scan_pattern_id_from_proposal(proposal)

    ticker = proposal.ticker
    quantity = proposal.quantity

    if not quantity or quantity <= 0:
        buying_power = _get_buying_power()
        stop = proposal.stop_loss or proposal.entry_price
        risk_per_share = abs(proposal.entry_price - stop) if stop else 0
        pick = {}
        if proposal.signals_json:
            try:
                _s = proposal.signals_json
                pick["signals"] = json.loads(_s) if isinstance(_s, str) else _s
            except Exception:
                pass
        pick["is_crypto"] = proposal.ticker.endswith("-USD")
        bp = buying_power if buying_power > 0 else 10000.0
        quantity, position_size_pct = _compute_position_size(
            proposal.entry_price, stop, bp, pick,
        )
        if quantity is not None and quantity > 0:
            proposal.quantity = quantity
            if position_size_pct is not None:
                proposal.position_size_pct = position_size_pct
        elif buying_power > 0 and risk_per_share > 0:
            risk_dollars = buying_power * (_MAX_RISK_PCT / 100)
            quantity = max(1, int(risk_dollars / risk_per_share))
            proposal.quantity = quantity
        else:
            quantity = 1
            proposal.quantity = 1

    target_broker = broker or get_best_broker_for(ticker)

    if not is_any_connected() or target_broker == "manual":
        trade = Trade(
            user_id=user_id,
            ticker=ticker,
            direction="long",
            entry_price=proposal.entry_price,
            quantity=quantity,
            status="open",
            broker_source="manual",
            scan_pattern_id=_prop_spid,
            tca_reference_entry_price=float(proposal.entry_price),
            indicator_snapshot=json.dumps({
                "stop_loss": proposal.stop_loss,
                "take_profit": proposal.take_profit,
                "proposal_id": proposal.id,
                "scan_pattern_id": _prop_spid,
            }),
            tags="proposal-approved",
            notes=f"Approved from proposal #{proposal.id} (manual — broker not connected)",
        )
        db.add(trade)
        db.flush()
        proposal.status = "executed"
        proposal.executed_at = datetime.utcnow()
        proposal.trade_id = trade.id
        db.commit()
        return {"status": "recorded", "trade_id": trade.id, "reason": "Broker not connected — trade recorded locally"}

    try:
        result = place_buy_order(
            ticker=ticker,
            quantity=quantity,
            order_type="limit",
            limit_price=proposal.entry_price,
            broker=target_broker,
        )

        used_broker = result.get("broker", target_broker)

        if result.get("ok"):
            raw_state = (result.get("raw") or {}).get("state") or (result.get("raw") or {}).get("status") or "queued"
            chili_status = map_status(used_broker, raw_state)
            is_already_filled = raw_state.lower() == "filled"

            proposal.broker_order_id = result.get("order_id")
            if is_already_filled:
                proposal.status = "executed"
                proposal.executed_at = datetime.utcnow()
            else:
                proposal.status = "working"

            _ptags = ""
            if proposal.signals_json:
                try:
                    _signals = json.loads(proposal.signals_json) if isinstance(proposal.signals_json, str) else proposal.signals_json
                    if isinstance(_signals, list):
                        _ptags = ",".join(_extract_pattern_keywords(_signals))
                except Exception:
                    pass

            avg_price = _safe_float(
                (result.get("raw") or {}).get("average_price")
                or (result.get("raw") or {}).get("average_filled_price")
            )

            trade = Trade(
                user_id=user_id,
                ticker=ticker,
                direction="long",
                entry_price=avg_price if is_already_filled and avg_price else proposal.entry_price,
                quantity=quantity,
                status="open" if is_already_filled else "working",
                broker_source=used_broker,
                broker_order_id=result.get("order_id"),
                broker_status=raw_state,
                last_broker_sync=datetime.utcnow(),
                filled_at=datetime.utcnow() if is_already_filled else None,
                avg_fill_price=avg_price if is_already_filled else None,
                tca_reference_entry_price=float(proposal.entry_price),
                strategy_proposal_id=proposal.id,
                scan_pattern_id=_prop_spid,
                indicator_snapshot=json.dumps({
                    "stop_loss": proposal.stop_loss,
                    "take_profit": proposal.take_profit,
                    "proposal_id": proposal.id,
                    "scan_pattern_id": _prop_spid,
                }),
                tags="auto-trade,proposal",
                pattern_tags=_ptags or None,
                notes=f"Order placed from proposal #{proposal.id} via {used_broker}: {proposal.thesis[:100]}",
            )
            db.add(trade)
            db.flush()
            if is_already_filled:
                try:
                    from .tca_service import apply_tca_on_trade_fill

                    apply_tca_on_trade_fill(trade)
                except Exception:
                    pass
            proposal.trade_id = trade.id
            db.commit()

            broker_label = used_broker.title()
            if is_already_filled:
                msg = (
                    f"ORDER FILLED: BUY {quantity} {ticker} @ ${trade.entry_price:,.2f} "
                    f"via {broker_label} | Proposal #{proposal.id}"
                )
                dispatch_alert(db, user_id, POSITION_OPENED, ticker, msg)
                return {"status": "executed", "order_id": result.get("order_id"), "trade_id": trade.id, "broker": used_broker}
            else:
                msg = (
                    f"ORDER PLACED: BUY {quantity} {ticker} @ ${proposal.entry_price:,.2f} "
                    f"(limit, waiting for fill) via {broker_label} | Proposal #{proposal.id}"
                )
                dispatch_alert(db, user_id, POSITION_OPENED, ticker, msg)
                return {"status": "working", "order_id": result.get("order_id"), "trade_id": trade.id, "broker": used_broker}
        else:
            error = result.get("error", "Unknown error")
            msg = f"ORDER FAILED: {ticker} — {error}"
            dispatch_alert(db, user_id, POSITION_OPENED, ticker, msg)
            return {"status": "failed", "error": error}

    except Exception as e:
        logger.error(f"[alerts] Execution failed for {ticker}: {e}")
        return {"status": "error", "error": str(e)}


def _safe_float(val) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _get_buying_power() -> float:
    """Get current buying power from Robinhood, or a sensible default."""
    try:
        from ..broker_service import is_connected, get_portfolio
        if is_connected():
            portfolio = get_portfolio()
            return portfolio.get("buying_power", 0)
    except Exception:
        pass
    return 10000.0  # default for position sizing when broker is not connected


_PRICE_DRIFT_EXPIRE_PCT = 30  # expire proposals when price drifts >30% from entry


def _open_trades_per_sector(db: Session, user_id: int) -> dict[str, int]:
    """Count open long trades by coarse sector (for concentration caps)."""
    from ...models.trading import Trade
    from .backtest_engine import TICKER_TO_SECTOR

    out: dict[str, int] = {}
    for t in (
        db.query(Trade)
        .filter(Trade.user_id == user_id, Trade.status == "open", Trade.direction == "long")
        .all()
    ):
        sec = TICKER_TO_SECTOR.get(t.ticker, "unknown")
        out[sec] = out.get(sec, 0) + 1
    return out


def _proposal_passes_sector_cap(db: Session, user_id: int | None, ticker: str) -> bool:
    from ...config import settings

    cap = int(getattr(settings, "brain_max_open_per_sector", 0) or 0)
    if cap <= 0 or user_id is None:
        return True
    from .backtest_engine import TICKER_TO_SECTOR

    sec = TICKER_TO_SECTOR.get(ticker, "unknown")
    counts = _open_trades_per_sector(db, user_id)
    if counts.get(sec, 0) >= cap:
        return False
    return True


def _expire_proposals(db: Session) -> int:
    """Mark expired pending proposals (time-based + price-drift)."""
    from ...models.trading import StrategyProposal
    from .market_data import fetch_quote

    count = 0

    # 1. Time-based expiry
    time_expired = (
        db.query(StrategyProposal)
        .filter(
            StrategyProposal.status == "pending",
            StrategyProposal.expires_at < datetime.utcnow(),
        )
        .all()
    )
    for p in time_expired:
        p.status = "expired"
        count += 1

    # 2. Price-drift expiry: if price moved far from entry, the setup is invalid
    active = (
        db.query(StrategyProposal)
        .filter(StrategyProposal.status.in_(["pending", "approved"]))
        .all()
    )
    for p in active:
        try:
            quote = fetch_quote(p.ticker)
            price = quote.get("price", 0) if quote else 0
            if price and p.entry_price and p.entry_price > 0:
                drift_pct = abs(price - p.entry_price) / p.entry_price * 100
                if drift_pct > _PRICE_DRIFT_EXPIRE_PCT:
                    p.status = "expired"
                    p.thesis = (p.thesis or "") + f" [Auto-expired: price drifted {drift_pct:.0f}% from entry]"
                    count += 1
                    logger.info(
                        f"[alerts] Price-drift expired {p.ticker}: "
                        f"entry ${p.entry_price:.2f} → ${price:.2f} ({drift_pct:.0f}%)"
                    )
        except Exception:
            pass

    if count:
        try:
            db.commit()
        except Exception:
            db.rollback()
    return count


def _proposal_to_dict(p) -> dict[str, Any]:
    now = datetime.utcnow()
    proposed_at = p.proposed_at
    expires_at = p.expires_at

    age_seconds = (
        round((now - proposed_at).total_seconds()) if proposed_at else None
    )
    expires_in_seconds = (
        round((expires_at - now).total_seconds()) if expires_at else None
    )
    is_expired = p.status == "expired" or (
        expires_at is not None and expires_at < now
    )

    expiry_reason = None
    if p.status == "expired" and p.thesis:
        if "[Auto-expired: price drifted" in p.thesis:
            expiry_reason = "price_drift"
        else:
            expiry_reason = "time"

    # Derive trade_type and duration from signals + indicators stored on the proposal
    trade_type_info = {"type": "swing", "label": "Swing Trade", "duration": ""}
    try:
        _sigs = json.loads(p.signals_json) if p.signals_json else []
        _inds = json.loads(p.indicator_json) if p.indicator_json else {}
        from .scanner import classify_trade_type, _estimate_hold_duration
        _he = {}
        if _inds.get("atr") and p.entry_price and p.take_profit and p.entry_price > 0:
            _tf = "15m" if (p.ticker or "").endswith("-USD") else "1d"
            _he = _estimate_hold_duration(
                p.entry_price, p.take_profit, _inds["atr"], _tf,
                _inds.get("adx"),
            )
        trade_type_info = classify_trade_type(
            _sigs, _he, _inds,
            is_crypto=(p.ticker or "").endswith("-USD"),
        )
    except Exception:
        pass

    return {
        "id": p.id,
        "ticker": p.ticker,
        "direction": p.direction,
        "status": p.status,
        "entry_price": p.entry_price,
        "stop_loss": p.stop_loss,
        "take_profit": p.take_profit,
        "quantity": p.quantity,
        "position_size_pct": p.position_size_pct,
        "projected_profit_pct": p.projected_profit_pct,
        "projected_loss_pct": p.projected_loss_pct,
        "risk_reward_ratio": p.risk_reward_ratio,
        "confidence": p.confidence,
        "timeframe": p.timeframe,
        "trade_type": trade_type_info["type"],
        "trade_type_label": trade_type_info["label"],
        "duration_estimate": trade_type_info["duration"],
        "thesis": p.thesis,
        "signals": json.loads(p.signals_json) if p.signals_json else [],
        "indicators": json.loads(p.indicator_json) if p.indicator_json else {},
        "brain_score": p.brain_score,
        "ml_probability": p.ml_probability,
        "scan_score": p.scan_score,
        "proposed_at": proposed_at.isoformat() if proposed_at else None,
        "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
        "executed_at": p.executed_at.isoformat() if p.executed_at else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "age_seconds": age_seconds,
        "expires_in_seconds": expires_in_seconds,
        "is_expired": is_expired,
        "expiry_reason": expiry_reason,
        "broker_order_id": p.broker_order_id,
        "trade_id": p.trade_id,
        "scan_pattern_id": getattr(p, "scan_pattern_id", None),
    }
