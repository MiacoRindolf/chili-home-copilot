"""Autonomous auto-arm-live guard + selection logic (Ross-style)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.trading.momentum_neural.auto_arm as aa
from app.services import coinbase_service
from app.services.trading import governance, portfolio_risk
from app.services.trading.momentum_neural import automation_query, operator_actions


class _FakeDB:
    def add(self, *_a, **_k) -> None:
        pass

    def commit(self) -> None:
        pass


def _cand(symbol="RSC-USD", variant_id=8, score=0.61):
    return SimpleNamespace(symbol=symbol, variant_id=variant_id, viability_score=score)


@pytest.fixture
def happy(monkeypatch):
    """Patch every seam to the happy path; tests override one to exercise a guard."""
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_scheduler_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_autotrader_user_id", 1, raising=False)
    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 0)
    monkeypatch.setattr(portfolio_risk, "check_portfolio_drawdown_breaker", lambda db, uid: (False, None))
    monkeypatch.setattr(automation_query, "expire_stale_live_arm_sessions", lambda db, *, user_id: 0)
    monkeypatch.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: [_cand()])
    monkeypatch.setattr(aa, "_symbol_free", lambda db, sym, uid: True)
    monkeypatch.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    monkeypatch.setattr(coinbase_service, "connect", lambda: {"ok": True})
    monkeypatch.setattr(
        operator_actions, "begin_live_arm",
        lambda db, **k: {"ok": True, "arm_token": "tok", "session_id": 99},
    )
    monkeypatch.setattr(
        operator_actions, "confirm_live_arm",
        lambda db, **k: {"ok": True, "state": "queued_live"},
    )
    return monkeypatch


def test_happy_path_arms(happy):
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "RSC-USD"
    assert out["session_id"] == 99
    assert out["state"] == "queued_live"


def test_flag_off_skips(happy):
    happy.setattr(aa.settings, "chili_momentum_auto_arm_live_enabled", False, raising=False)
    assert aa.run_auto_arm_pass(_FakeDB())["skipped"] == "flag_off"


def test_live_runner_off_skips(happy):
    happy.setattr(aa.settings, "chili_momentum_live_runner_enabled", False, raising=False)
    assert aa.run_auto_arm_pass(_FakeDB())["skipped"] == "live_runner_off"


def test_kill_switch_skips(happy):
    happy.setattr(governance, "is_kill_switch_active", lambda: True)
    assert aa.run_auto_arm_pass(_FakeDB())["skipped"] == "kill_switch"


def test_concurrency_skips(happy):
    happy.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 1)
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "live_session_active"
    assert out["active"] == 1


def test_drawdown_breaker_skips(happy):
    happy.setattr(portfolio_risk, "check_portfolio_drawdown_breaker", lambda db, uid: (True, "dd_15pct"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "drawdown_breaker"
    assert out["dd_reason"] == "dd_15pct"


def test_no_candidates_skips(happy):
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: [])
    assert aa.run_auto_arm_pass(_FakeDB())["skipped"] == "no_fresh_live_eligible"


def test_no_active_trigger_skips(happy):
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (False, "waiting_for_break"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "no_active_trigger"
    assert out["scanned"] == 1


def test_symbol_owned_by_other_skips_candidate(happy):
    happy.setattr(aa, "_symbol_free", lambda db, sym, uid: False)
    out = aa.run_auto_arm_pass(_FakeDB())
    # the only candidate is owned by another autopilot -> nothing arms
    assert out["skipped"] == "no_active_trigger"


def test_begin_blocked_does_not_arm(happy):
    happy.setattr(operator_actions, "begin_live_arm", lambda db, **k: {"ok": False, "error": "risk_blocked"})
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 0
    assert out["skipped"] == "begin_blocked"
    assert out["begin_error"] == "risk_blocked"


def test_confirm_blocked_does_not_arm(happy):
    happy.setattr(operator_actions, "confirm_live_arm", lambda db, **k: {"ok": False, "error": "broker_not_ready"})
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 0
    assert out["skipped"] == "confirm_blocked"
    assert out["confirm_error"] == "broker_not_ready"


def test_dedupe_by_symbol_keeps_best_variant_distinct_symbols():
    # 10 RSC variants (top), then FIDA, then SOL — dedupe must yield 3 distinct symbols
    rows = (
        [_cand("RSC-USD", v, 0.65) for v in range(1, 11)]
        + [_cand("FIDA-USD", 2, 0.63)]
        + [_cand("SOL-USD", 5, 0.61)]
    )
    out = aa._dedupe_by_symbol(rows, limit=10)
    syms = [r.symbol for r in out]
    assert syms == ["RSC-USD", "FIDA-USD", "SOL-USD"]  # one per symbol, order preserved


def test_dedupe_respects_limit():
    rows = [_cand(f"S{i}-USD", 1, 0.6 - i * 0.01) for i in range(20)]
    out = aa._dedupe_by_symbol(rows, limit=5)
    assert len(out) == 5
    assert [r.symbol for r in out] == [f"S{i}-USD" for i in range(5)]


def test_picks_first_firing_candidate(happy):
    cands = [_cand("AAA-USD", 8, 0.70), _cand("BBB-USD", 8, 0.65), _cand("CCC-USD", 8, 0.60)]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: cands)
    # only BBB is surging now
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (sym == "BBB-USD", "pullback_break_ok" if sym == "BBB-USD" else "waiting_for_break"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "BBB-USD"
