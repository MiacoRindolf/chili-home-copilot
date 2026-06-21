"""The #769 hard max-loss-circuit ABSOLUTE floor must anchor BOTH Robinhood equity
rails — the unofficial robin_stocks rail (``robinhood_spot``) AND the sanctioned
Agentic Trading MCP rail (``robinhood_agentic_mcp``).

The Monday equity lane routes through ``robinhood_agentic_mcp``, which trades the SAME
RH low-float names with the SAME gap-through risk (the −$697 MTEN/SDOT/CCTG/CAST tail
that gapped 5-9% THROUGH their tight stops). The circuit sets
``le["exit_floor_anchored"]`` so the BAILOUT submit flattens at the ABSOLUTE
loss-anchored floor (avg − K*stop_distance) instead of chasing a gapped book down the
bid-relative ladder. This integration test drives ``tick_live_session`` into a real
circuit breach for every execution family and asserts the floor is anchored for BOTH
RH equity rails and — by design — NOT for crypto (24/7, no LULD, dust keeps the ladder).

This complements ``tests/test_max_loss_circuit.py`` (which unit-tests the PURE
``max_loss_circuit_decision`` helper): here we assert the LIVE per-family wiring.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import MomentumSymbolViability
from app.services.trading.momentum_neural.persistence import create_trading_automation_session
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.services.trading.venue.protocol import (
    FreshnessMeta,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
)

from tests.test_momentum_paper_runner import _seed_live_eligible_row


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _fresh() -> FreshnessMeta:
    return FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=120.0)


def _uid(db: Session, suffix: str) -> int:
    u = User(name=f"CircuitFloor_{suffix}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


class _FakeAdapter:
    """A live spot adapter that fully fills any exit at the bid present when the order
    was placed. Lets the test drive a single tick into the circuit breach."""

    def __init__(self, bid: float):
        self._bid = float(bid)
        self._orders: dict[str, dict] = {}
        self._n = 0

    def set_bid(self, bid: float) -> None:
        self._bid = float(bid)

    def is_enabled(self) -> bool:
        return True

    def get_best_bid_ask(self, product_id):
        b = self._bid
        return (
            NormalizedTicker(
                product_id=product_id, bid=b, ask=b * 1.0005, mid=b,
                spread_bps=5.0, freshness=_fresh(),
            ),
            _fresh(),
        )

    def get_product(self, product_id):
        return (
            NormalizedProduct(
                product_id=product_id,
                base_currency=str(product_id).split("-")[0],
                quote_currency="USD",
                status="online",
                trading_disabled=False,
                cancel_only=False,
                limit_only=False,
                post_only=False,
                auction_mode=False,
                base_increment=0.0001,
                base_min_size=0.0001,
            ),
            _fresh(),
        )

    def get_position_quantity(self, product_id):
        return None  # unknown -> no broker-qty clamp (the session qty is used)

    def place_market_order(self, *, product_id, side, base_size, client_order_id=None, **kw):
        self._n += 1
        oid = f"mkt-{self._n}"
        self._orders[oid] = {"size": float(base_size), "price": self._bid}
        return {"ok": True, "order_id": oid, "client_order_id": client_order_id or oid}

    def place_limit_order_gtc(self, *, product_id, side, base_size, limit_price,
                              client_order_id=None, **kw):
        self._n += 1
        oid = f"lim-{self._n}"
        self._orders[oid] = {"size": float(base_size), "price": float(limit_price)}
        return {"ok": True, "order_id": oid, "client_order_id": client_order_id or oid}

    def get_order(self, order_id):
        rec = self._orders.get(str(order_id), {"size": 1e9, "price": self._bid})
        return (
            NormalizedOrder(
                order_id=str(order_id),
                client_order_id="c",
                product_id="X",
                side="sell",
                status="FILLED",
                order_type="limit",
                filled_size=rec["size"],
                average_filled_price=rec["price"],
            ),
            _fresh(),
        )

    def cancel_order(self, order_id):
        return {"ok": True, "raw": {}}


def _circuit_pos_snapshot(opened_iso: str, *, product_id: str) -> dict:
    """An ENTERED position whose TIGHT structural stop (avg − stop = $2 risk on qty=1)
    makes the K=2 circuit threshold $4 (floor at avg−$4 = 96). max_loss_per_trade_usd is
    LARGE so the 1× C1 check never pre-empts the structural-risk circuit (C1b)."""
    return {
        RISK_SNAPSHOT_KEY: {"allowed": True},
        "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
        "momentum_policy_caps": {
            "max_notional_per_trade_usd": 100000.0,
            "max_hold_seconds": 86400,
            "max_loss_per_trade_usd": 100000.0,  # large -> C1 (1x) never fires first
        },
        "momentum_live_execution": {
            "entry_slip_bps_ref": 6.0,
            "entry_stop_atr_pct": 0.02,
            "position": {
                "product_id": product_id,
                "side": "long",
                "quantity": 1.0,
                "original_quantity": 1.0,
                "avg_entry_price": 100.0,
                "notional_usd": 100.0,
                "opened_at_utc": opened_iso,
                "high_water_mark": 100.0,
                "stop_price": 98.0,     # structural risk = $2.00 -> K=2 threshold $4
                "target_price": 104.0,
            },
        },
    }


def _le(sess) -> dict:
    return (sess.risk_snapshot_json or {}).get("momentum_live_execution") or {}


def _drive_breach(db: Session, monkeypatch, *, symbol: str, execution_family: str) -> dict:
    """Seed a live-eligible ENTERED session for ``execution_family`` and drive ONE tick
    with the bid GAPPED THROUGH the structural stop (bid=95, floor=96) so the circuit
    breaches. Returns the resulting ``momentum_live_execution`` dict."""
    import app.services.trading.momentum_neural.live_runner as lr

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_max_loss_circuit_enabled", True)
    # Isolate the exit FSM: entry-risk boundary + kill switch always green.
    monkeypatch.setattr(lr, "runner_boundary_risk_ok", lambda *a, **k: (True, {}))
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)
    # The connectivity preflight calls the REAL broker is_connected() for robinhood_spot /
    # coinbase_spot (no live broker in tests -> the tick would skip). The agentic family
    # already fails open. Force the preflight green so every family exercises the circuit.
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda ef: True)

    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    via = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == symbol,
            MomentumSymbolViability.variant_id == vid,
        )
        .one()
    )
    via.viability_score = 0.9
    via.live_eligible = True
    db.commit()

    uid = _uid(db, execution_family)
    opened = datetime.now(timezone.utc).isoformat()
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol=symbol,
        variant_id=vid,
        mode="live",
        state="live_entered",
        execution_family=execution_family,
        risk_snapshot_json=_circuit_pos_snapshot(opened, product_id=symbol),
        correlation_id=f"c-circuit-{execution_family}",
    )
    db.commit()

    ad = _FakeAdapter(bid=95.0)  # −5% gap THROUGH the −4% floor -> circuit breach
    lr.tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    assert sess.state == "live_bailout", f"{execution_family}: expected bailout, got {sess.state}"
    le = _le(sess)
    assert le.get("max_loss_circuit_fired") is True, f"{execution_family}: circuit must fire"
    assert le.get("max_loss_circuit_floor_price") == pytest.approx(96.0)
    return le


# ---------------------------------------------------------------------------
# The two RH EQUITY rails anchor the absolute floor; crypto keeps the ladder.
# ---------------------------------------------------------------------------

def test_agentic_mcp_anchors_floor_on_circuit_breach(monkeypatch, db: Session):
    """The Monday equity lane (robinhood_agentic_mcp) trades the same RH low-float
    gap-through names — it MUST anchor the absolute floor exactly like robinhood_spot."""
    le = _drive_breach(
        db, monkeypatch, symbol="AGTX",
        execution_family="robinhood_agentic_mcp",
    )
    assert le.get("exit_floor_anchored") is True


def test_robinhood_spot_anchors_floor_on_circuit_breach(monkeypatch, db: Session):
    """Parity control: the original RH rail already anchored the floor (#769)."""
    le = _drive_breach(
        db, monkeypatch, symbol="RHSX",
        execution_family="robinhood_spot",
    )
    assert le.get("exit_floor_anchored") is True


def test_crypto_does_not_anchor_floor_on_circuit_breach(monkeypatch, db: Session):
    """By design: crypto (coinbase_spot) fires the circuit but keeps the bid-relative
    ladder (24/7, no LULD, dust) — exit_floor_anchored stays unset."""
    le = _drive_breach(
        db, monkeypatch, symbol="CBTX-USD",
        execution_family="coinbase_spot",
    )
    assert not le.get("exit_floor_anchored")
