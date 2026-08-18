"""Leader retry exemption sa post-loss cooldown TIMER (2026-08-18 Ross recap).

"I Was Red -$12k Before My Big Winner": ang pumalyang unang breakout ng
LEADING GAINER na hindi namatay ay pinasok ni Ross muli nang mas malaki — ang
+$74k winner. Ang cooldown TIMER (5-20 min adaptive) ang kumakain mismo ng
retry window na iyon. Ang exemption: board #1 lang, TIMER lang — ang 2-strike
day block (`loss_blocked`) ay nananatiling absoluto, at ang trigger seam ay may
sariling G4 structural escalation bago makapasok muli.

Fixture pattern mula sa test_ross_parity_scenarios.py::test_no_revenge_reentry_after_loss.
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


def _happy_path(monkeypatch, *, candidates):
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_live_scheduler_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(aa.settings, "chili_autotrader_user_id", 1, raising=False)
    monkeypatch.setattr(aa.settings, "chili_momentum_decouple_watching_enabled", False, raising=False)
    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 0)
    monkeypatch.setattr(portfolio_risk, "check_portfolio_drawdown_breaker", lambda db, uid: (False, None))
    monkeypatch.setattr(automation_query, "expire_stale_live_arm_sessions", lambda db, *, user_id: 0)
    monkeypatch.setattr(
        aa, "_fresh_live_eligible_candidates", lambda db, *, limit: list(candidates)
    )
    monkeypatch.setattr(aa, "_symbol_free", lambda db, sym, uid: True)
    monkeypatch.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "pullback_break_ok"))
    monkeypatch.setattr(aa, "_candidate_freshness", lambda sym: None)
    monkeypatch.setattr(
        account_identity,
        "read_current_non_alpaca_account_identity",
        lambda _family: {"ok": True, "identity": "leader-exempt-test-v1", "reason": None},
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


def _far_future():
    return datetime.utcnow() + timedelta(minutes=15)


def _lgvn():
    return SimpleNamespace(symbol="LGVN-USD", variant_id=8, viability_score=0.70)


def test_leader_timer_cooldown_exempted(monkeypatch):
    """Board #1 na TIMER LANG ang pumipigil -> pumapasok pa rin sa arm."""
    _happy_path(monkeypatch, candidates=[_lgvn()])
    monkeypatch.setattr(
        aa, "_symbol_loss_guards",
        lambda db, **kwargs: (set(), {"LGVN-USD": _far_future()}),
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    # Ang saklaw ng test na ito ay ang LOSS-GUARD seam lang: ang leader ay
    # dumaan (exempt counter tumaas, walang loss_guard skip). Ang kandidato ay
    # maaaring mahulog sa IBANG downstream seam sa fixture na ito (hal. crypto
    # illiquidity probe na hindi minomock) — hindi iyon bahagi ng exemption.
    assert out.get("loss_cooldown_leader_exempt", 0) >= 1, out
    assert out.get("loss_guard_skipped", 0) == 0, out


def test_two_strike_block_still_absolute_for_leader(monkeypatch):
    """Ang 2-strike day block ay disiplina ni Ross mismo — hindi ine-exempt."""
    _happy_path(monkeypatch, candidates=[_lgvn()])
    monkeypatch.setattr(
        aa, "_symbol_loss_guards",
        lambda db, **kwargs: ({"LGVN-USD"}, {}),
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("armed", 0) == 0, out
    assert out.get("loss_guard_skipped", 0) >= 1, out


def test_timer_cooldown_still_blocks_non_leader(monkeypatch):
    """Ang exemption ay para LANG sa board #1 — ang pangalawang kandidato sa
    timer cooldown ay nananatiling naka-skip."""
    leader = SimpleNamespace(symbol="AAAA-USD", variant_id=8, viability_score=0.80)
    _happy_path(monkeypatch, candidates=[leader, _lgvn()])
    monkeypatch.setattr(
        aa, "_symbol_loss_guards",
        lambda db, **kwargs: (set(), {"LGVN-USD": _far_future()}),
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("loss_guard_skipped", 0) >= 1, out
    assert out.get("loss_cooldown_leader_exempt", 0) == 0, out


def test_flag_off_restores_timer_block_for_leader(monkeypatch):
    _happy_path(monkeypatch, candidates=[_lgvn()])
    monkeypatch.setattr(
        aa.settings,
        "chili_momentum_loss_cooldown_leader_exemption_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        aa, "_symbol_loss_guards",
        lambda db, **kwargs: (set(), {"LGVN-USD": _far_future()}),
    )
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("armed", 0) == 0, out
    assert out.get("loss_guard_skipped", 0) >= 1, out
