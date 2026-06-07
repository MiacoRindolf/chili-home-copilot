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
    # default max_concurrent_live_sessions is now 5 — full at 5 active
    happy.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 5)
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "live_session_active"
    assert out["active"] == 5


def test_arms_when_below_concurrency_cap(happy):
    # 3 active < 5 cap -> still arms a new one
    happy.setattr(aa, "_active_live_session_count", lambda db, *, user_id: 3)
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1


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


def test_deduped_begin_skips_confirm(happy):
    # begin_live_arm dedups (the symbol already holds an active live session):
    # it returns that session's token, whose session is no longer arm-pending.
    # The pass must NOT forward that stale token to confirm_live_arm (which
    # would fail invalid_token and churn) — it reports already_active instead.
    def _confirm_must_not_run(*a, **k):
        raise AssertionError("confirm_live_arm must not run on a deduped begin")

    happy.setattr(
        operator_actions,
        "begin_live_arm",
        lambda db, **k: {
            "ok": True,
            "deduped": True,
            "session_id": 77,
            "arm_token": "stale-token",
            "state": "watching_live",
        },
    )
    happy.setattr(operator_actions, "confirm_live_arm", _confirm_must_not_run)
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["skipped"] == "already_active"
    assert out["session_id"] == 77
    assert out.get("armed", 0) == 0


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


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_a, **_k):
        return self

    def all(self):
        return self._rows


class _DBWithRows(_FakeDB):
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_a, **_k):
        return _FakeQuery(self._rows)


def test_is_coinbase_tradeable_symbol():
    assert aa._is_coinbase_tradeable_symbol("KAIO-USD") is True
    assert aa._is_coinbase_tradeable_symbol("BTC-USDC") is True
    assert aa._is_coinbase_tradeable_symbol("ARKK") is False
    assert aa._is_coinbase_tradeable_symbol("CLSK") is False
    assert aa._is_coinbase_tradeable_symbol("") is False


def test_equity_candidate_skipped_even_if_higher_viability(happy):
    # ARKK (equity) ranks higher + its trigger fires, but the coinbase_spot lane
    # cannot trade it -> must be skipped; the crypto KAIO is armed instead.
    happy.setattr(
        aa, "_fresh_live_eligible_candidates",
        lambda db, *, limit: [_cand("ARKK", 8, 0.80), _cand("KAIO-USD", 8, 0.65)],
    )
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "momentum_ok"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "KAIO-USD"


def test_market_closed_equity_skipped(happy):
    # crypto_only OFF so equities can flow; an equity whose market is CLOSED must be
    # skipped (would not fill), the 24/7 crypto armed instead.
    happy.setattr(aa.settings, "chili_momentum_auto_arm_crypto_only", False, raising=False)
    happy.setattr(
        aa, "_fresh_live_eligible_candidates",
        lambda db, *, limit: [_cand("ARKK", 8, 0.80), _cand("KAIO-USD", 8, 0.65)],
    )
    happy.setattr(aa, "_symbol_market_open", lambda sym: sym.endswith("-USD"))
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (True, "momentum_ok"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "KAIO-USD"  # ARKK skipped: market closed


def test_market_open_helper_crypto_always_open(monkeypatch):
    # crypto is 24/7 -> always True regardless of market_open_now
    assert aa._symbol_market_open("BTC-USD") is True


def test_reaper_cancels_stale_pre_entry_sessions(monkeypatch):
    from datetime import datetime
    cancelled = []
    monkeypatch.setattr(
        operator_actions, "begin_live_arm", lambda *a, **k: {"ok": False},
    )  # unused
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query.cancel_automation_session",
        lambda db, *, user_id, session_id: cancelled.append(session_id) or {"ok": True},
    )
    rows = [
        SimpleNamespace(id=8, symbol="RSC-USD", state="watching_live"),
        SimpleNamespace(id=9, symbol="FIDA-USD", state="queued_live"),
    ]
    n = aa._reap_stale_watching_sessions(_DBWithRows(rows), user_id=1, now=datetime.utcnow())
    assert n == 2
    assert cancelled == [8, 9]


def test_reaper_returns_zero_when_none(monkeypatch):
    from datetime import datetime
    n = aa._reap_stale_watching_sessions(_DBWithRows([]), user_id=1, now=datetime.utcnow())
    assert n == 0


def test_pass_surfaces_reaped_count(happy):
    happy.setattr(aa, "_reap_stale_watching_sessions", lambda db, *, user_id, now: 1)
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out.get("reaped") == 1
    assert out["armed"] == 1  # reaping a stale slot then arming the fresh mover


def test_picks_first_firing_candidate(happy):
    cands = [_cand("AAA-USD", 8, 0.70), _cand("BBB-USD", 8, 0.65), _cand("CCC-USD", 8, 0.60)]
    happy.setattr(aa, "_fresh_live_eligible_candidates", lambda db, *, limit: cands)
    # only BBB is surging now
    happy.setattr(aa, "_entry_trigger_fires", lambda sym: (sym == "BBB-USD", "pullback_break_ok" if sym == "BBB-USD" else "waiting_for_break"))
    out = aa.run_auto_arm_pass(_FakeDB())
    assert out["armed"] == 1
    assert out["symbol"] == "BBB-USD"
