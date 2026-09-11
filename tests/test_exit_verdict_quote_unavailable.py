"""The real held-tick quote branch preserves a tape decision without an order."""
from datetime import timedelta

from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import live_runner as lr
from tests.test_held_tick_bbo_iqfeed_l1_first import _wired  # noqa: F401
from tests.test_momentum_emergency_exit_recovery import (
    _ScriptedAlpaca, _ensure_retained_entry_owner, _seed_session,
)
from tests.test_exit_verdict_f_state_machine import T_ENTRY, _spike_tape, _spike_sells


def test_real_held_tick_records_rollover_without_a_mid_or_an_invented_bid(db, monkeypatch, _wired):
    tape = _spike_tape()
    high = tape.rows[-1]
    _spike_sells(tape)
    marker = {
        "phase": "armed", "accel_prev": 1200.0,
        "entry_at": T_ENTRY.isoformat(), "entry_px": 10.0,
        "frontier_at": high[5].isoformat(), "frontier_id": high[6],
        "last_print": high[0], "last_print_at": high[5].isoformat(),
        "leg_high": {"price": high[0], "observed_at": high[5].isoformat(), "id": high[6]},
        "deadman": {"level": 9.0, "level_source": "resting_stop", "ratchets": 0},
        "prints_since_entry": 3, "prints_since_high": 0,
    }
    sess = _seed_session(db, symbol="EVNOBBO", quantity=10, avg_entry_price=10,
                         le_extra={"entry_filled_at_utc": T_ENTRY.isoformat(), "exit_verdict": marker})
    _ensure_retained_entry_owner(db, sess)
    monkeypatch.setattr(lr, "_utcnow", lambda: T_ENTRY + timedelta(seconds=12.5))
    monkeypatch.setattr(lr, "_live_tick_bbo", lambda *a, **k: (
        None, None, {"reason": "held_bbo_unavailable", "counts_toward_halt": False},
    ))
    monkeypatch.setattr(eg, "leg_prints_between", tape.leg_prints_between)
    monkeypatch.setattr(eg, "leg_prints_since_high", tape.leg_prints_since_high)
    monkeypatch.setattr(eg, "signed_tape_accel_features", tape.signed_tape_accel_features)
    adapter = _ScriptedAlpaca(positions=[10.0])
    lr._reconcile_counters.pop(int(sess.id), None)
    result = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    db.commit()
    db.refresh(sess)
    assert result["exit_decision_waiting_for_bbo"] is True
    saved = sess.risk_snapshot_json["momentum_live_execution"]
    assert saved["exit_verdict"]["phase"] == "exit_pending"
    assert saved["exit_verdict"]["exit"]["trigger"] == "accel_rollover"
    assert saved["exit_verdict"]["exit"]["bid"] is None
    assert not saved.get("pending_exit_reason")  # no order intent falsely marked submitted
    assert adapter.limit_calls == [] and adapter.market_calls == []
    assert saved["position"]["quantity"] == 10
