"""Live-runner integration for the adaptive order-flow EXHAUSTION LOCK.

These assert the call-site invariants the pure-helper tests cannot:

  * EQUITY BYTE-IDENTICAL: with the lock flag ON and an ``exhaustion_lock_partial_armed``
    flag injected into ``le``, an equity (non ``-USD``) session takes the IDENTICAL
    decision as lock-off — the secondary hook is gated DIRECTLY on ``-USD`` (not
    transitively via the flag), so a stale/forged flag can never flip equity.
  * CRYPTO early partial: an armed crypto runner routes through the SAME audited
    SCALING_OUT -> _apply_confirmed_live_partial_exit -> breakeven path (the MEGA
    give-back fix) when ``..._partial_enabled`` is on.
  * NO-DOUBLE-EXIT: while a resting scale-out limit is working the level
    (``scale_limit_order_id`` set), the armed partial does NOT fire — the
    resting-limit path owns the de-risk that tick.

Reuses the live integration harness (``_FakeAdapter``, seeding) from the
asymmetric-exit suite.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.trading import MomentumSymbolViability
from app.services.trading.momentum_neural.persistence import create_trading_automation_session

from tests.test_momentum_asymmetric_exit import _FakeAdapter, _le, _uid
from tests.test_momentum_paper_runner import _seed_live_eligible_row


def _pos_snapshot(opened_iso: str, product_id: str) -> dict:
    from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY

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
            # pre-armed runner past the trail-activate, partial NOT yet taken,
            # well BELOW the fixed target (the MEGA shape).
            "exhaustion_lock_partial_armed": True,
            "position": {
                "product_id": product_id,
                "side": "long",
                "quantity": 1.0,
                "original_quantity": 1.0,
                "avg_entry_price": 100.0,
                "notional_usd": 100.0,
                "opened_at_utc": opened_iso,
                "high_water_mark": 103.0,   # +1.5R peak (risk 2.0) — below the 104 target
                "stop_price": 98.0,
                "target_price": 104.0,
                "partial_taken": False,
            },
        },
    }


def _arm_session(db: Session, monkeypatch, *, symbol: str, lr):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.5)
    monkeypatch.setattr(lr, "runner_boundary_risk_ok", lambda *a, **k: (True, {}))
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)
    # Deterministic: the _FakeAdapter is enabled, but the per-venue connectivity
    # preflight (coinbase/robinhood is_connected) is env-flaky — force-connect.
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda ef: True)
    # No live ring in tests: the lock helper no-ops on (None, None); the partial
    # fires off the armed flag. Also avoid the 5m-EMA network fetch.
    import app.services.trading.momentum_neural.pipeline as _pl
    import app.services.trading.market_data as _md
    monkeypatch.setattr(_pl, "_live_ofi_microprice", lambda *a, **k: (None, None))
    monkeypatch.setattr(_md, "fetch_ohlcv_df", lambda *a, **k: None, raising=False)

    ef = "coinbase_spot" if symbol.endswith("-USD") else "robinhood_spot"
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == symbol, MomentumSymbolViability.variant_id == vid)
        .one()
    )
    via.viability_score = 0.9
    via.live_eligible = True
    db.commit()

    uid = _uid(db, symbol.replace("-", "_"))
    opened = datetime.now(timezone.utc).isoformat()
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol=symbol,
        variant_id=vid,
        mode="live",
        state="live_trailing",  # already a runner
        execution_family=ef,
        risk_snapshot_json=_pos_snapshot(opened, symbol),
        correlation_id=f"c-ofi-{symbol}",
    )
    db.commit()
    return sess


def test_equity_armed_flag_is_byte_identical_no_ofi_partial(monkeypatch, db: Session):
    """Equity session: lock flag + partial flag ON, armed flag forged into le.
    The -USD direct gate means the OFI partial NEVER fires for equity — the bid
    (103, below the 104 target) leaves the session in TRAILING, unchanged."""
    import app.services.trading.momentum_neural.live_runner as lr

    monkeypatch.setattr(settings, "chili_momentum_exit_ofi_lock_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_exit_ofi_lock_partial_enabled", True)

    sess = _arm_session(db, monkeypatch, symbol="RUN", lr=lr)  # EQUITY (no -USD)
    ad = _FakeAdapter(bid=103.0)  # below the 104 target
    lr.tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    # Equity must NOT have transitioned to scaling_out off the forged armed flag.
    assert sess.state == "live_trailing"
    le = _le(sess)
    assert le.get("position", {}).get("partial_taken") in (False, None)
    # the forged flag is irrelevant for equity — still present, never consumed
    assert le.get("exhaustion_lock_partial_armed") is True


def test_crypto_armed_flag_fires_early_partial_through_scale_out(monkeypatch, db: Session):
    """Crypto session: armed flag + flags ON -> early partial fires below the
    target, routes through SCALING_OUT -> breakeven (the MEGA give-back fix)."""
    import app.services.trading.momentum_neural.live_runner as lr

    monkeypatch.setattr(settings, "chili_momentum_exit_ofi_lock_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_exit_ofi_lock_partial_enabled", True)

    sess = _arm_session(db, monkeypatch, symbol="RUN-USD", lr=lr)  # CRYPTO
    ad = _FakeAdapter(bid=103.0)  # below the 104 target — only the OFI partial can fire

    # T1: the armed flag (crypto + flag on) triggers the partial -> SCALING_OUT.
    lr.tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    assert sess.state == "live_scaling_out"
    # the one-tick flag is consumed
    assert _le(sess).get("exhaustion_lock_partial_armed") is None

    # T2: SCALING_OUT books the partial, balance stop -> breakeven, holds runner.
    lr.tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    le = _le(sess)
    pos = le.get("position")
    assert sess.state == "live_trailing"
    assert pos is not None and pos["partial_taken"] is True
    assert pos["quantity"] == pytest.approx(0.5)
    assert pos["stop_price"] == pytest.approx(100.0)  # breakeven — the give-back fix
    # partial PnL booked through the audited path: (103-100)*0.5 = 1.5
    assert float(le["realized_pnl_usd"]) == pytest.approx(1.5)


def test_crypto_armed_partial_does_not_fire_while_scale_limit_resting(monkeypatch, db: Session):
    """No-double-exit: a resting scale-out limit owns the level this tick — the
    armed OFI partial must NOT transition to SCALING_OUT while it is working."""
    import app.services.trading.momentum_neural.live_runner as lr

    monkeypatch.setattr(settings, "chili_momentum_exit_ofi_lock_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_exit_ofi_lock_partial_enabled", True)

    sess = _arm_session(db, monkeypatch, symbol="RUN-USD", lr=lr)
    # plant a resting scale-out limit that is still OPEN (not filled)
    snap = dict(sess.risk_snapshot_json)
    mle = dict(snap["momentum_live_execution"])
    mle["scale_limit_order_id"] = "resting-1"
    mle["scale_limit_px"] = 104.0
    snap["momentum_live_execution"] = mle
    sess.risk_snapshot_json = snap
    db.commit()

    class _OpenLimitAdapter(_FakeAdapter):
        def get_order(self, order_id):
            from app.services.trading.venue.protocol import NormalizedOrder, FreshnessMeta
            fresh = FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=120.0)
            # resting limit still OPEN with no fills
            return (
                NormalizedOrder(
                    order_id=str(order_id), client_order_id="c", product_id="RUN-USD",
                    side="sell", status="OPEN", order_type="limit",
                    filled_size=0.0, average_filled_price=0.0,
                ),
                fresh,
            )

    ad = _OpenLimitAdapter(bid=103.0)  # below target so only the OFI partial could fire
    lr.tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    # The resting-limit guard (`not scale_limit_order_id`) blocks the armed partial.
    assert sess.state == "live_trailing"
    le = _le(sess)
    assert le.get("position", {}).get("partial_taken") in (False, None)
    # the armed flag is still pending (not consumed by a transition this tick)
    assert le.get("exhaustion_lock_partial_armed") is True
