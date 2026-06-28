"""Phase 8: live automation runner (guarded Coinbase adapter path)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_ENTERED,
    STATE_LIVE_ERROR,
    STATE_LIVE_EXITED,
    STATE_LIVE_PENDING_ENTRY,
    STATE_LIVE_SCALING_OUT,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
    assert_transition_live,
    can_transition_live,
)
from app.services.trading.momentum_neural.live_runner import (
    _adaptive_live_max_spread_bps,
    _expected_move_bps_from_ohlcv,
    _notional_guard_multiplier,
    _quote_quality_block,
    list_runnable_live_sessions,
    summarize_live_execution,
    tick_live_session,
)
from app.services.trading.momentum_neural.paper_runner import list_runnable_paper_sessions
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.services.trading.venue.coinbase_spot import reset_duplicate_client_order_guard_for_tests
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedOrder, NormalizedProduct, NormalizedTicker

from tests.test_momentum_paper_runner import _seed_live_eligible_row


def _uid(db: Session, name_suffix: str) -> int:
    u = User(name=f"LiveRun_{name_suffix}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


def _fresh() -> FreshnessMeta:
    return FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=120.0)


@pytest.fixture(autouse=True)
def _venue_connected_by_default(monkeypatch):
    """The #565 venue-connectivity preflight (``live_runner._venue_broker_connected``)
    short-circuits the tick with ``venue_broker_not_connected`` whenever the broker is
    not connected — which is ALWAYS the case in the test env (no live creds). That
    preflight was added AFTER these tick-logic tests were written, so without this
    default they skip BEFORE the state-transition / kill-switch / order-adoption logic
    they intend to exercise (8 stale failures — NOT a production bug; in prod the venue
    IS connected so the preflight passes). Default it to CONNECTED; the disconnected-
    skip test (``test_tick_skips_disconnected_venue``) overrides this back to False."""
    import app.services.trading.momentum_neural.live_runner as _lr
    monkeypatch.setattr(_lr, "_venue_broker_connected", lambda ef: True)


def _mk_adapter():
    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="SOL-USD",
            bid=99.95,
            ask=100.05,
            mid=100.0,
            spread_bps=10.0,
            freshness=_fresh(),
        ),
        _fresh(),
    )
    prod = NormalizedProduct(
        product_id="SOL-USD",
        base_currency="SOL",
        quote_currency="USD",
        status="online",
        trading_disabled=False,
        cancel_only=False,
        limit_only=False,
        post_only=False,
        auction_mode=False,
        base_increment=0.001,
        base_min_size=0.001,
    )
    ad.get_product.return_value = (prod, _fresh())
    ad.place_market_order.return_value = {"ok": True, "order_id": "ord-entry-1", "client_order_id": "cid-e1"}
    # Momentum entries are now marketable-LIMIT orders (sweep-protected); mirror the envelope.
    ad.place_limit_order_gtc.return_value = {"ok": True, "order_id": "ord-entry-1", "client_order_id": "cid-e1"}
    ad.get_order.return_value = (
        NormalizedOrder(
            order_id="ord-entry-1",
            client_order_id="cid-e1",
            product_id="SOL-USD",
            side="buy",
            status="FILLED",
            order_type="market",
            filled_size=0.25,
            average_filled_price=100.5,
        ),
        _fresh(),
    )
    ad.cancel_order.return_value = {"ok": True, "raw": {}}
    return ad


def test_live_fsm_transition_rules() -> None:
    assert can_transition_live("armed_pending_runner", STATE_QUEUED_LIVE)
    assert not can_transition_live(STATE_QUEUED_LIVE, "armed_pending_runner")
    assert can_transition_live(STATE_LIVE_PENDING_ENTRY, STATE_LIVE_ENTERED)
    assert not can_transition_live(STATE_QUEUED_LIVE, STATE_LIVE_ENTERED)
    with pytest.raises(ValueError):
        assert_transition_live(STATE_QUEUED_LIVE, "armed_pending_runner")


def test_summarize_live_execution_helpers() -> None:
    assert summarize_live_execution({}) == {}
    assert summarize_live_execution(None) == {}  # type: ignore[arg-type]
    snap = {
        "momentum_live_execution": {
            "tick_count": 2,
            "entry_order_id": "o1",
            "last_exit_intent": {"reason": "stop"},
            "exit_execution_intents": [{"reason": "stop"}],
            "pending_exit_reason": "stop",
            "pending_exit_quantity": 0.1,
            "last_exit_pending_confirmation": {"why": "exit_fill_pending"},
            "last_partial_exit_reason": "target",
            "last_partial_exit_price": 51.0,
            "last_exit_notional_basis_usd": 5.0,
            "last_exit_return_bps": 200.0,
            "last_partial_exit_notional_basis_usd": 1.0,
            "last_partial_exit_return_bps": 100.0,
            "position": {"quantity": 0.1, "avg_entry_price": 50.0, "notional_usd": 5.0},
        }
    }
    s = summarize_live_execution(snap)
    assert s.get("tick_count") == 2
    assert s.get("in_position") is True
    assert s.get("avg_entry_price") == 50.0
    assert s.get("last_exit_intent") == {"reason": "stop"}
    assert s.get("exit_execution_intent_count") == 1
    assert s.get("pending_exit_reason") == "stop"
    assert s.get("pending_exit_quantity") == 0.1
    assert s.get("last_exit_pending_confirmation") == {"why": "exit_fill_pending"}
    assert s.get("last_partial_exit_reason") == "target"
    assert s.get("last_partial_exit_price") == 51.0
    assert s.get("last_exit_notional_basis_usd") == 5.0
    assert s.get("last_exit_return_bps") == 200.0
    assert s.get("last_partial_exit_notional_basis_usd") == 1.0
    assert s.get("last_partial_exit_return_bps") == 100.0


def test_quote_quality_block_preserves_zero_live_spread_cap(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_max_spread_bps_live", 0.0)
    fresh = _fresh()

    gate = _quote_quality_block(
        NormalizedTicker(
            product_id="SOL-USD",
            bid=99.995,
            ask=100.005,
            mid=100.0,
            spread_bps=1.0,
            freshness=fresh,
        ),
        fresh,
    )

    assert gate is not None
    assert gate["reason"] == "wide_bbo_spread"
    assert gate["spread_bps"] == 1.0
    assert gate["max_spread_bps"] == 0.0


def test_notional_guard_multiplier_preserves_zero_bps(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_order_notional_guard_bps", 0.0)

    assert _notional_guard_multiplier() == 1.0


def test_adaptive_max_spread_bps_floor_and_loosening() -> None:
    from app.services.trading.momentum_neural.risk_policy import adaptive_max_spread_bps

    base = 12.0
    # Unknown / non-finite / non-positive expected move -> base floor (no loosen).
    assert adaptive_max_spread_bps(base, None, 0.5) == base
    assert adaptive_max_spread_bps(base, 0.0, 0.5) == base
    assert adaptive_max_spread_bps(base, -5.0, 0.5) == base
    assert adaptive_max_spread_bps(base, float("nan"), 0.5) == base
    # Bad ratio -> base floor.
    assert adaptive_max_spread_bps(base, 400.0, 0.0) == base
    assert adaptive_max_spread_bps(base, 400.0, -1.0) == base
    # Low-vol instrument: ratio*move below the floor -> keep the floor (never tighten).
    assert adaptive_max_spread_bps(base, 10.0, 0.5) == base  # 0.5*10 = 5 < 12
    # Explosive instrument: ratio*move above the floor -> loosen proportionally.
    assert adaptive_max_spread_bps(base, 400.0, 0.5) == pytest.approx(200.0)


def test_adaptive_max_spread_bps_absolute_cap() -> None:
    """Ross 'skip if the spread is too wide': the adaptive tolerance never exceeds
    the absolute cap, no matter how explosive the name."""
    from app.services.trading.momentum_neural.risk_policy import adaptive_max_spread_bps

    base = 12.0
    # Explosive name (INHD-like): 0.5*1678 = 839 bps uncapped; the cap holds it to 300.
    assert adaptive_max_spread_bps(base, 1678.0, 0.5) == pytest.approx(839.0)
    assert adaptive_max_spread_bps(base, 1678.0, 0.5, abs_cap_bps=300.0) == pytest.approx(300.0)
    # Below the cap -> unaffected.
    assert adaptive_max_spread_bps(base, 400.0, 0.5, abs_cap_bps=300.0) == pytest.approx(200.0)
    # The cap never forces tolerance BELOW the documented floor.
    assert adaptive_max_spread_bps(base, 1678.0, 0.5, abs_cap_bps=5.0) == base
    # No cap -> prior behavior preserved.
    assert adaptive_max_spread_bps(base, 1678.0, 0.5, abs_cap_bps=None) == pytest.approx(839.0)


def test_adaptive_live_max_spread_bps_reads_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_max_spread_bps_live", 12.0)
    monkeypatch.setattr(settings, "chili_momentum_risk_spread_to_expected_move_ratio", 0.5)
    assert _adaptive_live_max_spread_bps(None) == 12.0  # no move -> floor
    assert _adaptive_live_max_spread_bps(10.0) == 12.0  # quiet -> floor
    assert _adaptive_live_max_spread_bps(300.0) == pytest.approx(150.0)  # mover -> loosen


def test_expected_move_bps_from_ohlcv() -> None:
    import pandas as pd

    assert _expected_move_bps_from_ohlcv(None) is None
    assert _expected_move_bps_from_ohlcv(pd.DataFrame()) is None
    # Steady ~2% per-bar range around 100 -> ~200 bps expected move (ATR/close).
    price = 100.0
    rows = [
        {"High": price * 1.01, "Low": price * 0.99, "Close": price, "Volume": 1000.0}
        for _ in range(30)
    ]
    em = _expected_move_bps_from_ohlcv(pd.DataFrame(rows))
    assert em is not None
    assert em == pytest.approx(200.0, rel=0.1)


def test_quote_quality_block_adaptive_override_allows_wide_spread_on_mover() -> None:
    fresh = _fresh()
    tick = NormalizedTicker(
        product_id="MOV-USD",
        bid=99.65,
        ask=100.35,
        mid=100.0,
        spread_bps=70.0,
        freshness=fresh,
    )
    # Base floor (12 bps) blocks a 70 bps spread...
    blocked = _quote_quality_block(tick, fresh, max_spread_bps=12.0)
    assert blocked is not None and blocked["reason"] == "wide_bbo_spread"
    # ...but an adaptive tolerance from a high expected move (0.5 * 300 = 150)
    # lets the explosive mover through.
    assert _quote_quality_block(tick, fresh, max_spread_bps=150.0) is None


def test_live_exit_intent_records_packet_context(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    calls: list[tuple[int | None, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "record_packet_execution_intent",
        lambda _db, packet_id, payload: calls.append((packet_id, payload)),
    )
    sess = SimpleNamespace(
        id=42,
        state=STATE_LIVE_ENTERED,
        symbol="SOL-USD",
        variant_id=7,
        venue="coinbase",
        execution_family="coinbase_spot",
    )
    le = {
        "entry_decision_packet_id": 123,
        "position": {
            "quantity": 0.25,
            "avg_entry_price": 100.0,
            "stop_price": 98.0,
            "target_price": 106.0,
            "opened_at_utc": "2026-01-01T00:00:00",
        },
    }

    live_runner_mod._record_live_exit_intent_safe(
        MagicMock(),
        sess,
        le=le,
        reason="stop",
        product_id="SOL-USD",
        quantity=0.25,
        client_order_id="cid-exit",
        bid=97.5,
        ask=98.0,
        mid=97.75,
        extra={"stop_price": 98.0},
    )

    assert calls and calls[0][0] == 123
    payload = calls[0][1]
    assert payload["surface"] == "momentum_live_runner_exit"
    assert payload["side"] == "sell"
    assert payload["reason"] == "stop"
    assert payload["client_order_id"] == "cid-exit"
    assert payload["reference_notional_usd"] == pytest.approx(24.375)
    assert le["last_exit_intent"]["reason"] == "stop"
    assert le["exit_execution_intents"][-1]["product_id"] == "SOL-USD"


def test_live_exit_submit_failure_does_not_flatten_local_position(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(
        id=43,
        state=STATE_LIVE_ENTERED,
        risk_snapshot_json={},
        correlation_id="corr-exit-fail",
    )
    le = {
        "exit_client_order_id": "cid-exit",
        "position": {
            "quantity": 0.25,
            "avg_entry_price": 100.0,
        },
    }

    ok = live_runner_mod._live_exit_submit_succeeded(
        MagicMock(),
        sess,
        le=le,
        result={"ok": False, "error": "venue_down"},
        reason="stop",
    )

    assert ok is False
    assert le["position"]["quantity"] == 0.25
    assert le["last_exit_submit_failed"]["reason"] == "stop"
    assert sess.risk_snapshot_json["momentum_live_execution"]["position"]["quantity"] == 0.25
    assert events and events[-1][0] == "live_exit_submit_failed"


def test_live_exit_poll_waits_for_open_order(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(id=44, state=STATE_LIVE_ENTERED, risk_snapshot_json={}, correlation_id="corr-pending")
    le = {
        "exit_order_id": "ord-exit-open",
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }
    adapter = SimpleNamespace(
        get_order=lambda _oid: (
            NormalizedOrder(
                order_id="ord-exit-open",
                client_order_id="cid-exit",
                product_id="SOL-USD",
                side="sell",
                status="OPEN",
                order_type="market",
                filled_size=0.0,
                average_filled_price=None,
            ),
            _fresh(),
        )
    )

    out = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="stop",
        quantity=0.25,
    )

    assert out["pending"] is True
    assert le["position"]["quantity"] == 0.25
    assert le["last_exit_pending_confirmation"]["why"] == "exit_fill_pending"
    assert events and events[-1][0] == "live_exit_pending_confirmation"


def test_confirmed_live_exit_is_the_only_flatten_path(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_runner_mod, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(live_runner_mod, "_finalize_live_decision_after_exit", lambda *a, **k: None)
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(
        id=45,
        state=STATE_LIVE_ENTERED,
        mode="live",
        symbol="BTC-USD",
        risk_snapshot_json={},
        correlation_id="corr-confirmed",
    )
    le = {
        "exit_order_id": "ord-exit-filled",
        "pending_exit_reason": "stop",
        "pending_exit_quantity": 0.25,
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }

    pnl = live_runner_mod._complete_confirmed_live_exit(
        MagicMock(),
        sess,
        le=le,
        quantity=0.25,
        entry_price=100.0,
        fill_price=99.0,
        reason="stop",
        slip_bps=6.0,
    )

    assert pnl == pytest.approx(-0.25)
    assert sess.state == STATE_LIVE_EXITED
    assert le["position"] is None
    assert "pending_exit_reason" not in le
    assert le["last_exit_notional_basis_usd"] == pytest.approx(25.0)
    assert le["last_exit_return_bps"] == pytest.approx(-100.0)
    assert sess.risk_snapshot_json["momentum_live_execution"]["last_exit_reason"] == "stop"
    assert events and events[-1][0] == "live_exit_filled"


def test_terminal_partial_live_exit_reduces_position_without_flattening(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_runner_mod, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(
        id=46,
        state=STATE_LIVE_SCALING_OUT,
        mode="live",
        risk_snapshot_json={},
        correlation_id="corr-partial",
    )
    le = {
        "exit_order_id": "ord-exit-partial",
        "pending_exit_reason": "target",
        "pending_exit_quantity": 0.25,
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }

    pnl = live_runner_mod._apply_confirmed_live_partial_exit(
        MagicMock(),
        sess,
        le=le,
        filled_quantity=0.1,
        entry_price=100.0,
        fill_price=101.0,
        reason="target",
    )

    assert pnl == pytest.approx(0.1)
    assert sess.state == STATE_LIVE_SCALING_OUT
    assert le["position"]["quantity"] == pytest.approx(0.15)
    assert "pending_exit_reason" not in le
    assert le["last_partial_exit_notional_basis_usd"] == pytest.approx(10.0)
    assert le["last_partial_exit_return_bps"] == pytest.approx(100.0)
    assert sess.risk_snapshot_json["momentum_live_execution"]["position"]["quantity"] == pytest.approx(0.15)
    assert events and events[-1][0] == "live_partial_exit_filled"


def test_list_runnable_live_ignores_paper(db: Session) -> None:
    from app.models.trading import MomentumStrategyVariant
    from app.services.trading.momentum_neural.persistence import (
        create_trading_automation_session,
        ensure_momentum_strategy_variants,
    )
    from app.services.trading.momentum_neural.paper_fsm import STATE_QUEUED as PQ

    uid = _uid(db, "mix")
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    create_trading_automation_session(
        db,
        user_id=uid,
        symbol="PAP-USD",
        variant_id=v.id,
        mode="paper",
        state=PQ,
        risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
    )
    create_trading_automation_session(
        db,
        user_id=uid,
        symbol="LIV-USD",
        variant_id=v.id,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
    )
    db.commit()
    live_rows = list_runnable_live_sessions(db, limit=50)
    assert all(r.mode == "live" for r in live_rows)
    paper_rows = list_runnable_paper_sessions(db, limit=50)
    assert all(r.mode == "paper" for r in paper_rows)


def test_tick_live_armed_to_watching(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="TL1-USD")
    db.commit()
    uid = _uid(db, "tl1")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="TL1-USD",
        variant_id=vid,
        mode="live",
        state="armed_pending_runner",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True, "evaluated_at_utc": "2026-01-01T00:00:00+00:00"},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 50, "max_hold_seconds": 3600},
        },
        correlation_id="c-live-1",
    )
    db.commit()
    ad = _mk_adapter()

    def factory():
        return ad

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        r1 = tick_live_session(db, sess.id, adapter_factory=factory)
    assert r1.get("ok")
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_QUEUED_LIVE

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        r2 = tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_WATCHING_LIVE


def test_kill_switch_blocks_before_entry(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="KS-USD")
    db.commit()
    uid = _uid(db, "ks")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="KS-USD",
        variant_id=vid,
        mode="live",
        state="armed_pending_runner",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
        },
    )
    db.commit()
    ad = _mk_adapter()

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=True):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    assert out.get("blocked") or sess.state == STATE_LIVE_ERROR
    assert sess.state == STATE_LIVE_ERROR
    ad.place_market_order.assert_not_called()
    ad.place_limit_order_gtc.assert_not_called()


def test_broken_bbo_above_abs_cap_blocks_entry_without_error(monkeypatch, db: Session) -> None:
    """SKIP-FOR-LIMITS (operator 2026-06-23): the momentum entry is a marketable
    LIMIT whose price bounds the fill cost, so a merely-wide-but-LIVE spread is no
    longer a hard veto — it is handled as a sized cost + the bounded limit (see
    ``test_merely_wide_bbo_proceeds_to_limit_entry``). The SURVIVING hard block is the
    BROKEN-QUOTE abs-cap ceiling: a spread WIDER than
    ``chili_momentum_risk_max_spread_bps_abs_cap`` is a halted / toxic book and is
    still rejected with ``wide_bbo_spread`` — the session stays WATCHING_LIVE and no
    order (market OR limit) is placed, without raising."""
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    # Production default: the entry quote gate uses ONLY the broken-quote abs cap as
    # its ceiling. Pin both so the quote below (400bps) is unambiguously a broken book
    # (400 > 300) regardless of any future default change.
    monkeypatch.setattr(settings, "chili_momentum_skip_spread_gate_for_limit_entry", True)
    monkeypatch.setattr(settings, "chili_momentum_risk_max_spread_bps_abs_cap", 300.0)
    vid, _ = _seed_live_eligible_row(db, symbol="WID-USD")
    db.commit()
    uid = _uid(db, "broken")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="WID-USD",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
        },
    )
    db.commit()
    ad = _mk_adapter()
    fresh = _fresh()
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="WID-USD",
            bid=98.0,
            ask=102.0,
            mid=100.0,
            spread_bps=400.0,
            freshness=fresh,
        ),
        fresh,
    )

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    assert out == {"ok": True, "blocked": True, "reason": "wide_bbo_spread"}
    assert sess.state == STATE_WATCHING_LIVE
    ad.place_market_order.assert_not_called()
    ad.place_limit_order_gtc.assert_not_called()
    gate = (sess.risk_snapshot_json or {})["momentum_live_execution"]["last_quote_quality_gate"]
    assert gate["spread_bps"] == 400.0
    assert gate["max_spread_bps"] == 300.0


def test_merely_wide_bbo_proceeds_to_limit_entry(monkeypatch, db: Session) -> None:
    """SKIP-FOR-LIMITS (operator 2026-06-23) new correct behavior: a wide-but-LIVE
    spread BELOW the broken-quote abs cap (200bps < 300bps) is NOT hard-vetoed — the
    Ross low-float movers the lane targets inherently trade wide (the old flat
    wide-spread veto was the equity 0-fills trap). It proceeds to a price-BOUNDED
    marketable LIMIT entry (``place_limit_order_gtc``), so the fill cost is capped by
    the limit price (never a naked market order); the spread is absorbed as a sized
    cost (the derate-only L2.2 multiplier) + the bounded limit. Regression guard
    against re-introducing a flat wide-spread entry veto."""
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_skip_spread_gate_for_limit_entry", True)
    monkeypatch.setattr(settings, "chili_momentum_risk_max_spread_bps_abs_cap", 300.0)
    vid, _ = _seed_live_eligible_row(db, symbol="WID-USD")
    db.commit()
    uid = _uid(db, "merelywide")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="WID-USD",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
        },
    )
    db.commit()
    ad = _mk_adapter()
    fresh = _fresh()
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="WID-USD",
            bid=99.0,
            ask=101.0,
            mid=100.0,
            spread_bps=200.0,
            freshness=fresh,
        ),
        fresh,
    )

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    # NOT hard-blocked on spread — it proceeded past the quote gate to the entry.
    assert out.get("reason") != "wide_bbo_spread"
    assert sess.state == STATE_LIVE_PENDING_ENTRY
    # Bounded marketable LIMIT, never a naked market order.
    ad.place_market_order.assert_not_called()
    ad.place_limit_order_gtc.assert_called()


def test_live_execution_summary_persisted(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="SNP-USD")
    db.commit()
    uid = _uid(db, "snp")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="SNP-USD",
        variant_id=vid,
        mode="live",
        state=STATE_WATCHING_LIVE,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 80, "max_hold_seconds": 3600},
        },
    )
    db.commit()
    ad = _mk_adapter()
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
        db.commit()
        db.refresh(sess)
    snap = sess.risk_snapshot_json or {}
    assert "momentum_live_execution" in snap
    assert int(snap["momentum_live_execution"].get("tick_count") or 0) >= 1


def test_dev_tick_endpoint_gated(client) -> None:
    r = client.post("/api/trading/momentum/live-runner/tick", json={"session_id": 1})
    assert r.status_code == 404


def test_ack_timeout_adopts_filled_order_not_orphan(monkeypatch, db: Session) -> None:
    """RACE GUARD: when the entry order FILLS between the 10s ack-timeout and the
    (slow, <=30s-cadence) tick, the session must ADOPT the fill, NOT cancel +
    abandon it -> orphan. [CTNT 2026-06-09: filled @21s, ack-timeout @22.9s ->
    orphaned -> -$283.] First get_order (top fill-handler) sees OPEN; the ack-
    timeout re-fetch sees FILLED -> must return pending (adopt), not re-watch."""
    from datetime import datetime, timedelta
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="RACE-USD")
    db.commit()
    uid = _uid(db, "race")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db, user_id=uid, symbol="RACE-USD", variant_id=vid, mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": {
                "entry_submitted": True,
                "entry_order_id": "o-race",
                # Submitted long enough ago to trip the ack-timeout rest-backstop
                # (now elapsed > max(0.5,rest_bars)*entry_interval_s, ~120s+ on a 1m
                # interval — refactored 2026-06-11 from the old fixed ~10s, so 20s no
                # longer fires). Large + interval-agnostic so the adopt path runs.
                "entry_submit_utc": (datetime.utcnow() - timedelta(seconds=7200)).isoformat(),
            },
        },
    )
    db.commit()
    ad = _mk_adapter()
    _open = NormalizedOrder(order_id="o-race", client_order_id="cid", product_id="RACE-USD",
                            side="buy", status="OPEN", order_type="limit",
                            filled_size=0.0, average_filled_price=None)
    _filled = NormalizedOrder(order_id="o-race", client_order_id="cid", product_id="RACE-USD",
                              side="buy", status="filled", order_type="limit",
                              filled_size=809.0, average_filled_price=2.21)
    _calls = {"n": 0}
    def _get_order(_oid):
        _calls["n"] += 1
        return (_open, _fresh()) if _calls["n"] == 1 else (_filled, _fresh())
    ad.get_order.side_effect = _get_order

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)

    # adopted (not abandoned): re-fetch saw the fill -> pending, no cancel, order kept
    assert out.get("pending") == "ack_timeout_filled_adopt", out
    ad.cancel_order.assert_not_called()
    assert sess.state != STATE_WATCHING_LIVE
    assert (sess.risk_snapshot_json or {})["momentum_live_execution"].get("entry_order_id") == "o-race"


def test_ack_timeout_cancel_race_adopts_filled_order(monkeypatch, db: Session) -> None:
    """The CANCEL ITSELF can lose the race: the ack-timeout re-fetch sees OPEN so it
    cancels, but the order fills before/despite the cancel landing. The POST-cancel
    re-fetch must ADOPT the (cancelled-but-)filled order, NOT abandon it to an
    unmanaged orphan. [SDOT 2026-06-10: 56sh / $1,608 filled while the cancel raced ->
    orphaned with no lane stop, operator exited it by hand.]"""
    from datetime import datetime, timedelta
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="RACE-USD")
    db.commit()
    uid = _uid(db, "race2")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db, user_id=uid, symbol="RACE-USD", variant_id=vid, mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": {
                "entry_submitted": True,
                "entry_order_id": "o-race2",
                # Submitted long enough ago to trip the ack-timeout rest-backstop
                # (now elapsed > max(0.5,rest_bars)*entry_interval_s, ~120s+ on a 1m
                # interval — refactored 2026-06-11 from the old fixed ~10s, so 20s no
                # longer fires). Large + interval-agnostic so the adopt path runs.
                "entry_submit_utc": (datetime.utcnow() - timedelta(seconds=7200)).isoformat(),
            },
        },
    )
    db.commit()
    ad = _mk_adapter()
    _open = NormalizedOrder(order_id="o-race2", client_order_id="cid", product_id="RACE-USD",
                            side="buy", status="OPEN", order_type="limit",
                            filled_size=0.0, average_filled_price=None)
    # cancelled-but-filled: filled_size>0 + 'cancelled' -> _order_done_for_entry() True
    _raced = NormalizedOrder(order_id="o-race2", client_order_id="cid", product_id="RACE-USD",
                             side="buy", status="cancelled", order_type="limit",
                             filled_size=56.0, average_filled_price=23.55)
    _calls = {"n": 0}
    def _get_order(_oid):
        _calls["n"] += 1
        # 1st (top fill-handler) + 2nd (ack-timeout _fresh) = OPEN -> cancels;
        # 3rd (post-cancel _post) = raced fill -> must adopt.
        return (_open, _fresh()) if _calls["n"] <= 2 else (_raced, _fresh())
    ad.get_order.side_effect = _get_order

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)

    ad.cancel_order.assert_called_once()  # it DID cancel (the _fresh re-fetch saw open)
    assert out.get("pending") == "ack_timeout_cancel_raced_fill_adopt", out
    assert sess.state != STATE_WATCHING_LIVE  # NOT abandoned to an orphan
    assert (sess.risk_snapshot_json or {})["momentum_live_execution"].get("entry_order_id") == "o-race2"


def test_late_fill_sweep_repoints_abandoned_order(monkeypatch, db: Session) -> None:
    """An entry order the ack-timeout abandoned (pointer wiped, id kept in history)
    that fills SECONDS later must be RE-POINTED + adopted via the late-fill sweep —
    not left as an unmanaged broker position. [BATL 2026-06-10: 5 such fills stacked
    ~$8k with no lane stop.]"""
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="LATE-USD")
    db.commit()
    uid = _uid(db, "late")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db, user_id=uid, symbol="LATE-USD", variant_id=vid, mode="live",
        state=STATE_WATCHING_LIVE,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": {
                # abandoned by a previous ack-timeout: pointer wiped, history kept
                "entry_submitted": False,
                "entry_order_id": None,
                "entry_order_ids_all": ["o-lost"],
            },
        },
    )
    db.commit()
    ad = _mk_adapter()
    ad.get_order.return_value = (
        NormalizedOrder(order_id="o-lost", client_order_id="cid-l", product_id="LATE-USD",
                        side="buy", status="filled", order_type="limit",
                        filled_size=56.0, average_filled_price=1.633),
        _fresh(),
    )

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)

    assert out.get("pending") == "late_fill_repointed", out
    le = (sess.risk_snapshot_json or {})["momentum_live_execution"]
    assert le.get("entry_order_id") == "o-lost"       # re-pointed at the real order
    assert le.get("entry_submitted") is True
    assert sess.state == STATE_LIVE_PENDING_ENTRY     # the fill-handler adopts next pass
    assert (le.get("entry_orders_resolved") or {}).get("o-lost") == "adopted"


def test_unresolved_entry_order_blocks_new_submit(monkeypatch, db: Session) -> None:
    """While a previously-placed order is UNRESOLVED (venue still shows it open after
    an abandon), the runner must NOT place another entry order — stacking guard."""
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="STAK-USD")
    db.commit()
    uid = _uid(db, "stak")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db, user_id=uid, symbol="STAK-USD", variant_id=vid, mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": {
                "entry_submitted": False,
                "entry_order_id": None,
                "entry_order_ids_all": ["o-openlost"],
            },
        },
    )
    db.commit()
    ad = _mk_adapter()
    ad.get_order.return_value = (
        NormalizedOrder(order_id="o-openlost", client_order_id="cid-s", product_id="STAK-USD",
                        side="buy", status="OPEN", order_type="limit",
                        filled_size=0.0, average_filled_price=None),
        _fresh(),
    )

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)

    assert out.get("blocked") is True and out.get("reason") == "unresolved_entry_orders", out
    ad.place_limit_order_gtc.assert_not_called()      # NO second clip
    ad.place_market_order.assert_not_called()


def test_void_resolution_unblocks_guard() -> None:
    """A history id the venue confirms cancelled-with-zero-fill resolves to void and
    no longer blocks the pre-submit guard."""
    from app.services.trading.momentum_neural.live_runner import (
        _sweep_unresolved_entry_orders, _unresolved_entry_order_ids,
    )

    le = {"entry_order_ids_all": ["o-dead"], "entry_submitted": False, "entry_order_id": None}
    assert _unresolved_entry_order_ids(le) == ["o-dead"]
    ad = MagicMock()
    ad.get_order.return_value = (
        NormalizedOrder(order_id="o-dead", client_order_id="c", product_id="X-USD",
                        side="buy", status="cancelled", order_type="limit",
                        filled_size=0.0, average_filled_price=None),
        _fresh(),
    )
    sess = SimpleNamespace(id=1, risk_snapshot_json={}, state=STATE_WATCHING_LIVE, symbol="X-USD")
    db = MagicMock()
    with patch("app.services.trading.momentum_neural.live_runner._commit_le"), \
         patch("app.services.trading.momentum_neural.live_runner._emit"), \
         patch("app.services.trading.momentum_neural.live_runner._safe_transition"):
        repointed = _sweep_unresolved_entry_orders(ad, db, sess, le)
    assert repointed is False
    assert _unresolved_entry_order_ids(le) == []      # void -> guard unblocked
    assert (le.get("entry_orders_resolved") or {}).get("o-dead") == "void"


# ── Halt awareness (suspected-halt detect / resume cooldown) ──────────────────


def _halt_helpers():
    from app.services.trading.momentum_neural.live_runner import (
        _halt_resume_cooldown_active,
        _register_fresh_quote_tick,
        _register_stale_quote_tick,
    )
    return _register_stale_quote_tick, _register_fresh_quote_tick, _halt_resume_cooldown_active


def test_halt_detected_after_stale_streak_and_position_alert():
    """3 consecutive stale-quote ticks = suspected halt; a held position raises the
    loud position_halted alert (the software stop cannot execute during a halt)."""
    _stale, _freshq, _cool = _halt_helpers()
    sess = SimpleNamespace(id=9, symbol="KMRK", state=STATE_LIVE_ENTERED, risk_snapshot_json={})
    db = MagicMock()
    le = {"position": {"quantity": 250.0, "avg_entry_price": 4.35, "stop_price": 4.1}}
    events = []
    with patch("app.services.trading.momentum_neural.live_runner._emit",
               side_effect=lambda _db, _s, et, p: events.append(et)):
        _stale(db, sess, le); _stale(db, sess, le)
        assert "suspected_halt_detected" not in events  # below threshold: just a blip
        _stale(db, sess, le)  # 3rd consecutive -> halt
    assert le.get("halt_stale_streak") == 3
    assert le.get("suspected_halt_since_utc")
    assert "suspected_halt_detected" in events
    assert "position_halted" in events  # held into the halt -> loud alert


def test_halt_resume_starts_cooldown_then_expires():
    """Fresh quotes after a suspected halt = RESUME: entry cooldown active (whipsaw
    window), then expires."""
    from datetime import timedelta
    from app.services.trading.momentum_neural.live_runner import _utcnow

    _stale, _freshq, _cool = _halt_helpers()
    sess = SimpleNamespace(id=9, symbol="KMRK", state=STATE_WATCHING_LIVE, risk_snapshot_json={})
    db = MagicMock()
    le = {"suspected_halt_since_utc": "2026-06-10T15:50:00", "halt_stale_streak": 5}
    events = []
    with patch("app.services.trading.momentum_neural.live_runner._emit",
               side_effect=lambda _db, _s, et, p: events.append(et)):
        _freshq(db, sess, le)
    assert "halt_resumed" in events
    assert le.get("halt_stale_streak") == 0
    assert not le.get("suspected_halt_since_utc")
    assert le.get("halt_resumed_at_utc")
    assert _cool(le) is True                       # inside the whipsaw window: blocked
    le["halt_resumed_at_utc"] = (_utcnow() - timedelta(seconds=10_000)).isoformat()
    assert _cool(le) is False                      # window expired: entries allowed


def test_fresh_quote_without_halt_is_noop():
    _stale, _freshq, _cool = _halt_helpers()
    sess = SimpleNamespace(id=9, symbol="OK", state=STATE_WATCHING_LIVE, risk_snapshot_json={})
    db = MagicMock()
    le = {"halt_stale_streak": 1}
    events = []
    with patch("app.services.trading.momentum_neural.live_runner._emit",
               side_effect=lambda _db, _s, et, p: events.append(et)):
        _freshq(db, sess, le)
    assert events == []                            # no halt_resumed: there was no halt
    assert le.get("halt_stale_streak") == 0        # blip streak cleared
    assert _cool(le) is False


def test_tick_skips_disconnected_venue(monkeypatch, db: Session) -> None:
    """A tick must NOT carry the session row lock into broker calls against a
    disconnected venue (idle-in-tx holder) — it skips cleanly and resumes when the
    broker reconnects."""
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="DEAD-USD")
    db.commit()
    uid = _uid(db, "deadvenue")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session
    import app.services.trading.momentum_neural.live_runner as lr

    sess = create_trading_automation_session(
        db, user_id=uid, symbol="DEAD-USD", variant_id=vid, mode="live",
        state=STATE_WATCHING_LIVE,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": {},
        },
    )
    db.commit()
    ad = _mk_adapter()
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda ef: False)

    out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)

    assert out.get("skipped") == "venue_broker_not_connected", out
    ad.get_best_bid_ask.assert_not_called()   # no broker call while the lock is held
    ad.get_order.assert_not_called()


# ── G1 marketable re-peg LOOP (2026-06-22 review-driven coverage) ──────────
def _mk_pending_repeg_session(db, monkeypatch, symbol, *, post_order, max_notional=2000.0):
    """A LIVE_PENDING_ENTRY session whose resting buy is 'left behind' (live bid above
    the limit) so the ack-timeout path reaches the re-peg branch. ``post_order`` is what
    the POST-cancel get_order refetch returns (returned only AFTER cancel_order fires, so
    the pre-cancel fill-handler/race-guard refetches always see OPEN — robust to count)."""
    from datetime import datetime, timedelta
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_entry_chase_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_entry_max_repegs", 3)
    vid, vrow = _seed_live_eligible_row(db, symbol=symbol)
    _is_crypto = symbol.upper().endswith("-USD")
    _ef = "coinbase_spot" if _is_crypto else "robinhood_spot"
    _venue = "coinbase" if _is_crypto else "robinhood"
    vrow.execution_family = _ef     # equity symbol -> equity venue (asset-class alignment)
    db.commit()
    uid = _uid(db, "rp_" + symbol.replace("-", "_"))
    sess = create_trading_automation_session(
        db, user_id=uid, venue=_venue, execution_family=_ef, symbol=symbol,
        variant_id=vid, mode="live", state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": {
                "entry_submitted": True,
                "entry_order_id": "o-rp",
                "entry_submit_utc": (datetime.utcnow() - timedelta(seconds=7200)).isoformat(),
                "entry_limit_price": "10.0",
                "entry_original_limit_px": 10.0,
                "entry_repeg_count": 0,
                "entry_expected_move_bps": 5000.0,
                "structural_stop_price": 9.0,
                "entry_notional_guard": {"max_notional_usd": max_notional},
                "entry_resize_basis": {
                    "max_loss_usd": 50.0, "atr_pct": 0.05, "stop_atr_mult": 0.6,
                    "base_increment": 1.0, "base_min_size": 1.0,
                },
            },
        },
    )
    db.commit()
    ad = _mk_adapter()
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(product_id=symbol, bid=10.10, ask=10.15, mid=10.125,
                         spread_bps=50.0, freshness=_fresh()),
        _fresh(),
    )
    _open = NormalizedOrder(order_id="o-rp", client_order_id="cid", product_id=symbol,
                            side="buy", status="OPEN", order_type="limit",
                            filled_size=0.0, average_filled_price=None)
    _state = {"cancelled": False}
    def _cancel(_oid):
        _state["cancelled"] = True
        return {"ok": True, "raw": {}}
    ad.cancel_order.side_effect = _cancel
    def _get_order(_oid):
        return (post_order, _fresh()) if _state["cancelled"] else (_open, _fresh())
    ad.get_order.side_effect = _get_order
    ad.place_limit_order_gtc.return_value = {"ok": True, "order_id": "o-rp-2", "client_order_id": "cid-rp-2"}
    return sess, ad


def test_repeg_fires_gfd_riskfirst_resolves_old(monkeypatch, db: Session) -> None:
    """G1 re-peg LOOP (equity): a left-behind entry within the cumulative ceiling is
    cancel-replaced UP to the live ask with (1) time_in_force='gfd' (never GTC),
    (2) RISK-FIRST qty (not notional-only), (3) the OLD order marked resolved (no orphan)."""
    from app.services.trading.momentum_neural.risk_policy import compute_risk_first_quantity
    from app.services.trading.momentum_neural.live_runner import _entry_repeg_price
    _cancelled = NormalizedOrder(order_id="o-rp", client_order_id="cid", product_id="RPEG",
                                 side="buy", status="cancelled", order_type="limit",
                                 filled_size=0.0, average_filled_price=None)
    sess, ad = _mk_pending_repeg_session(db, monkeypatch, "RPEG", post_order=_cancelled)
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)

    assert out.get("pending") == "entry_repegged", out
    _kw = ad.place_limit_order_gtc.call_args.kwargs
    assert _kw.get("time_in_force") == "gfd"                       # (1) DAY, never GTC
    _rp_new = _entry_repeg_price(original_limit_px=10.0, live_ask=10.15, expected_move_bps=5000.0)
    _rf_q, _ = compute_risk_first_quantity(
        entry_price=_rp_new, atr_pct=0.05, max_loss_usd=50.0, max_notional_ceiling_usd=2000.0,
        base_increment=1.0, base_min_size=1.0, stop_atr_mult=0.6)
    assert float(_kw.get("base_size")) == pytest.approx(_rf_q, abs=1e-6)   # (2) risk-first qty
    assert int(_rf_q) < int(2000.0 / _rp_new)                              # risk-first binds, < notional-only
    le = (sess.risk_snapshot_json or {})["momentum_live_execution"]
    assert (le.get("entry_orders_resolved") or {}).get("o-rp") == "void"   # (3) old resolved (no orphan)
    assert le.get("entry_order_id") == "o-rp-2"
    assert int(le.get("entry_repeg_count") or 0) == 1


def test_repeg_blocked_on_indeterminate_cancel_state(monkeypatch, db: Session) -> None:
    """G1 #4: if the POST-cancel get_order returns None (unknown), DON'T place a 2nd
    order — return pending=cancel_indeterminate so the next tick re-checks venue truth."""
    sess, ad = _mk_pending_repeg_session(db, monkeypatch, "RPEG2", post_order=None)
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    assert out.get("pending") == "cancel_indeterminate", out
    ad.place_limit_order_gtc.assert_not_called()   # no naked second order


def test_repeg_excluded_for_crypto_symbol(monkeypatch, db: Session) -> None:
    """G1 #5: crypto (-USD) is NEVER chased (asset-class gated) even when left behind —
    it falls through to the safe cancel + re-watch, no marketable re-peg."""
    _cancelled = NormalizedOrder(order_id="o-rp", client_order_id="cid", product_id="RPEG-USD",
                                 side="buy", status="cancelled", order_type="limit",
                                 filled_size=0.0, average_filled_price=None)
    sess, ad = _mk_pending_repeg_session(db, monkeypatch, "RPEG-USD", post_order=_cancelled)
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)
    assert out.get("pending") != "entry_repegged", out   # no chase on crypto
    ad.place_limit_order_gtc.assert_not_called()
    assert sess.state == STATE_WATCHING_LIVE              # safe re-watch
