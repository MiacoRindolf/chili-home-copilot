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
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import MomentumSymbolViability
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
