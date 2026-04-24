"""Build and persist :class:`Signal` rows (Q1.T3 phase 1)."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....config import settings
from .signal import Horizon, Side, Signal

logger = logging.getLogger(__name__)

_TYPE_TO_HORIZON: dict[str, Horizon] = {
    "scalp": "scalp",
    "daytrade": "day",
    "swing": "swing",
    "position": "position",
    "breakout": "swing",
    "reversal": "swing",
    "momentum": "swing",
    "trend_follow": "swing",
}


def _normalize_confidence(raw: float) -> float:
    x = float(raw)
    if x > 1.0:
        x = x / 10.0
    return min(1.0, max(0.0, x))


def _horizon_from_trade_class(
    trade_type: str, timeframe: str, is_crypto: bool,
) -> Horizon:
    tt = (trade_type or "").lower()
    if tt == "momentum" and is_crypto:
        return "day"
    if tt in _TYPE_TO_HORIZON:
        return _TYPE_TO_HORIZON[tt]
    tf = (timeframe or "").lower()
    if any(x in tf for x in ("15m", "5m", "1m", "30m")):
        return "intraday"
    return "swing"


def _venue(is_crypto: bool) -> str:
    return "CRYPTO" if is_crypto else "US_EQ"


def _decimal(x: Any, fallback: str = "0") -> Decimal:
    if x is None:
        return Decimal(fallback)
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(fallback)


def _optional_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _features_from_pick(pick: dict[str, Any], proposal_id: int | None) -> dict[str, Any]:
    indicators = pick.get("indicators") or {}
    out: dict[str, Any] = {}
    if isinstance(indicators, dict):
        out.update(indicators)
    if proposal_id is not None:
        out["strategy_proposal_id"] = proposal_id
    sp = pick.get("scan_pattern_id")
    if sp is not None:
        out["scan_pattern_id"] = sp
    return out


def _rule_fires_from_pick(pick: dict[str, Any]) -> list[str]:
    raw = pick.get("signals") or []
    if isinstance(raw, list):
        return [str(s) for s in raw]
    return []


def _side_from_pick(pick: dict[str, Any], proposal_direction: str | None) -> Side:
    d = (proposal_direction or pick.get("direction") or "").lower()
    sig = str(pick.get("signal") or "").lower()
    if d == "short" or sig in ("short", "sell"):
        return "short"
    if sig == "flat":
        return "flat"
    return "long"


def build_signal_from_strategy_pick(
    *,
    pick: dict[str, Any],
    proposal_id: int | None,
    entry: float,
    stop: float,
    target: float,
    trade_class: dict[str, Any],
    timeframe_label: str,
    created_at: datetime,
    expires_at: datetime,
    scanner: str,
    strategy_family: str,
    proposal_direction: str | None = None,
    gate_status: str = "proposed",
    gate_reasons: list[str] | None = None,
) -> Signal:
    is_crypto = bool(pick.get("is_crypto"))
    side = _side_from_pick(pick, proposal_direction)
    horizon = _horizon_from_trade_class(
        str(trade_class.get("type") or ""),
        str(pick.get("timeframe") or timeframe_label),
        is_crypto,
    )
    indicators = pick.get("indicators") or {}
    atr_raw = indicators.get("atr") if isinstance(indicators, dict) else None
    risk = abs(float(entry) - float(stop))
    atr_dec = _decimal(atr_raw, str(risk * 0.5 if risk else "0.01"))
    exp_ret = (
        _decimal((float(target) - float(entry)) / float(entry))
        if entry
        else Decimal("0")
    )
    exp_vol = (
        (_decimal(atr_raw) / _decimal(entry, "1"))
        if atr_raw and entry
        else Decimal("0.01")
    )
    conf = pick.get("brain_confidence")
    if conf is None:
        conf = (pick.get("combined_score") or 0) * 10
    confidence = _normalize_confidence(float(conf or 0))
    spid = pick.get("scan_pattern_id")
    pattern_id = str(int(spid)) if spid is not None else None

    conf_ds = pick.get("deflated_sharpe")
    conf_pbo = pick.get("pbo")
    regime = pick.get("regime")
    reg_post = pick.get("regime_posterior")
    thesis = pick.get("thesis")

    return Signal(
        signal_id=str(uuid.uuid4()),
        scanner=scanner,
        strategy_family=strategy_family,
        pattern_id=pattern_id,
        symbol=str(pick.get("ticker") or "").upper(),
        venue=_venue(is_crypto),
        side=side,
        horizon=horizon,
        created_at=created_at,
        expires_at=expires_at,
        entry_price=_decimal(entry),
        stop_price=_decimal(stop),
        take_profit_price=_decimal(target),
        atr=atr_dec,
        expected_return=exp_ret,
        expected_vol=exp_vol,
        confidence=confidence,
        deflated_sharpe=_optional_float(conf_ds),
        pbo=_optional_float(conf_pbo),
        regime=regime if isinstance(regime, str) else None,
        regime_posterior=reg_post if isinstance(reg_post, dict) else None,
        llm_rationale=thesis if isinstance(thesis, str) else None,
        features=_features_from_pick(pick, proposal_id),
        rule_fires=_rule_fires_from_pick(pick),
        gate_status=gate_status,  # type: ignore[arg-type]
        gate_reasons=list(gate_reasons or []),
    )


def _coerce_snapshot(ind: Any) -> dict[str, Any]:
    if ind is None:
        return {}
    if isinstance(ind, dict):
        return ind
    if isinstance(ind, str):
        try:
            o = json.loads(ind)
            return o if isinstance(o, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def build_signal_from_breakout_alert(
    *,
    alert: Any,
    scanner: str,
    strategy_family: str,
    created_at: datetime,
    expires_at: datetime,
    gate_status: str = "proposed",
    gate_reasons: list[str] | None = None,
) -> Signal:
    ticker = (alert.ticker or "").upper()
    is_crypto = (alert.asset_type or "").lower() == "crypto"
    entry = float(alert.entry_price or alert.price_at_alert or 0)
    stop = float(alert.stop_loss or (entry * 0.97 if entry else 0))
    target = float(alert.target_price or 0)
    snap = _coerce_snapshot(alert.indicator_snapshot)
    flat = snap.get("flat_indicators") if isinstance(snap.get("flat_indicators"), dict) else {}
    fused: dict[str, Any] = {**snap, **flat} if flat else dict(snap)
    atr_raw = fused.get("atr") if isinstance(fused, dict) else None

    signals_raw = alert.signals_snapshot or {}
    if isinstance(signals_raw, str):
        try:
            signals_raw = json.loads(signals_raw)
        except json.JSONDecodeError:
            signals_raw = {}
    sig_list: list[Any] = []
    if isinstance(signals_raw, dict):
        sig_list = signals_raw.get("signals") or []
    if not isinstance(sig_list, list):
        sig_list = []

    trade_type = "breakout"
    scorecard = snap.get("imminent_scorecard") if isinstance(snap, dict) else None
    if not (isinstance(scorecard, dict) and scorecard):
        trade_type = "swing"

    horizon = _horizon_from_trade_class(trade_type, alert.timeframe or "", is_crypto)
    risk = abs(entry - stop)
    atr_dec = _decimal(atr_raw, str(risk * 0.5 if risk else "0.01"))

    feats = dict(fused)
    feats["breakout_alert_id"] = int(alert.id)
    if alert.scan_pattern_id:
        feats["scan_pattern_id"] = int(alert.scan_pattern_id)

    conf_score = _normalize_confidence(float(alert.score_at_alert or 0.5))
    tp_dec: Decimal | None = _decimal(target) if target else None
    exp_ret = (
        _decimal((target - entry) / entry)
        if entry and target
        else Decimal("0")
    )
    exp_vol = (
        (_decimal(atr_raw) / _decimal(entry, "1"))
        if atr_raw and entry
        else Decimal("0.01")
    )

    return Signal(
        signal_id=str(uuid.uuid4()),
        scanner=scanner,
        strategy_family=strategy_family,
        pattern_id=str(int(alert.scan_pattern_id)) if alert.scan_pattern_id else None,
        symbol=ticker,
        venue=_venue(is_crypto),
        side="long",
        horizon=horizon,
        created_at=created_at,
        expires_at=expires_at,
        entry_price=_decimal(entry),
        stop_price=_decimal(stop),
        take_profit_price=tp_dec,
        atr=atr_dec,
        expected_return=exp_ret,
        expected_vol=exp_vol,
        confidence=conf_score,
        deflated_sharpe=None,
        pbo=None,
        regime=getattr(alert, "regime_at_alert", None),
        regime_posterior=None,
        llm_rationale=None,
        features=feats,
        rule_fires=[str(s) for s in sig_list],
        gate_status=gate_status,  # type: ignore[arg-type]
        gate_reasons=list(gate_reasons or []),
    )


def persist_unified_signal(db: Session, signal: Signal) -> None:
    tp = signal.take_profit_price
    stmt = text(
        """
        INSERT INTO unified_signals (
            signal_id, scanner, strategy_family, pattern_id, symbol, venue, side, horizon,
            created_at, expires_at, entry_price, stop_price, take_profit_price,
            atr, expected_return, expected_vol, confidence,
            deflated_sharpe, pbo, regime, regime_posterior, llm_rationale,
            features, rule_fires, gate_status, gate_reasons
        ) VALUES (
            :signal_id, :scanner, :strategy_family, :pattern_id, :symbol, :venue, :side, :horizon,
            :created_at, :expires_at, :entry_price, :stop_price, :take_profit_price,
            :atr, :expected_return, :expected_vol, :confidence,
            :deflated_sharpe, :pbo, :regime, CAST(:regime_posterior AS jsonb), :llm_rationale,
            CAST(:features AS jsonb), CAST(:rule_fires AS jsonb), :gate_status,
            CAST(:gate_reasons AS jsonb)
        )
        """
    )
    db.execute(
        stmt,
        {
            "signal_id": signal.signal_id,
            "scanner": signal.scanner,
            "strategy_family": signal.strategy_family,
            "pattern_id": signal.pattern_id,
            "symbol": signal.symbol,
            "venue": signal.venue,
            "side": signal.side,
            "horizon": signal.horizon,
            "created_at": signal.created_at,
            "expires_at": signal.expires_at,
            "entry_price": str(signal.entry_price),
            "stop_price": str(signal.stop_price),
            "take_profit_price": str(tp) if tp is not None else None,
            "atr": str(signal.atr),
            "expected_return": str(signal.expected_return),
            "expected_vol": str(signal.expected_vol),
            "confidence": signal.confidence,
            "deflated_sharpe": signal.deflated_sharpe,
            "pbo": signal.pbo,
            "regime": signal.regime,
            "regime_posterior": json.dumps(signal.regime_posterior)
            if signal.regime_posterior
            else None,
            "llm_rationale": signal.llm_rationale,
            "features": json.dumps(signal.features, default=str),
            "rule_fires": json.dumps(signal.rule_fires),
            "gate_status": signal.gate_status,
            "gate_reasons": json.dumps(signal.gate_reasons),
        },
    )


def try_emit_unified_signal(db: Session, signal: Signal, *, commit: bool = False) -> None:
    if not getattr(settings, "chili_unified_signal_enabled", False):
        return
    try:
        if commit:
            persist_unified_signal(db, signal)
            db.commit()
        else:
            with db.begin_nested():
                persist_unified_signal(db, signal)
    except Exception:
        logger.exception("[unified_signal] persist failed signal_id=%s", signal.signal_id)
        if commit:
            try:
                db.rollback()
            except Exception:
                pass


def emit_signal_for_strategy_proposal(
    db: Session,
    *,
    pick: dict[str, Any],
    proposal: Any,
    trade_class: dict[str, Any],
    timeframe_label: str,
    scanner: str,
    strategy_family: str,
    commit: bool = False,
) -> None:
    sig = build_signal_from_strategy_pick(
        pick=pick,
        proposal_id=int(proposal.id),
        entry=float(proposal.entry_price),
        stop=float(proposal.stop_loss),
        target=float(proposal.take_profit),
        trade_class=trade_class,
        timeframe_label=timeframe_label,
        created_at=proposal.proposed_at,
        expires_at=proposal.expires_at,
        scanner=scanner,
        strategy_family=strategy_family,
        proposal_direction=getattr(proposal, "direction", None),
    )
    try_emit_unified_signal(db, sig, commit=commit)


def emit_signal_for_breakout_alert(
    db: Session,
    alert: Any,
    *,
    scanner: str,
    strategy_family: str,
    commit: bool = False,
) -> None:
    _e = float(
        getattr(alert, "entry_price", None)
        or getattr(alert, "price_at_alert", None)
        or 0
    )
    if _e <= 0:
        return
    created = getattr(alert, "alerted_at", None) or datetime.utcnow()
    if hasattr(created, "tzinfo") and created.tzinfo is not None:
        created = created.replace(tzinfo=None)
    expires = created + timedelta(hours=24)
    sig = build_signal_from_breakout_alert(
        alert=alert,
        scanner=scanner,
        strategy_family=strategy_family,
        created_at=created,
        expires_at=expires,
    )
    try_emit_unified_signal(db, sig, commit=commit)
