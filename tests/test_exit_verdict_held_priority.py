"""Actual held ticks must observe G before optional hold/trail return paths."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.venue.protocol import NormalizedTicker
from tests.test_held_tick_bbo_iqfeed_l1_first import _wired  # noqa: F401
from tests.test_exit_verdict_f_state_machine import T_ENTRY, _spike_tape, _spike_sells
from tests.test_exit_verdict_g_whole_exit_seam import _resting_deadman_session
from tests.test_momentum_emergency_exit_recovery import _fresh


@pytest.fixture(autouse=True)
def _no_external_market_or_broker_http(monkeypatch):
    import requests
    from curl_cffi import requests as curl_requests

    def unavailable(*args, **kwargs):
        raise RuntimeError("external HTTP unavailable in held-priority regression")

    monkeypatch.setattr(requests.sessions.Session, "request", unavailable)
    monkeypatch.setattr(curl_requests.Session, "request", unavailable)
    monkeypatch.setattr(lr, "_replay_aware_fetch_ohlcv_df", lambda *a, **k: None)


def _held_tick(db, monkeypatch, *, smart_hold=False, early_trail=False, supported=True,
               rollover=True, loss_cap=None):
    tape = _spike_tape()
    high = tape.rows[-1]
    if rollover:
        _spike_sells(tape)
    now = T_ENTRY + timedelta(seconds=12.5 if rollover else 4)
    marker = {
        "phase": "armed", "accel_prev": 1200.0,
        "accel_prev_contract": lr._exit_verdict_settings()["contract_id"],
        "entry_at": T_ENTRY.isoformat(), "entry_px": 10.0,
        "frontier_at": high[5].isoformat(), "frontier_id": high[6],
        "last_print": high[0], "last_print_at": high[5].isoformat(),
        "leg_high": {"price": high[0], "observed_at": high[5].isoformat(), "id": high[6]},
        "deadman": {"level": 9.0, "level_source": "resting_stop", "ratchets": 0},
        "prints_since_entry": 3, "prints_since_high": 0,
    }
    sess, le, adapter, _, _ = _resting_deadman_session(
        db, symbol="EVPRIOR", deadman_qty=10, marker=marker if supported else None,
    )
    if not supported:
        le.pop("entry_filled_at_utc", None)
    le["position"]["target_price"] = 12.0
    le["position"]["high_water_mark"] = 10.9
    le["position"]["opened_at_utc"] = T_ENTRY.isoformat()
    le["breakout_level_price"] = 10.5
    snapshot = dict(sess.risk_snapshot_json)
    snapshot["momentum_live_execution"] = le
    if loss_cap is not None:
        snapshot["momentum_policy_caps"]["max_loss_per_trade_usd"] = loss_cap
    sess.risk_snapshot_json = snapshot
    db.add(sess)
    db.commit()
    monkeypatch.setattr(lr, "_utcnow", lambda: now)
    bid = 9.5 if loss_cap is not None else (10.9 if early_trail else 10.24)
    meta = _fresh()
    ticker = NormalizedTicker(product_id="EVPRIOR", bid=bid, ask=bid + .02,
                              mid=bid + .01, freshness=meta)
    monkeypatch.setattr(lr, "_live_tick_bbo", lambda *a, **k: (ticker, meta, {"reason": "test_fresh_bbo"}))
    adapter.execution_bbo = (ticker, meta)
    # The resting generation is owned by the real claim fixture. Its independent
    # maintenance is already covered by seam tests; no provider/broker is contacted.
    monkeypatch.setattr(lr, "_ensure_alpaca_deadman_stop", lambda *a, **k: {})
    monkeypatch.setattr(settings, "chili_momentum_smart_hold_enabled", smart_hold)
    monkeypatch.setattr(settings, "chili_momentum_early_trail_arm_enabled", early_trail)
    monkeypatch.setattr(settings, "chili_momentum_breakout_bailout_enabled", True)
    monkeypatch.setattr(lr, "_opinion_exit_suppressed", lambda *a, **k: False)
    monkeypatch.setattr(lr, "_c1_iqfeed_phantom_loss", lambda *a, **k: (False, {}))
    monkeypatch.setattr(eg, "leg_prints_between", tape.leg_prints_between)
    monkeypatch.setattr(eg, "leg_prints_since_high", tape.leg_prints_since_high)
    monkeypatch.setattr(eg, "signed_tape_accel_features", tape.signed_tape_accel_features)
    submits = []
    original_submit = lr._submit_live_market_exit

    def submit(*args, **kwargs):
        submits.append(kwargs)
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(lr, "_submit_live_market_exit", submit)
    lr._reconcile_counters.pop(int(sess.id), None)
    return sess, le, adapter, tape, submits


@pytest.mark.parametrize("held_state", [lr.STATE_LIVE_ENTERED, lr.STATE_LIVE_SCALING_OUT, lr.STATE_LIVE_TRAILING])
@pytest.mark.parametrize("smart_hold,early_trail", [(True, False), (False, True), (True, True)])
def test_actual_held_rollover_submits_whole_exit_before_optional_returns(
    db, monkeypatch, _wired, smart_hold, early_trail, held_state,
):
    sess, _, adapter, tape, submits = _held_tick(
        db, monkeypatch, smart_hold=smart_hold, early_trail=early_trail,
    )
    sess.state = held_state
    db.commit()
    result = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    db.commit()
    db.refresh(sess)
    saved = sess.risk_snapshot_json["momentum_live_execution"]
    assert result.get("exit_verdict") == "accel_rollover", result
    assert saved["exit_verdict"]["phase"] == "exit_pending"
    assert saved["exit_verdict"]["exit"]["trigger"] == "accel_rollover"
    assert len(submits) == 1 and submits[0]["quantity"] == 10
    assert submits[0]["extra"]["exit_fraction"] == 1
    assert submits[0]["reason"] == "tape_accel_rollover"
    assert not any(event == "live_trailing_armed" for event, _ in _wired)
    assert not any(event == "smart_hold_decision" for event, _ in _wired)
    assert any(kind == "between" for kind, _ in tape.reads)
    # The existing seam freezes its owned whole-close successor before transport.
    assert result.get("ok") is True, result
    assert adapter.market_calls == [] and adapter.limit_calls == []


def test_actual_max_loss_priority_precedes_rollover(db, monkeypatch, _wired):
    sess, _, adapter, tape, submits = _held_tick(
        db, monkeypatch, smart_hold=True, early_trail=True, loss_cap=1.0,
    )
    result = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    assert result["state"] == lr.STATE_LIVE_BAILOUT, result
    assert any(event == "live_bailout" and payload["reason"] == "max_loss_per_trade"
               for event, payload in _wired)
    assert not tape.reads and not submits
    assert adapter.market_calls == [] and adapter.limit_calls == []


def test_early_trail_still_arms_after_a_real_print_hold(db, monkeypatch, _wired):
    sess, _, adapter, tape, submits = _held_tick(
        db, monkeypatch, early_trail=True, rollover=False,
    )
    result = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    assert result["state"] == lr.STATE_LIVE_TRAILING, result
    assert any(event == "live_trailing_armed" and payload["early_arm"] for event, payload in _wired)
    assert any(kind == "between" for kind, _ in tape.reads)
    assert not submits and not adapter.market_calls and not adapter.limit_calls


@pytest.mark.parametrize("supported", [True, False])
def test_smart_hold_cut_is_only_an_opinion_for_supported_equity(
    db, monkeypatch, _wired, supported,
):
    from app.services.trading.momentum_neural import pipeline

    sess, _, adapter, tape, submits = _held_tick(
        db, monkeypatch, smart_hold=True, supported=supported, rollover=False,
    )
    monkeypatch.setattr(pipeline, "_live_realized_vol", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_live_flow_slope", lambda *a, **k: {})
    monkeypatch.setattr(lr, "_recent_scalp_median_hold_s", lambda *a, **k: 30.0)
    monkeypatch.setattr(lr, "_smart_hold_time_floor_s", lambda *a, **k: 1.0)
    monkeypatch.setattr(lr, "_smart_hold_breach_volume", lambda *a, **k: (100.0, 50.0))
    monkeypatch.setattr(lr, "_replay_aware_fetch_ohlcv_df", lambda *a, **k: None)
    monkeypatch.setattr(lr, "smart_hold_decision", lambda **k: SimpleNamespace(
        cut=True, hold=False, reason="test_confirmed_cut", band_frac=.01,
        hold_floor_px=10.4, time_floor_suppressed=False,
    ))
    result = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    db.commit()
    db.refresh(sess)
    saved = sess.risk_snapshot_json["momentum_live_execution"]
    assert any(event == "smart_hold_decision" for event, _ in _wired), result
    if supported:
        assert saved["exit_verdict"]["phase"] == "armed"
        assert "smart_hold_fast_bail" in saved["opinion_exit_armed"]["reasons"]
        assert sess.state != lr.STATE_LIVE_BAILOUT
        assert any(kind == "between" for kind, _ in tape.reads)
    else:
        assert sess.state == lr.STATE_LIVE_BAILOUT
        assert not tape.reads
        assert not saved.get("opinion_exit_armed")
    assert not submits
    assert adapter.market_calls == [] and adapter.limit_calls == []
