"""Q1.T3 phase 2 — unified_signals consumer shadow layer.

Phase 1 (shipped) added an additive emit of ``Signal`` rows alongside
existing bespoke ``BreakoutAlert`` / ``StrategyProposal`` payloads.
Phase 2 (this module) adds a shadow consumer: when
``CHILI_UNIFIED_SIGNAL_CONSUMER_ENABLED=true``, the autotrader looks up
the corresponding ``unified_signals`` row for every ``BreakoutAlert``
it processes, computes the decision both ways, and logs any
discrepancy without changing the actual decision. This generates the
parity evidence needed before phase 3 (full cutover).

Flag stays ``False`` by default. When operator flips ``True``, the only
runtime cost is a single-row ``SELECT`` per alert and a structured log
emit; no DB writes from this module unless a discrepancy is found, in
which case one row goes to ``unified_signal_consumer_parity_log``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def find_unified_signal_for_breakout_alert(
    db: Session,
    alert_id: int,
) -> Optional[dict]:
    """Return the unified_signals row that was emitted for this alert.

    The phase-1 emit helper writes ``signal_id`` deterministically as
    ``f"breakout-alert-{alert_id}"`` so we can look it up by string match.
    Returns ``None`` if the consumer flag was off when the alert was
    emitted (no unified_signals row exists), or if the row was created
    but later cleaned up.
    """
    try:
        row = db.execute(
            text(
                """
                SELECT id, signal_id, scanner, strategy_family, symbol, side,
                       horizon, entry_price, stop_price, take_profit_price,
                       confidence, gate_status, gate_reasons, features
                FROM unified_signals
                WHERE signal_id = :sid
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"sid": f"breakout-alert-{alert_id}"},
        ).fetchone()
        if not row:
            return None
        return {
            "id": int(row[0]),
            "signal_id": row[1],
            "scanner": row[2],
            "strategy_family": row[3],
            "symbol": row[4],
            "side": row[5],
            "horizon": row[6],
            "entry_price": float(row[7]) if row[7] is not None else None,
            "stop_price": float(row[8]) if row[8] is not None else None,
            "take_profit_price": float(row[9]) if row[9] is not None else None,
            "confidence": float(row[10]) if row[10] is not None else None,
            "gate_status": row[11],
            "gate_reasons": row[12] or [],
            "features": row[13] or {},
        }
    except Exception as e:
        logger.debug("[signal_consumer] lookup failed for alert %s: %s", alert_id, e)
        return None


def cross_check_alert_vs_unified(
    db: Session,
    *,
    alert_id: int,
    alert_ticker: str,
    alert_entry_price: Optional[float],
    decision: str,
    decision_reason: Optional[str],
) -> dict:
    """Shadow check: compare the bespoke decision against what we'd
    derive from the unified_signals row.

    Returns a dict ``{matched, discrepancies, unified_signal_id}``.
    On any discrepancy, writes one row to
    ``unified_signal_consumer_parity_log`` for operator review.

    This is the heart of phase 2: it produces statistical evidence of
    consumer-readiness without affecting any actual decision.
    """
    out: dict = {
        "matched": True,
        "discrepancies": [],
        "unified_signal_id": None,
    }

    sig = find_unified_signal_for_breakout_alert(db, alert_id)
    if sig is None:
        # No unified_signals row — common during phase 1 ramp-up. Not a
        # parity violation; just no data to compare against.
        out["matched"] = None  # Tri-state: True / False / None (no_data)
        return out

    out["unified_signal_id"] = sig["id"]

    # Symbol must match (sanity check).
    if (sig["symbol"] or "").upper() != (alert_ticker or "").upper():
        out["matched"] = False
        out["discrepancies"].append(
            f"symbol_mismatch: alert={alert_ticker} unified={sig['symbol']}"
        )

    # Entry price within 1bp tolerance (rounding/cast noise is OK).
    if alert_entry_price is not None and sig["entry_price"] is not None:
        rel = abs(alert_entry_price - sig["entry_price"]) / max(
            abs(alert_entry_price), 1e-9
        )
        if rel > 0.0001:  # 1 bp
            out["matched"] = False
            out["discrepancies"].append(
                f"entry_price_mismatch: alert={alert_entry_price:.4f} "
                f"unified={sig['entry_price']:.4f} rel={rel:.6f}"
            )

    # Gate status sanity: when autotrader places ('placed'/'scaled_in'),
    # the unified_signals row should NOT be 'gated_reject'.
    if decision in ("placed", "scaled_in") and sig["gate_status"] == "gated_reject":
        out["matched"] = False
        out["discrepancies"].append(
            f"decision_vs_gate_status_mismatch: decision={decision} "
            f"unified.gate_status={sig['gate_status']} reasons={sig['gate_reasons']}"
        )

    # Persist parity row if we found discrepancies.
    if out["discrepancies"]:
        try:
            db.execute(
                text(
                    """
                    INSERT INTO unified_signal_consumer_parity_log
                        (alert_id, unified_signal_id, decision, decision_reason,
                         discrepancies, recorded_at)
                    VALUES (:aid, :usid, :dec, :reason, :disc, NOW())
                    """
                ),
                {
                    "aid": alert_id,
                    "usid": sig["id"],
                    "dec": decision,
                    "reason": (decision_reason or "")[:500],
                    "disc": json.dumps(out["discrepancies"]),
                },
            )
            db.commit()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.debug("[signal_consumer] parity log write failed: %s", e)

    return out


def maybe_shadow_consume(
    db: Session,
    *,
    alert_id: int,
    alert_ticker: str,
    alert_entry_price: Optional[float],
    decision: str,
    decision_reason: Optional[str] = None,
) -> Optional[dict]:
    """Top-level helper called from the autotrader. No-op when the
    consumer flag is off. Returns the cross-check dict on shadow run,
    None when flag is off.
    """
    try:
        from ...config import settings
        if not getattr(settings, "chili_unified_signal_consumer_enabled", False):
            return None
    except Exception:
        return None

    return cross_check_alert_vs_unified(
        db,
        alert_id=alert_id,
        alert_ticker=alert_ticker,
        alert_entry_price=alert_entry_price,
        decision=decision,
        decision_reason=decision_reason,
    )
