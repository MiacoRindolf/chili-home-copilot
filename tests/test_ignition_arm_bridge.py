"""IGNITION→ARM BRIDGE (2026-08-19 YJ miss): scoped auto-arm pass tests.

Ang ignite job ay nailalagay ang isang crosser sa viability board sa loob ng
5-15s, pero ang ARM ay naghihintay sa susunod na FULL pass — sinukat na 300-1200s
kontra 10s cadence, kaya ang 12:28Z YJ ignition leg ay tumakbo nang WALANG live
session. Ang bridge = parehong run_auto_arm_pass na naka-scope sa ignited
symbols; LAHAT ng guard ay tumatakbo pa rin, at ang mga NILALAKTAWAN lang ay
strictly risk-reducing (snapshot build, watching-reaper, displacement, at ang
board-leader privileges).

Fixture pattern mula sa test_loss_cooldown_leader_exemption.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import app.services.trading.momentum_neural.auto_arm as aa
from app.services import coinbase_service
from app.services.trading import governance, portfolio_risk
from app.services.trading.momentum_neural import (
    automation_query,
    operator_actions,
    risk_policy,
)
from app.services.trading.venue import account_identity


class _FakeDB:
    def add(self, *_a, **_k) -> None:
        pass

    def commit(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def expunge_all(self) -> None:
        pass

    def query(self, *_a, **_k):
        return self

    def filter(self, *_a, **_k):
        return self

    def all(self):
        return []

    def first(self):
        return None

    def execute(self, *_a, **_k):
        return SimpleNamespace(fetchall=lambda: [], fetchone=lambda: None, scalar=lambda: None)

    def get(self, *_a, **_k):
        return None


def _cand(symbol: str = "LGVN-USD", score: float = 0.70):
    return SimpleNamespace(
        symbol=symbol,
        variant_id=8,
        viability_score=score,
        execution_readiness_json={},
    )


def _happy_path(monkeypatch, *, candidates):
    """Lahat ng seam papunta sa isang matagumpay na arm — kopyang pattern ng
    leader-exemption test, dagdag ang crypto-liquidity + venue-ready mocks para
    DETERMINISTIC ang armed=1 (hindi umaasa sa unmocked na downstream probe)."""
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_scheduler_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_autotrader_user_id", 1, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_decouple_watching_enabled", False, raising=False)
    # Ang -USD fixture symbol ay dumadaan sa crypto live-arm gate chain — buksan
    # ito para DETERMINISTIC ang armed=1 (hindi clock/flag dependent).
    monkeypatch.setattr(aa.settings, "chili_momentum_crypto_live_arm_enabled", True, raising=False)
    monkeypatch.setattr(aa, "_crypto_paused_us_session", lambda: False)
    from app.services.trading.momentum_neural import market_profile as _mkt

    monkeypatch.setattr(_mkt, "crypto_schedule_enabled", lambda: False)
    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 0)
    monkeypatch.setattr(portfolio_risk, "check_portfolio_drawdown_breaker", lambda db, uid: (False, None))
    monkeypatch.setattr(automation_query, "expire_stale_live_arm_sessions", lambda db, *, user_id: 0)

    def _fetch(db, *, limit, ross_universe_symbols=None, as_of_utc=None,
               only_symbols=None, viability_max_age_override=None):
        _fetch.calls.append(
            {
                "only_symbols": only_symbols,
                "viability_max_age_override": viability_max_age_override,
                "ross_universe_symbols": ross_universe_symbols,
            }
        )
        if only_symbols is not None:
            return [c for c in candidates if str(c.symbol).upper() in only_symbols]
        return list(candidates)

    _fetch.calls = []
    monkeypatch.setattr(aa, "_fresh_live_eligible_candidates", _fetch)
    monkeypatch.setattr(aa, "_symbol_free", lambda db, sym, uid: True)
    monkeypatch.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    monkeypatch.setattr(aa, "_candidate_freshness", lambda sym: None)
    monkeypatch.setattr(aa, "crypto_liquidity_ok", lambda sym, c, adapter=None: (True, {}, None))
    monkeypatch.setattr(aa, "_venue_broker_ready_for", lambda sym, cache: True)
    monkeypatch.setattr(aa, "_symbol_loss_guards", lambda db, **kwargs: (set(), {}))
    monkeypatch.setattr(
        account_identity,
        "read_current_non_alpaca_account_identity",
        lambda _family: {"ok": True, "identity": "ignition-bridge-test-v1", "reason": None},
    )
    monkeypatch.setattr(
        risk_policy,
        "load_current_live_loss_history",
        lambda db, **kwargs: (
            (),
            {
                "history_available": True,
                "coverage_grade": "CURRENT_LIVE_COMPLETE",
                "replay_certifiable": False,
            },
        ),
    )
    monkeypatch.setattr(
        risk_policy,
        "consecutive_loss_halt_decision",
        lambda db, **kwargs: (False, {"halted": False, "history_available": True, "config_provenance": {}}),
    )
    monkeypatch.setattr(coinbase_service, "connect", lambda: {"ok": True})
    monkeypatch.setattr(
        operator_actions, "begin_live_arm",
        lambda db, **k: {"ok": True, "arm_token": "tok", "session_id": 99},
    )
    monkeypatch.setattr(
        operator_actions, "confirm_live_arm",
        lambda db, **k: {"ok": True, "state": "queued_live"},
    )
    return _fetch


def _far_future():
    return datetime.utcnow() + timedelta(minutes=15)


# ────────────────────────── scoped run_auto_arm_pass ──────────────────────────


def test_scoped_pass_arms_ignited_symbol(monkeypatch):
    """Happy path: naka-scope sa kaka-ignite na simbolo -> ARMED, at ang fetch ay
    dumaan sa scoped path (only_symbols + tightened freshness override)."""
    fetch = _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"lgvn-usd"})
    assert out.get("scoped_ignition") == ["LGVN-USD"], out
    assert out.get("armed", 0) == 1, out
    assert len(fetch.calls) == 1
    assert fetch.calls[0]["only_symbols"] == frozenset({"LGVN-USD"})
    assert fetch.calls[0]["viability_max_age_override"] == 90.0


def test_scoped_pass_empty_symbols_is_noop(monkeypatch):
    _happy_path(monkeypatch, candidates=[_cand()])
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"", "  "})
    assert out.get("skipped") == "scoped_no_symbols", out
    assert out.get("armed", 0) == 0


def test_scoped_pass_kill_switch_still_blocks(monkeypatch):
    """INVARIANT: bawat account-level guard ay tumatakbo pa rin sa scoped mode."""
    _happy_path(monkeypatch, candidates=[_cand()])
    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: True)
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD"})
    assert out.get("skipped") == "kill_switch", out
    assert out.get("armed", 0) == 0


def test_scoped_pass_no_leader_cooldown_exemption(monkeypatch):
    """INVARIANT: ang ignited symbol ay HINDI board #1 — ang loss-cooldown TIMER
    ay HINDI ine-exempt sa scoped mode (kung hindi, ang re-ignition ay magiging
    cooldown bypass)."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    monkeypatch.setattr(
        aa, "_symbol_loss_guards",
        lambda db, **kwargs: (set(), {"LGVN-USD": _far_future()}),
    )
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD"})
    assert out.get("armed", 0) == 0, out
    assert out.get("loss_guard_skipped", 0) >= 1, out
    assert out.get("loss_cooldown_leader_exempt", 0) == 0, out


def test_full_pass_leader_exemption_unchanged(monkeypatch):
    """PARITY: walang only_symbols -> ang board-#1 TIMER exemption ay buhay pa rin
    (ang scoped restriction ay hindi tumagas sa full pass)."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    monkeypatch.setattr(
        aa, "_symbol_loss_guards",
        lambda db, **kwargs: (set(), {"LGVN-USD": _far_future()}),
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("loss_cooldown_leader_exempt", 0) >= 1, out
    assert out.get("loss_guard_skipped", 0) == 0, out


def test_scoped_pass_skips_snapshot_build_and_watching_reaper(monkeypatch):
    """Ang bigat mismo ng pass (full-market snapshot + watching-reaper) ang
    nilalaktawan ng scoped mode — iyon ang buong punto ng bridge."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    calls = {"snapshot": 0, "reaper": 0}
    monkeypatch.setattr(aa, "_auto_arm_equity_only", lambda: True)
    monkeypatch.setattr(
        aa, "_ross_snapshot_rows_by_symbol",
        lambda: calls.__setitem__("snapshot", calls["snapshot"] + 1) or {},
    )
    monkeypatch.setattr(
        aa, "_reap_stale_watching_sessions",
        lambda db, *, user_id, now: calls.__setitem__("reaper", calls["reaper"] + 1) or 0,
    )
    aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD"})
    assert calls == {"snapshot": 0, "reaper": 0}, calls
    # PARITY: ang full pass ay tumatawag pa rin sa pareho.
    aa.run_auto_arm_pass(_FakeDB())
    assert calls["snapshot"] >= 1 and calls["reaper"] == 1, calls


def test_scoped_pass_no_displacement_on_full_slots(monkeypatch):
    """SCOPED: kapag puno ang slots, skip lang — hindi nag-e-evict ng watcher
    (ang displacement ay nangangailangan ng full-board rank context)."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    monkeypatch.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 99)
    called = {"displace": 0}
    monkeypatch.setattr(
        aa, "_try_displacement_for_full_slots",
        lambda db, *, uid, out: called.__setitem__("displace", called["displace"] + 1) or True,
    )
    out = aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD"})
    assert out.get("skipped") == "live_session_active", out
    assert called["displace"] == 0, called


def test_scoped_pass_no_rotation_telemetry(monkeypatch):
    """SCOPED: hindi tina-tatakan ang leader-rotation telemetry — ang ignited
    symbol sa slot 0 ng scoped list ay hindi totoong board-leader change."""
    _happy_path(monkeypatch, candidates=[_cand("LGVN-USD")])
    called = {"rotation": 0}
    monkeypatch.setattr(
        aa, "_emit_leader_rotation_if_changed",
        lambda db, sym: called.__setitem__("rotation", called["rotation"] + 1),
    )
    aa.run_auto_arm_pass(_FakeDB(), only_symbols={"LGVN-USD"})
    assert called["rotation"] == 0, called
    aa.run_auto_arm_pass(_FakeDB())
    assert called["rotation"] == 1, called


# ────────────────────────── run_scoped_ignition_arm ──────────────────────────


def _reset_debounce(monkeypatch):
    monkeypatch.setattr(aa, "_IGNITION_BRIDGE_LAST_ATTEMPT", {})


def test_bridge_flag_off_never_invokes_pass(monkeypatch):
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", False, raising=False
    )
    called = {"pass": 0}
    monkeypatch.setattr(
        aa, "run_auto_arm_pass",
        lambda db, **k: called.__setitem__("pass", called["pass"] + 1) or {},
    )
    assert aa.run_scoped_ignition_arm(_FakeDB(), ["YJ"]) is None
    assert called["pass"] == 0


def test_bridge_invokes_scoped_pass_once_then_debounces(monkeypatch):
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False
    )
    seen: list[frozenset] = []
    monkeypatch.setattr(
        aa, "run_auto_arm_pass",
        lambda db, *, only_symbols=None, **k: seen.append(only_symbols) or {"armed": 0},
    )
    out1 = aa.run_scoped_ignition_arm(_FakeDB(), ["yj", " yj "])
    assert out1 is not None
    assert seen == [frozenset({"YJ"})]
    # Agad na pangalawang crossing sa loob ng debounce window -> walang pass.
    assert aa.run_scoped_ignition_arm(_FakeDB(), ["YJ"]) is None
    assert len(seen) == 1
    # Ibang simbolo -> hindi apektado ng YJ debounce.
    out3 = aa.run_scoped_ignition_arm(_FakeDB(), ["YJ", "RDAC"])
    assert out3 is not None
    assert seen[-1] == frozenset({"RDAC"})


def test_bridge_debounce_stamped_on_attempt_even_if_blocked(monkeypatch):
    """Ang debounce ay sa ATTEMPT, hindi sa tagumpay — ang begin_blocked na
    simbolo ay hindi maghahammer ng scoped pass bawat ignite cadence."""
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False
    )
    calls = {"pass": 0}
    monkeypatch.setattr(
        aa, "run_auto_arm_pass",
        lambda db, **k: calls.__setitem__("pass", calls["pass"] + 1)
        or {"armed": 0, "skipped": "begin_blocked"},
    )
    assert aa.run_scoped_ignition_arm(_FakeDB(), ["YJ"]) is not None
    assert aa.run_scoped_ignition_arm(_FakeDB(), ["YJ"]) is None
    assert calls["pass"] == 1


def test_bridge_empty_and_blank_symbols_noop(monkeypatch):
    _reset_debounce(monkeypatch)
    monkeypatch.setattr(
        aa.settings, "chili_momentum_ignition_arm_bridge_enabled", True, raising=False
    )
    called = {"pass": 0}
    monkeypatch.setattr(
        aa, "run_auto_arm_pass",
        lambda db, **k: called.__setitem__("pass", called["pass"] + 1) or {},
    )
    assert aa.run_scoped_ignition_arm(_FakeDB(), []) is None
    assert aa.run_scoped_ignition_arm(_FakeDB(), ["", None]) is None
    assert called["pass"] == 0
