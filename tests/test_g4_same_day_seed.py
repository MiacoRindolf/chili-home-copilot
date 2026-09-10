"""THE RE-ENTRY RAMP MUST BIND (C): a new session on the same symbol-day is not a
new name.

Escalation state (``g4_reentry_escalation``, ``stopout_cycles``, the
``g4_prior_trade`` reference) is per-SESSION. SKYQ 2026-09-10: session 21591
reached level 3 and was capped at 14:02:11; session 21605 was armed at 14:04:32
at level 0. The cross-day seed (#1252) only reads the PRIOR day.

``same_day_escalation_seed`` returns the MAX level / stopout_cycles over the
symbol's OTHER live sessions today plus the prior-trade stash of the one with
the latest exit, and the runner applies it once per session with a
``g4_same_day_seed`` receipt carrying the source session id.

Runnable: pytest tests/test_g4_same_day_seed.py -v
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.risk_policy import same_day_escalation_seed

_SRC = pathlib.Path(LR.__file__)
_seq = 0


def _variant(db):
    global _seq
    _seq += 1
    v = MomentumStrategyVariant(family="test_family", variant_key=f"sameday_{_seq}",
                                label="same-day seed variant", params_json={})
    db.add(v)
    db.flush()
    return v


def _session(db, *, symbol, le, mode="live", started_at=None, family="alpaca_spot"):
    v = _variant(db)
    s = TradingAutomationSession(
        user_id=None, venue="test", execution_family=family, mode=mode, symbol=symbol,
        variant_id=v.id, state="live_finished",
        risk_snapshot_json={"momentum_live_execution": dict(le)},
        correlation_id="corr-sameday",
    )
    if started_at is not None:
        s.started_at = started_at
        s.created_at = started_at
    db.add(s)
    db.flush()
    return s


NOW = datetime(2026, 9, 10, 18, 4, 32)  # 14:04:32 ET, the SKYQ 21605 arm instant


def test_seed_is_the_max_over_todays_other_sessions_with_the_latest_reference(db):
    a = _session(db, symbol="SKYQ", started_at=NOW - timedelta(hours=2), le={
        "g4_reentry_escalation": 3, "stopout_cycles": 3,
        "g4_prior_trade": {"exit_price": 3.62, "high_water_mark": 3.64, "risk_dist": 0.28,
                           "was_loss": True, "exit_reason": "momentum_break_stop",
                           "exited_at_utc": "2026-09-10T18:01:50", "entry_filled_at_utc": "2026-09-10T17:58:18+00:00"},
    })
    b = _session(db, symbol="SKYQ", started_at=NOW - timedelta(hours=4), le={
        "g4_reentry_escalation": 1, "stopout_cycles": 1,
        "g4_prior_trade": {"exit_price": 3.30, "exited_at_utc": "2026-09-10T15:00:00"},
    })
    me = _session(db, symbol="SKYQ", started_at=NOW, le={})
    out = same_day_escalation_seed(db, symbol="SKYQ", exclude_session_id=me.id, as_of_utc=NOW)
    assert out["level"] == 3
    assert out["stopout_cycles"] == 3
    assert out["source_session_id"] == a.id
    assert out["sessions_seen"] == 2
    assert out["prior_trade"]["exit_price"] == 3.62, "the LATEST exit's reference travels with the level"
    assert b.id != out["source_session_id"]


def test_seed_excludes_itself_other_symbols_paper_and_yesterday(db):
    me = _session(db, symbol="SKYQ", started_at=NOW, le={"g4_reentry_escalation": 9, "stopout_cycles": 9})
    _session(db, symbol="TNON", started_at=NOW - timedelta(hours=1), le={"g4_reentry_escalation": 4, "stopout_cycles": 4})
    _session(db, symbol="SKYQ", mode="paper", started_at=NOW - timedelta(hours=1), le={"g4_reentry_escalation": 4, "stopout_cycles": 4})
    _session(db, symbol="SKYQ", started_at=NOW - timedelta(days=1), le={"g4_reentry_escalation": 4, "stopout_cycles": 4})
    out = same_day_escalation_seed(db, symbol="SKYQ", exclude_session_id=me.id, as_of_utc=NOW)
    assert out == {"level": 0, "stopout_cycles": 0, "source_session_id": None, "prior_trade": None, "sessions_seen": 0}


def test_seed_is_as_of_bounded(db):
    """A session started AFTER the decision instant is not visible (replay parity)."""
    me = _session(db, symbol="SKYQ", started_at=NOW, le={})
    _session(db, symbol="SKYQ", started_at=NOW + timedelta(minutes=5), le={"g4_reentry_escalation": 4, "stopout_cycles": 4})
    out = same_day_escalation_seed(db, symbol="SKYQ", exclude_session_id=me.id, as_of_utc=NOW)
    assert out["level"] == 0 and out["sessions_seen"] == 0


def test_seed_fails_open_without_a_db():
    out = same_day_escalation_seed(None, symbol="SKYQ")
    assert out["level"] == 0 and out["stopout_cycles"] == 0


# ── the runner applies it once, with a receipt ───────────────────────────────


def test_runner_seeds_level_cycles_and_reference_with_a_receipt(monkeypatch):
    now = datetime(2026, 9, 10, 18, 4, 40)
    monkeypatch.setattr(LR, "_utcnow", lambda: now)
    monkeypatch.setattr(LR, "_replay_l2_as_of_or_none", lambda: now)
    emitted = []
    monkeypatch.setattr(LR, "_emit", lambda db, sess, et, payload: emitted.append((et, payload)))
    commits = []
    monkeypatch.setattr(LR, "_commit_le", lambda sess, le: commits.append(dict(le)))
    prior = {"exit_price": 3.62, "high_water_mark": 3.64, "risk_dist": 0.28, "was_loss": True,
             "exit_reason": "momentum_break_stop", "exited_at_utc": "2026-09-10T18:01:50",
             "entry_filled_at_utc": "2026-09-10T17:58:18+00:00"}
    monkeypatch.setattr(LR, "same_day_escalation_seed", lambda *a, **k: {
        "level": 3, "stopout_cycles": 3, "source_session_id": 21591, "prior_trade": prior, "sessions_seen": 1})
    monkeypatch.setattr(LR, "_own_tape_noise_floor_pct", lambda db, s, entry_price: (0.01, 10))
    monkeypatch.setattr(EG, "signed_tape_accel_features", lambda *a, **k: {
        "signed_tape_accel": -5000.0, "back_buy_share": 0.4, "buy_share_delta": -0.1, "prints_since_high": 3, "n_ticks": 255})
    monkeypatch.setattr(EG, "prior_leg_high_print", lambda *a, **k: (3.66, 900))
    le = {"g4_leader_min": now.strftime("%Y%m%d%H%M"), "g4_leader_is": False}
    sess = SimpleNamespace(id=21605, symbol="SKYQ", execution_family="alpaca_spot")
    via = SimpleNamespace(viability_score=0.5)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="pullback_break_tick_ok", tick_px=3.68)
    assert le["g4_reentry_escalation"] == 3
    assert le["stopout_cycles"] == 3
    assert le["g4_prior_trade"]["exit_price"] == 3.62
    assert le["g4_escalation_seed_checked"] is True
    seeds = [p for et, p in emitted if et == "g4_same_day_seed"]
    assert len(seeds) == 1
    assert seeds[0]["source_session_id"] == 21591
    assert seeds[0]["seed_level"] == 3 and seeds[0]["seed_stopout_cycles"] == 3
    assert seeds[0]["prior_trade_seeded"] is True
    # and the bar binds immediately at the seeded level: 3.66 + 2R = 4.22 vs 3.68
    assert lvl == 3 and ok is False
    assert dbg["required_reclaim"] == 4.22
    assert dbg["reference_kind"] == "prior_leg_high_print"


def test_runner_seeds_the_reference_at_level_zero_after_a_green_leg(monkeypatch):
    """[59] (2026-09-10): the REFERENCE travels without the level. A new session on a
    symbol-day whose only earlier leg was GREEN seeds level 0 / cycles 0 — and the
    prior leg's stash, so the level-0 bar (a print above that leg's high print) binds
    at once. SKYQ 09-10 13:55Z: prior leg high print 3.80, last print 3.65 => WAIT."""
    now = datetime(2026, 9, 10, 13, 55, 34)
    monkeypatch.setattr(LR, "_utcnow", lambda: now)
    monkeypatch.setattr(LR, "_replay_l2_as_of_or_none", lambda: None)
    emitted = []
    monkeypatch.setattr(LR, "_emit", lambda db, sess, et, payload: emitted.append((et, payload)))
    monkeypatch.setattr(LR, "_commit_le", lambda sess, le: None)
    prior = {"exit_price": 3.72, "high_water_mark": 3.79, "risk_dist": 0.20, "was_loss": False,
             "exit_reason": "burst_window_exit", "exited_at_utc": "2026-09-10T13:53:29",
             "entry_filled_at_utc": "2026-09-10T13:50:56+00:00"}
    monkeypatch.setattr(LR, "same_day_escalation_seed", lambda *a, **k: {
        "level": 0, "stopout_cycles": 0, "source_session_id": 21591, "prior_trade": prior, "sessions_seen": 1})
    from app.services.trading.momentum_neural import risk_policy as RP
    monkeypatch.setattr(RP, "prior_day_rejection_seed", lambda db, s: 0)
    monkeypatch.setattr(EG, "signed_tape_accel_features", lambda *a, **k: {
        "signed_tape_accel": -3269.0, "back_buy_share": 0.5, "buy_share_delta": 0.0452, "prints_since_high": 243,
        "n_ticks": 255, "last_print": 3.65, "last_bid": 3.65, "last_ask": 3.66})
    monkeypatch.setattr(EG, "prior_leg_high_print", lambda *a, **k: (3.80, 12425))
    le = {}
    sess = SimpleNamespace(id=21605, symbol="SKYQ", execution_family="alpaca_spot")
    via = SimpleNamespace(viability_score=0.5)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=3.66)
    assert le["g4_prior_trade"]["exit_price"] == 3.72, "the reference was carried at level 0"
    assert "g4_reentry_escalation" not in le or int(le.get("g4_reentry_escalation") or 0) == 0
    seeds = [p for et, p in emitted if et == "g4_same_day_seed"]
    assert len(seeds) == 1
    assert seeds[0]["seed_level"] == 0 and seeds[0]["prior_trade_seeded"] is True
    assert seeds[0]["prior_trade_was_loss"] is False
    # and the level-0 bar binds immediately on the seeded reference
    assert (ok, lvl) == (False, 0)
    assert dbg["reason"] == "reclaim_of_prior_leg_high_wait"
    assert dbg["reference_kind"] == "prior_leg_high_print" and dbg["required_reclaim"] == 3.80
    assert dbg["price"] == 3.65 and dbg["price_kind"] == "last_print"


def test_runner_seeds_only_once_per_session(monkeypatch):
    now = datetime(2026, 9, 10, 18, 4, 40)
    monkeypatch.setattr(LR, "_utcnow", lambda: now)
    monkeypatch.setattr(LR, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(LR, "_commit_le", lambda sess, le: None)
    calls = []
    monkeypatch.setattr(LR, "same_day_escalation_seed", lambda *a, **k: calls.append(1) or {
        "level": 0, "stopout_cycles": 0, "source_session_id": None, "prior_trade": None, "sessions_seen": 0})
    from app.services.trading.momentum_neural import risk_policy as RP
    monkeypatch.setattr(RP, "prior_day_rejection_seed", lambda db, s: 0)
    le = {}
    sess = SimpleNamespace(id=1, symbol="SKYQ", execution_family="alpaca_spot")
    via = SimpleNamespace(viability_score=0.5)
    for _ in range(3):
        LR._g4_reentry_escalation_check(None, sess, le, via, trigger_reason="pullback_break_tick_ok", tick_px=1.0)
    assert calls == [1], "one read per session, not one per tick"
    assert le["g4_escalation_seed_checked"] is True


def test_the_same_day_seed_runs_before_the_cross_day_seed():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("def _g4_reentry_escalation_check(")
    body = src[i: i + 12000]
    assert body.index("same_day_escalation_seed(") < body.index("prior_day_rejection_seed")
    assert '"g4_same_day_seed"' in body
    assert '"source_session_id"' in body
