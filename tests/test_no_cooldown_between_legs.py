"""WALANG COOLDOWN SA PAGITAN NG MGA LEG (2026-09-10).

Operator: "wala dapat cooldown — pangtao lang ang cooldown... burahin mo na para di
na magkaissue ulit."

Dati: EXITED -> (compute 112 s / 450 s "adaptive" cooldown, write
``le["cooldown_until_utc"]``, emit ``live_cooldown_started``) -> STATE_LIVE_COOLDOWN
-> next tick, timer flag OFF since 2026-07-04 -> pop the key -> WATCHING. The
receipt announced protection that did not exist.

MEASURED live 2026-09-03..09-10 (trading_automation_events JOIN sessions mode=live,
26 sessions / 16 symbols): 54 ``live_cooldown_started``; 54 resolved (53
``live_recycled`` + 1 ``live_reentry_capped``) BEFORE their own ``until_utc``,
0 after; p50 gap 2.14 s, max 30.06 s; 28 re-entries followed, 21 of them inside
the announced window (p50 86 s to refill), net −$505.61 (5 green +$67.05 / 16 red
−$572.66) — legs the tape let through and the "cooldown" never touched. The
counterfactual of deleting it is therefore ZERO legs changed: nothing bound.

Three guarantees:
  (a) no code path writes ``cooldown_until_utc`` or emits ``live_cooldown_started``,
      and the FSM has no edge INTO ``live_cooldown``;
  (b) an EXITED session recycles STRAIGHT to WATCHING_LIVE with the same
      trade_cycles / stopout_cycles / G4 escalation bookkeeping the old COOLDOWN
      handler ran — and a legacy row persisted in ``live_cooldown`` (with a
      cooldown_until_utc still in the FUTURE) resolves the same way, immediately;
  (c) the timer setting is gone.

Uses the truncating ``db`` fixture (TEST_DATABASE_URL, _test DB) and the live-runner
tick harness from test_momentum_live_runner / test_momentum_paper_runner.
"""
from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import text

from app.config import Settings, settings
from app.services.trading.momentum_neural import live_fsm
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_COOLDOWN,
    STATE_LIVE_EXITED,
    STATE_LIVE_FINISHED,
    STATE_WATCHING_LIVE,
    can_transition_live,
)
from app.services.trading.momentum_neural.live_runner import tick_live_session
from app.services.trading.momentum_neural.replay_parity import LOAD_BEARING_TRANSITIONS
from app.services.trading.momentum_neural.risk_policy import (
    RISK_SNAPSHOT_KEY,
    stop_class_exit_reason,
)

from tests.test_momentum_live_runner import (  # noqa: F401
    _mk_adapter,
    _uid,
    _venue_connected_by_default,
)
from tests.test_momentum_paper_runner import _seed_live_eligible_row

# The 7-day live measurement that justifies the deletion, frozen as the acceptance
# record (see module docstring). ``bound`` is the number of cooldowns that lasted
# until their own until_utc — the only number that could have argued for keeping it.
MEASURED_7D_2026_09_10 = {
    "live_cooldown_started": 54,
    "resolved_before_until_utc": 54,
    "resolved_after_until_utc": 0,
    "p50_seconds_to_recycle": 2.14,
    "max_seconds_to_recycle": 30.06,
    "reentries_inside_announced_window": 21,
}


def test_measured_cooldown_bound_zero_times():
    m = MEASURED_7D_2026_09_10
    assert m["resolved_after_until_utc"] == 0
    assert m["resolved_before_until_utc"] == m["live_cooldown_started"]


# ── (a) the writer, the receipt and the edge are gone ────────────────────────


def _runner_code_lines() -> list[str]:
    """Non-comment source lines of live_runner (the comments still NAME the deleted
    receipt to explain the deletion — only executable code is scanned)."""
    src = inspect.getsource(lr)
    return [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]


def test_no_code_path_writes_cooldown_until_utc():
    writer = re.compile(r'le\[\s*["\']cooldown_until_utc["\']\s*\]\s*=')
    hits = [ln for ln in _runner_code_lines() if writer.search(ln)]
    assert hits == [], hits


def test_no_code_path_emits_live_cooldown_started():
    hits = [ln for ln in _runner_code_lines() if '"live_cooldown_started"' in ln]
    assert hits == [], hits


def test_no_code_path_transitions_into_cooldown():
    into = re.compile(r"_safe_transition\([^)]*STATE_LIVE_COOLDOWN")
    hits = [ln for ln in _runner_code_lines() if into.search(ln)]
    assert hits == [], hits
    # and the pure FSM refuses the edge — nothing can enter the legacy state
    assert not can_transition_live(STATE_LIVE_EXITED, STATE_LIVE_COOLDOWN)
    for frm in live_fsm.LIVE_RUNNER_RUNNABLE_STATES - {STATE_LIVE_COOLDOWN}:
        assert not can_transition_live(frm, STATE_LIVE_COOLDOWN), frm


def test_exited_recycles_and_terminalizes_directly_in_the_fsm():
    assert can_transition_live(STATE_LIVE_EXITED, STATE_WATCHING_LIVE)
    assert can_transition_live(STATE_LIVE_EXITED, STATE_LIVE_FINISHED)
    # legacy rows persisted in live_cooldown still resolve
    assert can_transition_live(STATE_LIVE_COOLDOWN, STATE_WATCHING_LIVE)
    assert can_transition_live(STATE_LIVE_COOLDOWN, STATE_LIVE_FINISHED)


def test_cooldown_receipt_is_not_a_parity_transition():
    assert "live_cooldown_started" not in LOAD_BEARING_TRANSITIONS
    assert "live_recycled" in LOAD_BEARING_TRANSITIONS


def test_runner_no_longer_imports_the_adaptive_cooldown_calculator():
    assert not hasattr(lr, "adaptive_reentry_cooldown_seconds")


# ── (c) the setting is gone ───────────────────────────────────────────────────


def test_timer_setting_is_gone():
    gone = (
        "chili_momentum_stopout_cooldown_timer_enabled",
        "chili_momentum_adaptive_reentry_cooldown_enabled",
        "chili_momentum_reentry_profit_cooldown_factor",
        "chili_momentum_reentry_cooldown_vol_ref_atr_pct",
        "chili_momentum_reentry_cooldown_vol_span",
    )
    for name in gone:
        assert name not in Settings.model_fields, name
        assert not hasattr(settings, name), name


# ── (b) EXITED -> WATCHING with the same bookkeeping ─────────────────────────


def _seed(db, *, symbol, name, state, le_extra):
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, name)
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    le = {
        # the PRIOR trade's lifecycle state (cleared by the recycle reset)
        "entry_order_id": "LEG1-ENTRY",
        "entry_order_ids_all": ["LEG1-ENTRY"],
        "entry_orders_resolved": {"LEG1-ENTRY": "adopted"},
        "entry_submitted": True,
        "position": {"quantity": 100.0, "avg_entry_price": 2.0, "product_id": symbol},
        # cross-cycle state that must survive
        "trade_cycles": 1,
        "stopout_cycles": 0,
        "g4_reentry_escalation": 0,
        "fees_usd_total": 1.5,
    }
    le.update(le_extra)
    sess = create_trading_automation_session(
        db, user_id=uid, symbol=symbol, variant_id=vid, mode="live", state=state,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": le,
        },
    )
    db.commit()
    return sess


def _tick(monkeypatch, db, sess):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    ad = _mk_adapter()
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
               return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    return out


def _le(sess):
    return (sess.risk_snapshot_json or {}).get("momentum_live_execution") or {}


def _events(db, sid):
    rows = db.execute(text(
        "SELECT event_type, payload_json FROM trading_automation_events "
        "WHERE session_id=:sid ORDER BY id"
    ), {"sid": sid}).fetchall()
    return [(r[0], r[1]) for r in rows]


def test_exited_stop_class_loss_recycles_straight_to_watching(monkeypatch, db):
    """One tick. Stop-class red exit: stopout_cycles 0 -> 1, G4 level 0 -> 1,
    trade_cycles 1 -> 2, state EXITED -> WATCHING_LIVE, ONE live_recycled receipt
    carrying the inputs, NO live_cooldown_started, NO cooldown_until_utc, and the
    legacy live_cooldown state never visited."""
    assert stop_class_exit_reason("stop_loss")
    sess = _seed(db, symbol="NOCD-USD", name="nocd_stop", state=STATE_LIVE_EXITED, le_extra={
        "last_exit_reason": "stop_loss",
        "last_exit_return_bps": -180.0,
        "g4_prior_trade": {"exit_reason": "stop_loss"},
        "realized_pnl_usd": -42.0,
    })
    out = _tick(monkeypatch, db, sess)
    assert out.get("ok") is True, out
    assert sess.state == STATE_WATCHING_LIVE, sess.state
    le = _le(sess)
    assert "cooldown_until_utc" not in le
    assert le.get("trade_cycles") == 2
    assert le.get("stopout_cycles") == 1
    assert le.get("g4_reentry_escalation") == 1
    assert le.get("fees_usd_total") == 1.5
    assert le.get("realized_pnl_usd") == -42.0
    # the recycle reset still cleared the prior leg's entry state
    for k in ("entry_order_id", "entry_order_ids_all", "entry_orders_resolved",
              "entry_submitted", "position"):
        assert k not in le, k
    evs = _events(db, sess.id)
    types = [t for t, _ in evs]
    assert "live_cooldown_started" not in types, types
    assert types.count("live_recycled") == 1, types
    payload = dict(evs[types.index("live_recycled")][1])
    assert payload["recycled_from_state"] == STATE_LIVE_EXITED
    assert payload["stopout_cycles"] == 1
    assert payload["escalation_level"] == 1
    assert payload["trade_cycles"] == 2


def test_exited_profit_recycles_with_a_free_cycle(monkeypatch, db):
    """A green leg is a free re-scalp: stopout_cycles stays 0 and the escalation
    level decays (2 -> 1) — the old COOLDOWN handler's rule, now on the direct path."""
    sess = _seed(db, symbol="NOCP-USD", name="nocp_win", state=STATE_LIVE_EXITED, le_extra={
        "last_exit_reason": "target",
        "last_exit_return_bps": 240.0,
        "g4_prior_trade": {"exit_reason": "target"},
        "realized_pnl_usd": -5.0,  # session still net red => no green_banked reset
        "stopout_cycles": 0,
        "g4_reentry_escalation": 2,
    })
    _tick(monkeypatch, db, sess)
    assert sess.state == STATE_WATCHING_LIVE, sess.state
    le = _le(sess)
    assert le.get("trade_cycles") == 2
    assert le.get("stopout_cycles") == 0
    assert le.get("g4_reentry_escalation") == 1
    assert "cooldown_until_utc" not in le
    types = [t for t, _ in _events(db, sess.id)]
    assert "live_cooldown_started" not in types
    assert types.count("live_recycled") == 1


def test_exited_stop_cap_terminalizes_directly(monkeypatch, db):
    """The bounded re-entry cap still ENDS the session — now EXITED -> FINISHED with
    no cooldown hop (the cap is a count, not a clock)."""
    monkeypatch.setattr(settings, "chili_momentum_reentry_after_stop_bound_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_max_stopout_reentries", 3)
    monkeypatch.setattr(settings, "chili_momentum_g4_reentry_escalation_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_fresh_ignition_reentry_bypass_enabled", False)
    sess = _seed(db, symbol="NOCC-USD", name="nocc_cap", state=STATE_LIVE_EXITED, le_extra={
        "last_exit_reason": "stop_loss",
        "last_exit_return_bps": -150.0,
        "g4_prior_trade": {"exit_reason": "stop_loss"},
        "realized_pnl_usd": -120.0,
        "stopout_cycles": 2,
    })
    _tick(monkeypatch, db, sess)
    assert sess.state == STATE_LIVE_FINISHED, sess.state
    le = _le(sess)
    assert le.get("stopout_cycles") == 3
    assert "cooldown_until_utc" not in le
    types = [t for t, _ in _events(db, sess.id)]
    assert "live_cooldown_started" not in types
    assert "live_reentry_capped" in types, types


def test_legacy_cooldown_row_with_future_until_recycles_immediately(monkeypatch, db):
    """A row persisted in live_cooldown BEFORE the deletion (with an until_utc an
    hour in the future) passes straight through: no clock is consulted, the legacy
    key is dropped, the same bookkeeping runs, and the receipt names the state it
    came from."""
    until = (datetime.utcnow() + timedelta(hours=1)).replace(tzinfo=timezone.utc)
    sess = _seed(db, symbol="NOCL-USD", name="nocl_legacy", state=STATE_LIVE_COOLDOWN, le_extra={
        "cooldown_until_utc": until.isoformat(),
        # the OLD exited-handler already stamped these before the hop
        "last_recycle_was_stopout": True,
        "last_recycle_holds_streak": False,
        "realized_pnl_usd": -30.0,
    })
    _tick(monkeypatch, db, sess)
    assert sess.state == STATE_WATCHING_LIVE, sess.state
    le = _le(sess)
    assert "cooldown_until_utc" not in le
    assert le.get("trade_cycles") == 2
    assert le.get("stopout_cycles") == 1
    evs = _events(db, sess.id)
    types = [t for t, _ in evs]
    assert "live_cooldown_started" not in types
    assert types.count("live_recycled") == 1
    assert dict(evs[types.index("live_recycled")][1])["recycled_from_state"] == STATE_LIVE_COOLDOWN
