"""Ross asymmetric exit structure for the momentum_neural lane.

Ross Cameron's edge (avg winner ~4.4x avg loser) comes from the EXIT structure,
not win-rate: sell ~1/2 into the first (2:1) target, move the balance stop to
breakeven, then HOLD + trail the runner for the tail. A 2:1-then-flat exit caps
the upside. These tests cover:

  1. The shared pure helpers (scale-out fraction, breakeven, split + dust guard,
     chandelier runner trail) — the parity contract both runners call.
  2. The parity contract itself: live_runner + paper_runner reference the SAME
     helper objects, so backtest and live take the identical structural decision.
  3. Live integration: a winner that hits the first target sells the configured
     fraction, the balance stop becomes the entry price (breakeven), the runner is
     held and trailed up, and the runner captures additional upside vs the old flat
     2:1 exit.
  4. Paper integration: the same structure end-to-end (parity with live).
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import (
    MomentumSymbolViability,
    TradingAutomationEvent,
    TradingAutomationSimulatedFill,
    TradingDecisionPacket,
    TradingDeploymentState,
)
from app.services.trading.momentum_neural import paper_execution as pe
from app.services.trading.momentum_neural.persistence import create_trading_automation_session, variant_for_id
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.services.trading.momentum_neural.strategy_params import normalize_strategy_params
from app.services.trading.venue.protocol import (
    FreshnessMeta,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
)

from tests.test_momentum_paper_runner import _seed_live_eligible_row


# ---------------------------------------------------------------------------
# 1. Shared pure helpers
# ---------------------------------------------------------------------------

def test_scale_out_fraction_reads_setting_and_clamps(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.6)
    assert pe.scale_out_fraction() == 0.6
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.75)
    assert pe.scale_out_fraction() == 0.75
    # Defended in depth against a misconfig that would sell 0% or 100%.
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.0)
    assert pe.scale_out_fraction() == 0.05
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 1.5)
    assert pe.scale_out_fraction() == 0.95
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", float("nan"))
    assert pe.scale_out_fraction() == 0.5  # default fallback


def test_breakeven_stop_moves_to_entry_and_never_loosens():
    # Stop below entry -> ratchet UP to breakeven (entry).
    assert pe.breakeven_stop_after_partial(100.0, 95.0) == 100.0
    # Stop already above entry (a prior tighten) -> never loosen back to entry.
    assert pe.breakeven_stop_after_partial(100.0, 101.0) == 101.0
    # Bad inputs -> return the current stop unchanged.
    assert pe.breakeven_stop_after_partial("x", 95.0) == 95.0


def test_scale_out_quantity_splits_on_original_size():
    # Sell half of the ORIGINAL size; remainder is the runner.
    assert pe.scale_out_quantity(current_qty=1.0, original_qty=1.0, fraction=0.5) == (0.5, 0.5, True)
    # Fraction is of the ORIGINAL size, not the current holding.
    sq, rem, ok = pe.scale_out_quantity(current_qty=0.8, original_qty=1.0, fraction=0.5)
    assert ok is True
    assert sq == pytest.approx(0.5)   # 0.5 of original 1.0 (not 0.4 of current 0.8)
    assert rem == pytest.approx(0.3)
    # 0.6 fraction.
    assert pe.scale_out_quantity(current_qty=10.0, original_qty=10.0, fraction=0.6) == (6.0, 4.0, True)


def test_scale_out_quantity_floors_to_base_increment():
    # 0.5 of 1.0 = 0.5, floored to a 0.3 increment -> 0.3 (runner 0.7).
    sq, rem, ok = pe.scale_out_quantity(
        current_qty=1.0, original_qty=1.0, fraction=0.5, base_increment=0.3,
    )
    assert ok is True
    assert sq == pytest.approx(0.3)
    assert rem == pytest.approx(0.7)


def test_scale_out_quantity_refuses_to_strand_dust():
    # Tiny crypto position: either leg below the venue min sell size -> can't split,
    # so the caller flattens whole at target (never strands un-sellable dust).
    sq, rem, ok = pe.scale_out_quantity(
        current_qty=0.0015, original_qty=0.0015, fraction=0.5,
        base_increment=0.001, base_min_size=0.001,
    )
    assert ok is False
    assert sq == 0.0
    # Invalid / degenerate inputs never split.
    assert pe.scale_out_quantity(current_qty=0.0, original_qty=1.0, fraction=0.5)[2] is False
    assert pe.scale_out_quantity(current_qty=1.0, original_qty=1.0, fraction=1.0)[2] is False


def test_runner_trail_chandelier_ratchets_up_only_and_floors_at_breakeven():
    # Chandelier = hwm * (1 - atr_pct*mult) = 110 * (1 - 0.012) = 108.68.
    trailed = pe.runner_trail_stop(
        high_water_mark=110.0, atr_pct=0.02, stop_atr_mult=0.6,
        breakeven_floor=100.0, current_stop=100.0,
    )
    assert trailed == pytest.approx(108.68)
    # Never loosen: a chandelier BELOW the current stop returns the current stop.
    held = pe.runner_trail_stop(
        high_water_mark=101.0, atr_pct=0.02, stop_atr_mult=0.6,
        breakeven_floor=100.0, current_stop=108.68,
    )
    assert held == pytest.approx(108.68)
    # Never below the breakeven floor (the partial already de-risked the runner).
    floored = pe.runner_trail_stop(
        high_water_mark=100.5, atr_pct=0.02, stop_atr_mult=0.6,
        breakeven_floor=100.0, current_stop=99.0,
    )
    assert floored == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 2. Parity contract: both runners share the IDENTICAL exit helpers
# ---------------------------------------------------------------------------

def test_shared_exit_helpers_are_the_parity_contract():
    import app.services.trading.momentum_neural.live_runner as lr
    import app.services.trading.momentum_neural.paper_runner as pr

    assert lr.scale_out_fraction is pe.scale_out_fraction is pr.scale_out_fraction
    assert lr.scale_out_quantity is pe.scale_out_quantity is pr.scale_out_quantity
    assert lr.breakeven_stop_after_partial is pe.breakeven_stop_after_partial is pr.breakeven_stop_after_partial
    assert lr.runner_trail_stop is pe.runner_trail_stop is pr.runner_trail_stop


def test_scale_out_decision_is_identical_across_runners():
    # Given identical inputs the structural decision (qty to sell, breakeven stop,
    # trailed stop) is computed by the shared helpers -> paper and live agree by
    # construction. This is the backtest-vs-live parity for the exit structure.
    frac = pe.scale_out_fraction()
    assert (pe.scale_out_quantity(current_qty=1.0, original_qty=1.0, fraction=frac)) == (0.5, 0.5, True)
    assert pe.breakeven_stop_after_partial(100.0, 98.0) == 100.0
    assert pe.runner_trail_stop(
        high_water_mark=110.0, atr_pct=0.02, stop_atr_mult=0.6,
        breakeven_floor=100.0, current_stop=100.0,
    ) == pytest.approx(108.68)


# ---------------------------------------------------------------------------
# Integration harness
# ---------------------------------------------------------------------------

def _fresh() -> FreshnessMeta:
    return FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=120.0)


def _uid(db: Session, suffix: str) -> int:
    u = User(name=f"AsymExit_{suffix}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


class _FakeAdapter:
    """A live spot adapter that fully fills any market exit at the bid present when
    the order was placed. Lets a test drive a price path tick by tick."""

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

    def place_market_order(self, *, product_id, side, base_size, client_order_id=None):
        self._n += 1
        oid = f"ord-{self._n}"
        self._orders[oid] = {"size": float(base_size), "price": self._bid}
        return {"ok": True, "order_id": oid, "client_order_id": client_order_id or oid}

    def get_order(self, order_id):
        rec = self._orders.get(str(order_id), {"size": 1e9, "price": self._bid})
        return (
            NormalizedOrder(
                order_id=str(order_id),
                client_order_id="c",
                product_id="RUN-USD",
                side="sell",
                status="FILLED",
                order_type="market",
                filled_size=rec["size"],
                average_filled_price=rec["price"],
            ),
            _fresh(),
        )

    def cancel_order(self, order_id):
        return {"ok": True, "raw": {}}


def _live_pos_snapshot(opened_iso: str) -> dict:
    return {
        RISK_SNAPSHOT_KEY: {"allowed": True},
        "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
        "momentum_policy_caps": {
            "max_notional_per_trade_usd": 1000.0,
            "max_hold_seconds": 86400,
            "max_loss_per_trade_usd": 1000.0,
        },
        "momentum_live_execution": {
            "entry_slip_bps_ref": 6.0,
            "entry_stop_atr_pct": 0.02,
            "position": {
                "product_id": "RUN-USD",
                "side": "long",
                "quantity": 1.0,
                "original_quantity": 1.0,
                "avg_entry_price": 100.0,
                "notional_usd": 100.0,
                "opened_at_utc": opened_iso,
                "high_water_mark": 100.0,
                "stop_price": 98.0,    # risk = 2.0
                "target_price": 104.0,  # 2:1 target (entry + 2*risk)
            },
        },
    }


def _le(sess) -> dict:
    return (sess.risk_snapshot_json or {}).get("momentum_live_execution") or {}


def _seed_packet(db: Session, *, user_id: int, symbol: str, mode: str) -> TradingDecisionPacket:
    packet = TradingDecisionPacket(
        user_id=user_id,
        chosen_ticker=symbol,
        decision_type="trade",
        execution_mode=mode,
        deployment_stage="paper",
        source_surface="autopilot",
        outcome_status="pending",
        shadow_advisory_only=False,
    )
    db.add(packet)
    db.flush()
    return packet


def _deployment_state(
    db: Session, *, scope_type: str, scope_key: str
) -> TradingDeploymentState:
    return (
        db.query(TradingDeploymentState)
        .filter(
            TradingDeploymentState.scope_type == scope_type,
            TradingDeploymentState.scope_key == scope_key,
        )
        .one()
    )


# ---------------------------------------------------------------------------
# 3. Live integration: scale-out -> breakeven -> runner -> trail captures tail
# ---------------------------------------------------------------------------

def test_live_first_target_scales_out_moves_to_breakeven_and_runner_captures_upside(
    monkeypatch, db: Session
):
    import app.services.trading.momentum_neural.live_runner as lr

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.5)
    # Isolate the exit FSM: entry-risk boundary + kill switch always green.
    monkeypatch.setattr(lr, "runner_boundary_risk_ok", lambda *a, **k: (True, {}))
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)

    vid, _ = _seed_live_eligible_row(db, symbol="RUN-USD")
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "RUN-USD", MomentumSymbolViability.variant_id == vid)
        .one()
    )
    via.viability_score = 0.9
    via.live_eligible = True
    db.commit()

    uid = _uid(db, "live")
    opened = datetime.now(timezone.utc).isoformat()
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="RUN-USD",
        variant_id=vid,
        mode="live",
        state="live_entered",
        risk_snapshot_json=_live_pos_snapshot(opened),
        correlation_id="c-asym-live",
    )
    db.commit()

    ad = _FakeAdapter(bid=104.5)  # at/above the 2:1 target
    factory = lambda: ad  # noqa: E731

    # T1: ENTERED detects the first target -> SCALING_OUT.
    lr.tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    assert sess.state == "live_scaling_out"

    # T2: SCALING_OUT sells the configured fraction, balance stop -> breakeven (entry),
    # state -> TRAILING with the runner held.
    lr.tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    le = _le(sess)
    pos = le.get("position")
    assert sess.state == "live_trailing"
    assert pos is not None, "runner must still be held (NOT flattened)"
    assert pos["quantity"] == pytest.approx(0.5)       # sold half, half runs
    assert pos["partial_taken"] is True
    assert pos["stop_price"] == pytest.approx(100.0)   # balance stop moved to entry
    realized_after_partial = float(le["realized_pnl_usd"])
    assert realized_after_partial == pytest.approx(2.25)  # (104.5-100)*0.5

    # T3: price runs to 110 -> the chandelier trail ratchets the runner stop UP.
    # Expected level derives from the SAME shared helper + the variant's real
    # stop_atr_mult (the parity contract), off the frozen entry ATR (0.02).
    _variant = variant_for_id(db, vid)
    _mult = float(normalize_strategy_params(_variant.params_json, family_id=_variant.family)["stop_atr_mult"])
    expected_trail = pe.runner_trail_stop(
        high_water_mark=110.0, atr_pct=0.02, stop_atr_mult=_mult,
        breakeven_floor=100.0, current_stop=100.0,
    )
    ad.set_bid(110.0)
    lr.tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    le = _le(sess)
    assert sess.state == "live_trailing"
    assert expected_trail > 100.0  # ratcheted above breakeven
    assert le["position"]["stop_price"] == pytest.approx(expected_trail)
    assert le["position"]["high_water_mark"] == pytest.approx(110.0)

    # T4: pullback to 108 trips the trailed runner stop -> exit.
    ad.set_bid(108.0)
    lr.tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    le = _le(sess)
    assert sess.state == "live_exited"
    total_realized = float(le["realized_pnl_usd"])
    # partial (2.25) + runner (108-100)*0.5 = 4.0  ->  6.25
    assert total_realized == pytest.approx(6.25)

    # THE THESIS: the asymmetric exit beat a flat 2:1 exit that sells 100% at target.
    flat_2to1_pnl = (104.0 - 100.0) * 1.0  # 4.0
    assert total_realized > flat_2to1_pnl
    assert le["last_exit_reason"] == "trail_stop"


# ---------------------------------------------------------------------------
# 4. Paper integration: parity with live (synthetic fills)
# ---------------------------------------------------------------------------

def _benign_ohlcv():
    import pandas as pd

    # Short frame -> swing-low confirm returns None -> BOS never fires in the test.
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 0.2 for c in closes],
            "Low": [c - 0.2 for c in closes],
            "Close": closes,
            "Volume": [1000.0] * len(closes),
        }
    )


def test_paper_first_target_scales_out_moves_to_breakeven_and_runner_captures_upside(
    monkeypatch, db: Session
):
    import app.services.trading.momentum_neural.paper_runner as prun

    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.5)
    monkeypatch.setattr(prun, "runner_boundary_risk_ok", lambda *a, **k: (True, {}))
    monkeypatch.setattr(prun, "fetch_ohlcv_df", lambda *a, **k: _benign_ohlcv())

    vid, _ = _seed_live_eligible_row(db, symbol="PRUN-USD")
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "PRUN-USD", MomentumSymbolViability.variant_id == vid)
        .one()
    )
    via.viability_score = 0.9
    via.paper_eligible = True
    # Zero costs so the PnL math is clean (exit_px == bid).
    via.execution_readiness_json = {"spread_bps": 0.0, "slippage_estimate_bps": 0.0, "fee_to_target_ratio": 0.0}
    db.commit()

    uid = _uid(db, "paper")
    opened = datetime.now(timezone.utc).isoformat()
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="PRUN-USD",
        variant_id=vid,
        mode="paper",
        state="entered",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_policy_caps": {"max_hold_seconds": 86400, "max_notional_per_trade_usd": 1000.0},
            "momentum_paper_execution": {
                "position": {
                    "side": "long",
                    "entry_price": 100.0,
                    "quantity": 1.0,
                    "original_quantity": 1.0,
                    "notional_usd": 100.0,
                    "opened_at_utc": opened,
                    "stop_price": 98.0,
                    "target_price": 104.0,
                    "high_water_mark": 100.0,
                    "entry_atr_pct": 0.02,
                    "fees_est_usd": 0.0,
                },
            },
        },
        correlation_id="c-asym-paper",
    )
    db.commit()

    price = {"v": 104.5}
    quote_fn = lambda _sym: {"mid": price["v"], "bid": price["v"], "ask": price["v"], "source": "test"}  # noqa: E731

    def _pe(s):
        return (s.risk_snapshot_json or {}).get("momentum_paper_execution") or {}

    # T1: ENTERED detects the first target -> SCALING_OUT.
    prun.tick_paper_session(db, sess.id, quote_fn=quote_fn)
    db.commit()
    db.refresh(sess)
    assert sess.state == "scaling_out"

    # T2: scale out the fraction, stop -> breakeven, hold runner -> TRAILING.
    prun.tick_paper_session(db, sess.id, quote_fn=quote_fn)
    db.commit()
    db.refresh(sess)
    pos = _pe(sess).get("position")
    assert sess.state == "trailing"
    assert pos is not None
    assert pos["quantity"] == pytest.approx(0.5)
    assert pos["partial_taken"] is True
    assert pos["stop_price"] == pytest.approx(100.0)
    assert float(_pe(sess)["realized_pnl_usd"]) == pytest.approx(2.25)

    # T3: run to 110 -> chandelier ratchets the runner stop up. Expected level
    # derives from the SAME shared helper + the variant's real stop_atr_mult.
    _variant = variant_for_id(db, vid)
    _mult = float(normalize_strategy_params(_variant.params_json, family_id=_variant.family)["stop_atr_mult"])
    expected_trail = pe.runner_trail_stop(
        high_water_mark=110.0, atr_pct=0.02, stop_atr_mult=_mult,
        breakeven_floor=100.0, current_stop=100.0,
    )
    price["v"] = 110.0
    prun.tick_paper_session(db, sess.id, quote_fn=quote_fn)
    db.commit()
    db.refresh(sess)
    assert sess.state == "trailing"
    assert expected_trail > 100.0
    assert _pe(sess)["position"]["stop_price"] == pytest.approx(expected_trail)

    # T4: pullback to 108 trips the trailed runner stop -> exit.
    price["v"] = 108.0
    prun.tick_paper_session(db, sess.id, quote_fn=quote_fn)
    db.commit()
    db.refresh(sess)
    pe_state = _pe(sess)
    assert sess.state == "exited"
    total_realized = float(pe_state["realized_pnl_usd"])
    assert total_realized == pytest.approx(6.25)
    # THE THESIS: beat the flat 2:1 exit (sell 100% at target = 4.0).
    assert total_realized > (104.0 - 100.0) * 1.0
    assert pe_state["last_exit_reason"] == "trail_stop"


def _assert_full_cycle_learning(
    db: Session,
    *,
    packet: TradingDecisionPacket,
    session_id: int,
    variant_id: int,
    mode: str,
    expected_pnl: float,
) -> None:
    db.refresh(packet)
    assert packet.research_vs_live_context_json["realized_pnl_usd"] == pytest.approx(
        expected_pnl
    )
    session_state = _deployment_state(
        db,
        scope_type="automation_session",
        scope_key=f"session:{session_id}",
    )
    variant_state = _deployment_state(
        db,
        scope_type="strategy_variant",
        scope_key=f"variant:{variant_id}",
    )
    count_attr = "live_trade_count" if mode == "live" else "paper_trade_count"
    assert getattr(session_state, count_attr) == 1
    assert session_state.rolling_expectancy_net == pytest.approx(expected_pnl)
    assert getattr(variant_state, count_attr) == 1
    assert variant_state.rolling_expectancy_net == pytest.approx(expected_pnl)


def _assert_cycle_learning_gap(
    db: Session,
    *,
    packet: TradingDecisionPacket,
    session_id: int,
    event_type: str,
    expected_reason: str,
) -> None:
    db.refresh(packet)
    assert "realized_pnl_usd" not in dict(
        packet.research_vs_live_context_json or {}
    )
    assert (
        db.query(TradingDeploymentState)
        .filter(
            TradingDeploymentState.scope_key == f"session:{session_id}"
        )
        .count()
        == 0
    )
    gap = (
        db.query(TradingAutomationEvent)
        .filter(
            TradingAutomationEvent.session_id == session_id,
            TradingAutomationEvent.event_type == event_type,
        )
        .one()
    )
    assert gap.payload_json["reason_code"] == expected_reason


def _paper_fill(
    sess,
    packet: TradingDecisionPacket,
    *,
    fill_type: str,
    quantity: float,
    price: float,
    after: str,
    pnl_usd: float | None = None,
    fees_usd: float | None = None,
    reason: str,
) -> TradingAutomationSimulatedFill:
    return TradingAutomationSimulatedFill(
        session_id=int(sess.id),
        symbol=sess.symbol,
        lane="simulation",
        side="long",
        action="enter_long" if fill_type == "entry" else "exit_long",
        fill_type=fill_type,
        quantity=quantity,
        price=price,
        reference_price=price,
        fees_usd=fees_usd,
        pnl_usd=pnl_usd,
        position_state_before="flat" if fill_type == "entry" else "long",
        position_state_after=after,
        reason=reason,
        marker_json={"entry": 100.0},
        decision_packet_id=int(packet.id),
    )


def test_live_scale_out_learning_uses_partial_plus_runner_pnl(
    monkeypatch, db: Session
) -> None:
    import app.services.trading.momentum_neural.live_runner as lr

    monkeypatch.setattr(settings, "brain_enable_deployment_ladder", True)
    vid, _ = _seed_live_eligible_row(db, symbol="LRN-USD")
    uid = _uid(db, "live-learning")
    packet = _seed_packet(db, user_id=uid, symbol="LRN-USD", mode="live")
    opened = datetime.now(timezone.utc).isoformat()
    snapshot = _live_pos_snapshot(opened)
    le = snapshot["momentum_live_execution"]
    le["entry_decision_packet_id"] = int(packet.id)
    le["realized_pnl_usd"] = 6.25
    le["position"]["quantity"] = 0.5
    le["position"]["partial_taken"] = True
    le["position"]["trade_realized_usd"] = 6.25
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="LRN-USD",
        variant_id=vid,
        mode="live",
        state="live_trailing",
        risk_snapshot_json=snapshot,
        correlation_id="c-learning-live",
    )
    db.flush()

    final_runner_pnl = lr._complete_confirmed_live_exit(
        db,
        sess,
        le=le,
        quantity=0.5,
        entry_price=100.0,
        fill_price=98.0,
        reason="trail_stop",
        slip_bps=0.0,
    )
    db.flush()

    assert final_runner_pnl == pytest.approx(-1.0)
    assert float(le["realized_pnl_usd"]) == pytest.approx(5.25)
    assert le["g4_prior_trade"]["was_loss"] is False
    assert le["post_exit_excursion_pending"]["realized_pnl"] == pytest.approx(
        5.25
    )
    _assert_full_cycle_learning(
        db,
        packet=packet,
        session_id=int(sess.id),
        variant_id=int(sess.variant_id),
        mode="live",
        expected_pnl=5.25,
    )


def test_captured_alpaca_learning_uses_verified_settlement_pnl_without_broker_io(
    monkeypatch, db: Session
) -> None:
    import app.services.trading.momentum_neural.live_runner as lr

    monkeypatch.setattr(settings, "brain_enable_deployment_ladder", True)
    vid, _ = _seed_live_eligible_row(db, symbol="CAPT")
    uid = _uid(db, "captured-learning")
    packet = _seed_packet(db, user_id=uid, symbol="CAPT", mode="live")
    snapshot = _live_pos_snapshot(datetime.now(timezone.utc).isoformat())
    le = snapshot["momentum_live_execution"]
    le["entry_decision_packet_id"] = int(packet.id)
    le[lr.KEY_ADAPTIVE_RISK_RESERVATION_REQUEST] = {"sealed": True}
    le[lr.KEY_ADAPTIVE_ALPACA_LIFECYCLE] = {
        "reservation_id": str(uuid.uuid4())
    }
    le["position"]["quantity"] = 0.5
    le["position"]["partial_taken"] = True
    le["position"]["trade_realized_usd"] = 2.25
    le["realized_pnl_usd"] = 2.25
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        venue="alpaca",
        execution_family="alpaca_spot",
        symbol="CAPT",
        variant_id=vid,
        mode="live",
        state="live_trailing",
        risk_snapshot_json=snapshot,
        correlation_id="c-learning-captured",
    )
    packet.automation_session_id = int(sess.id)
    closed_state = SimpleNamespace(state="closed")

    class _Store:
        def __init__(self) -> None:
            self.reads = 0

        def read_state(self, *_args, **_kwargs):
            self.reads += 1
            return closed_state

    store = _Store()
    settlement_row = SimpleNamespace(
        settlement_sha256="a" * 64,
        net_realized_pnl_usd=9.75,
    )
    settlement_calls: list[uuid.UUID] = []
    monkeypatch.setattr(
        lr, "load_adaptive_risk_reservation_request", lambda _payload: object()
    )
    monkeypatch.setattr(
        lr, "_adaptive_risk_store_for_session", lambda _sess: store
    )
    monkeypatch.setattr(
        lr, "_adaptive_alpaca_refresh_binding", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        lr,
        "_settle_adaptive_alpaca_cycle_if_complete",
        lambda _sess, _le, *, reservation_id: (
            settlement_calls.append(reservation_id)
            or {
                "ok": True,
                "settlement": SimpleNamespace(row=settlement_row),
            }
        ),
    )

    terminal_leg_pnl = lr._complete_confirmed_live_exit(
        db,
        sess,
        le=le,
        quantity=0.5,
        entry_price=100.0,
        fill_price=108.0,
        reason="trail_stop",
        slip_bps=0.0,
        adapter=object(),
    )
    db.flush()

    assert terminal_leg_pnl == pytest.approx(4.0)
    assert store.reads == 2
    assert len(settlement_calls) == 1
    assert le["realized_pnl_usd"] == pytest.approx(9.75)
    assert le["alpaca_settled_session_pnl_usd"] == pytest.approx(9.75)
    assert (
        le["post_exit_excursion_pending"]["realized_pnl"]
        == pytest.approx(9.75)
    )
    assert (
        le["post_exit_excursion_pending"]["settlement_sha256"]
        == "a" * 64
    )
    _assert_full_cycle_learning(
        db,
        packet=packet,
        session_id=int(sess.id),
        variant_id=int(sess.variant_id),
        mode="live",
        expected_pnl=9.75,
    )
    session_state = _deployment_state(
        db,
        scope_type="automation_session",
        scope_key=f"session:{int(sess.id)}",
    )
    assert session_state.stage_metrics_json["session_pnl_last_usd"] == pytest.approx(
        9.75
    )


@pytest.mark.parametrize("bad_partial_pnl", [None, "not-a-number", math.nan])
def test_live_partial_learning_gap_never_fabricates_zero_accumulator(
    monkeypatch, db: Session, bad_partial_pnl
) -> None:
    import app.services.trading.momentum_neural.live_runner as lr

    monkeypatch.setattr(settings, "brain_enable_deployment_ladder", True)
    vid, _ = _seed_live_eligible_row(db, symbol="LGAP-USD")
    uid = _uid(db, f"live-gap-{bad_partial_pnl!s}")
    packet = _seed_packet(db, user_id=uid, symbol="LGAP-USD", mode="live")
    snapshot = _live_pos_snapshot(datetime.now(timezone.utc).isoformat())
    le = snapshot["momentum_live_execution"]
    le["entry_decision_packet_id"] = int(packet.id)
    le["position"]["quantity"] = 0.5
    le["position"]["partial_taken"] = True
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="LGAP-USD",
        variant_id=vid,
        mode="live",
        state="live_trailing",
        risk_snapshot_json=snapshot,
        correlation_id=f"c-learning-gap-{uid}",
    )
    packet.automation_session_id = int(sess.id)
    if bad_partial_pnl is not None:
        # Inject after the durable JSON snapshot is flushed. PostgreSQL rejects
        # NaN JSON outright; this exercises a corrupt/malformed in-memory state.
        le["position"]["trade_realized_usd"] = bad_partial_pnl

    terminal_leg_pnl = lr._complete_confirmed_live_exit(
        db,
        sess,
        le=le,
        quantity=0.5,
        entry_price=100.0,
        fill_price=108.0,
        reason="trail_stop",
        slip_bps=0.0,
    )
    db.flush()

    assert terminal_leg_pnl == pytest.approx(4.0)
    assert le["position"] is None
    _assert_cycle_learning_gap(
        db,
        packet=packet,
        session_id=int(sess.id),
        event_type="live_cycle_learning_gap",
        expected_reason="live_partial_cycle_pnl_unavailable",
    )


@pytest.mark.parametrize(
    ("malformation", "expected_reason"),
    [
        (None, None),
        ("wrong_order", "paper_cycle_state_chain_mismatch"),
        ("unexpected_fill", "paper_cycle_fill_contract_mismatch"),
        (
            "duplicate_terminal",
            "paper_cycle_terminal_exit_count_mismatch",
        ),
        ("quantity_mismatch", "paper_cycle_exit_quantity_mismatch"),
        ("invalid_state_chain", "paper_cycle_state_chain_mismatch"),
        ("packet_binding", "paper_cycle_packet_binding_mismatch"),
    ],
)
def test_paper_scale_out_learning_uses_complete_packet_keyed_cycle(
    monkeypatch, db: Session, malformation: str | None, expected_reason: str | None
) -> None:
    import app.services.trading.momentum_neural.paper_runner as prun

    monkeypatch.setattr(settings, "brain_enable_deployment_ladder", True)
    vid, _ = _seed_live_eligible_row(db, symbol="PLRN-USD")
    uid = _uid(db, "paper-learning")
    packet = _seed_packet(db, user_id=uid, symbol="PLRN-USD", mode="paper")
    pe_state = {
        "last_entry_decision_packet_id": int(packet.id),
        "realized_pnl_usd": 1.0,
    }
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="PLRN-USD",
        variant_id=vid,
        mode="paper",
        state="exited",
        risk_snapshot_json={"momentum_paper_execution": pe_state},
        correlation_id="c-learning-paper",
    )
    packet.automation_session_id = int(sess.id)
    rows = [
        _paper_fill(
            sess,
            packet,
            fill_type="entry",
            quantity=1.0,
            price=100.0,
            after="long",
            reason="entry",
        ),
        _paper_fill(
            sess,
            packet,
            fill_type="exit",
            quantity=0.25,
            price=104.5,
            pnl_usd=1.0,
            fees_usd=0.125,
            after="long",
            reason="scale_out_target_1",
        ),
        _paper_fill(
            sess,
            packet,
            fill_type="exit",
            quantity=0.25,
            price=105.0,
            pnl_usd=1.125,
            fees_usd=0.125,
            after="long",
            reason="scale_out_target_2",
        ),
        _paper_fill(
            sess,
            packet,
            fill_type="exit",
            quantity=0.5,
            price=98.0,
            pnl_usd=-1.125,
            fees_usd=0.125,
            after="flat",
            reason="trail_stop",
        ),
    ]
    if malformation == "wrong_order":
        rows[2], rows[3] = rows[3], rows[2]
    elif malformation == "unexpected_fill":
        rows[1].fill_type = "cancel"
        rows[1].action = "cancel"
    elif malformation == "duplicate_terminal":
        rows[1].position_state_after = "flat"
    elif malformation == "quantity_mismatch":
        rows[3].quantity = 0.4
    elif malformation == "invalid_state_chain":
        rows[1].position_state_before = "flat"
    elif malformation == "packet_binding":
        packet.execution_mode = "live"
    db.add_all(rows)
    db.flush()

    prun._finalize_paper_decision_after_exit(
        db,
        sess,
        pe=pe_state,
        realized_pnl_usd=-1.125,
        slip_bps=0.0,
    )
    db.flush()

    if malformation is not None:
        _assert_cycle_learning_gap(
            db,
            packet=packet,
            session_id=int(sess.id),
            event_type="paper_cycle_learning_gap",
            expected_reason=str(expected_reason),
        )
        return

    _assert_full_cycle_learning(
        db,
        packet=packet,
        session_id=int(sess.id),
        variant_id=int(sess.variant_id),
        mode="paper",
        expected_pnl=1.0,
    )


def test_paper_learning_gap_never_falls_back_to_terminal_tranche(
    monkeypatch, db: Session
) -> None:
    import app.services.trading.momentum_neural.paper_runner as prun

    monkeypatch.setattr(settings, "brain_enable_deployment_ladder", True)
    vid, _ = _seed_live_eligible_row(db, symbol="GAP-USD")
    uid = _uid(db, "paper-gap")
    packet = _seed_packet(db, user_id=uid, symbol="GAP-USD", mode="paper")
    pe_state = {
        "last_entry_decision_packet_id": int(packet.id),
        "realized_pnl_usd": 4.0,
    }
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="GAP-USD",
        variant_id=vid,
        mode="paper",
        state="exited",
        risk_snapshot_json={"momentum_paper_execution": pe_state},
        correlation_id="c-learning-gap",
    )
    packet.automation_session_id = int(sess.id)
    db.add(
        _paper_fill(
            sess,
            packet,
            fill_type="exit",
            quantity=0.5,
            price=108.0,
            pnl_usd=4.0,
            after="flat",
            reason="trail_stop",
        )
    )
    db.flush()

    prun._finalize_paper_decision_after_exit(
        db,
        sess,
        pe=pe_state,
        realized_pnl_usd=4.0,
        slip_bps=0.0,
    )
    db.flush()

    _assert_cycle_learning_gap(
        db,
        packet=packet,
        session_id=int(sess.id),
        event_type="paper_cycle_learning_gap",
        expected_reason="paper_cycle_entry_count_mismatch",
    )
