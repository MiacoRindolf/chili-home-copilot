"""Autopilot mutual-exclusion lease + primary gate (P0.4).

These tests verify that a live AutoTrader v1 trade and a live momentum_neural
session cannot both issue orders on the same symbol at the same time. The
ownership "lease" is schema-based (no new table): we read the existing
Trade rows (auto_trader_version="v1", status="open") and TradingAutomationSession
rows (mode="live", state IN LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY).

Headline guarantees:
* ``find_symbol_owner`` correctly identifies the active owner from either path
  and ignores paper/terminal rows.
* ``check_autopilot_entry_gate`` blocks a foreign owner regardless of primary
  configuration.
* Strict-primary mode blocks the non-primary even on a free symbol.
* An integration tick through AutoTrader v1's ``_process_one_alert`` records
  an ``autopilot_mutex`` block in ``AutoTraderRun`` when momentum owns the
  symbol — the cross-autopilot case is the whole point of this ticket.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from app import models
from app.models.trading import (
    AutoTraderRun,
    BreakoutAlert,
    MomentumStrategyVariant,
    ScanPattern,
    Trade,
    TradingAutomationSession,
)
from app.services.trading import autopilot_scope
from app.services.trading.autopilot_scope import (
    AUTOPILOT_AUTO_TRADER_V1,
    AUTOPILOT_MOMENTUM_NEURAL,
    check_autopilot_entry_gate,
    find_symbol_owner,
)
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_ENTERED,
    STATE_LIVE_FINISHED,
    STATE_LIVE_PENDING_ENTRY,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _make_user(db, name: str = "mutex_u") -> models.User:
    u = models.User(name=name)
    db.add(u)
    db.flush()
    return u


def _make_variant(db) -> MomentumStrategyVariant:
    v = MomentumStrategyVariant(
        family="test_mutex",
        variant_key="mutex_v",
        label="mutex variant",
        params_json={},
    )
    db.add(v)
    db.flush()
    return v


def _open_v1_trade(db, *, user_id: int, ticker: str = "ZZZ") -> Trade:
    t = Trade(
        user_id=user_id,
        ticker=ticker,
        direction="long",
        entry_price=10.0,
        quantity=5.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=15.0,
        auto_trader_version="v1",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_live_session(
    db,
    *,
    user_id: int,
    symbol: str,
    state: str = STATE_LIVE_ENTERED,
    mode: str = "live",
) -> TradingAutomationSession:
    v = _make_variant(db)
    sess = TradingAutomationSession(
        user_id=user_id,
        symbol=symbol,
        mode=mode,
        variant_id=v.id,
        state=state,
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


@pytest.fixture(autouse=True)
def _reset_primary_mode(monkeypatch):
    """Each test starts with a known primary config — momentum is primary by default.

    ``get_primary_autopilot()`` has a defensive heuristic: when the configured
    primary is momentum_neural but ``chili_momentum_live_runner_enabled=False``
    AND ``chili_autotrader_live_enabled=True``, it re-routes primary to v1 so
    the system doesn't block every entry when the configured primary isn't
    actually running. That heuristic is correct in production, but it makes
    gate tests non-deterministic when the .env flips either flag — the test
    thinks primary is momentum but the gate sees it as v1.

    Pinning ``chili_momentum_live_runner_enabled=True`` here prevents the
    re-route regardless of the other flag, so every test in this file gets a
    stable "primary=momentum_neural" resolution unless it explicitly
    monkeypatches ``get_primary_autopilot`` itself.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "chili_autopilot_primary", "momentum_neural", raising=False)
    monkeypatch.setattr(settings, "chili_autopilot_strict_primary", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True, raising=False)
    yield


# ---------------------------------------------------------------------------
# Unit tests — find_symbol_owner
# ---------------------------------------------------------------------------


def test_find_symbol_owner_returns_none_for_free_symbol(db):
    u = _make_user(db)
    result = find_symbol_owner(db, symbol="FREE", user_id=u.id)
    assert result["owner"] is None
    assert result["v1_open_trades"] == 0
    assert result["momentum_live_sessions"] == 0
    assert result["symbol"] == "FREE"


def test_find_symbol_owner_detects_v1_open_trade(db):
    u = _make_user(db)
    _open_v1_trade(db, user_id=u.id, ticker="AAA")
    result = find_symbol_owner(db, symbol="AAA", user_id=u.id)
    assert result["owner"] == AUTOPILOT_AUTO_TRADER_V1
    assert result["v1_open_trades"] == 1
    assert result["momentum_live_sessions"] == 0


def test_find_symbol_owner_detects_momentum_live_session(db):
    u = _make_user(db)
    _make_live_session(db, user_id=u.id, symbol="BBB", state=STATE_LIVE_ENTERED)
    result = find_symbol_owner(db, symbol="BBB", user_id=u.id)
    assert result["owner"] == AUTOPILOT_MOMENTUM_NEURAL
    assert result["v1_open_trades"] == 0
    assert result["momentum_live_sessions"] == 1


def test_find_symbol_owner_ignores_paper_momentum_session(db):
    u = _make_user(db)
    _make_live_session(db, user_id=u.id, symbol="CCC", state=STATE_LIVE_ENTERED, mode="paper")
    result = find_symbol_owner(db, symbol="CCC", user_id=u.id)
    assert result["owner"] is None
    assert result["momentum_live_sessions"] == 0


def test_find_symbol_owner_ignores_terminal_momentum_session(db):
    u = _make_user(db)
    _make_live_session(db, user_id=u.id, symbol="DDD", state=STATE_LIVE_FINISHED)
    result = find_symbol_owner(db, symbol="DDD", user_id=u.id)
    # Terminal state is NOT in LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY.
    assert result["owner"] is None
    assert result["momentum_live_sessions"] == 0


def test_find_symbol_owner_ignores_closed_v1_trade(db):
    u = _make_user(db)
    t = _open_v1_trade(db, user_id=u.id, ticker="EEE")
    t.status = "closed"
    t.exit_price = 11.0
    t.exit_date = datetime.utcnow()
    db.commit()
    result = find_symbol_owner(db, symbol="EEE", user_id=u.id)
    assert result["owner"] is None
    assert result["v1_open_trades"] == 0


def test_find_symbol_owner_respects_user_scope(db):
    """Two users holding the same ticker don't contend with each other."""
    u1 = _make_user(db, name="mutex_a")
    u2 = _make_user(db, name="mutex_b")
    _open_v1_trade(db, user_id=u1.id, ticker="FFF")
    # u2 asking about FFF sees no owner — the lease is per-user.
    result = find_symbol_owner(db, symbol="FFF", user_id=u2.id)
    assert result["owner"] is None
    # u1 asking sees themself.
    result = find_symbol_owner(db, symbol="FFF", user_id=u1.id)
    assert result["owner"] == AUTOPILOT_AUTO_TRADER_V1


def test_find_symbol_owner_is_case_insensitive(db):
    u = _make_user(db)
    _make_live_session(db, user_id=u.id, symbol="BTC-USD", state=STATE_LIVE_PENDING_ENTRY)
    # Asking with lowercase should match — helper normalizes to upper.
    result = find_symbol_owner(db, symbol="btc-usd", user_id=u.id)
    assert result["owner"] == AUTOPILOT_MOMENTUM_NEURAL
    assert result["symbol"] == "BTC-USD"


# ---------------------------------------------------------------------------
# Unit tests — check_autopilot_entry_gate
# ---------------------------------------------------------------------------


def test_gate_allows_primary_on_free_symbol(db, monkeypatch):
    u = _make_user(db)
    monkeypatch.setattr(
        autopilot_scope, "get_primary_autopilot", lambda: AUTOPILOT_MOMENTUM_NEURAL
    )
    gate = check_autopilot_entry_gate(
        db, candidate=AUTOPILOT_MOMENTUM_NEURAL, symbol="FREE1", user_id=u.id
    )
    assert gate["allowed"] is True
    assert gate["reason"] == "ok"
    assert gate["owner"] is None


def test_gate_allows_non_primary_on_free_symbol_non_strict(db, monkeypatch):
    u = _make_user(db)
    # Non-strict mode (default) — v1 can enter a free symbol even when
    # momentum is primary.
    gate = check_autopilot_entry_gate(
        db, candidate=AUTOPILOT_AUTO_TRADER_V1, symbol="FREE2", user_id=u.id
    )
    assert gate["allowed"] is True
    assert gate["reason"] == "ok"


def test_gate_blocks_non_primary_on_free_symbol_when_strict(db, monkeypatch):
    u = _make_user(db)
    monkeypatch.setattr(
        autopilot_scope, "get_strict_primary_mode", lambda: True
    )
    # momentum is primary (fixture default); v1 is non-primary.
    gate = check_autopilot_entry_gate(
        db, candidate=AUTOPILOT_AUTO_TRADER_V1, symbol="FREE3", user_id=u.id
    )
    assert gate["allowed"] is False
    assert gate["reason"] == "not_primary"
    assert gate["owner"] is None


def test_gate_allows_self_owner_for_scale_in(db):
    """v1 that already owns the symbol (existing open v1 trade) can ask again —
    the scale-in code path depends on this."""
    u = _make_user(db)
    _open_v1_trade(db, user_id=u.id, ticker="SELF")
    gate = check_autopilot_entry_gate(
        db, candidate=AUTOPILOT_AUTO_TRADER_V1, symbol="SELF", user_id=u.id
    )
    assert gate["allowed"] is True
    assert gate["reason"] == "owner_self"
    assert gate["owner"] == AUTOPILOT_AUTO_TRADER_V1


def test_gate_blocks_v1_when_momentum_owns_symbol(db):
    """Headline mutual-exclusion guarantee from momentum side."""
    u = _make_user(db)
    _make_live_session(db, user_id=u.id, symbol="BTC-USD", state=STATE_LIVE_ENTERED)
    gate = check_autopilot_entry_gate(
        db, candidate=AUTOPILOT_AUTO_TRADER_V1, symbol="BTC-USD", user_id=u.id
    )
    assert gate["allowed"] is False
    assert gate["reason"] == "symbol_owned_by_other"
    assert gate["owner"] == AUTOPILOT_MOMENTUM_NEURAL


def test_gate_blocks_momentum_when_v1_owns_symbol(db):
    """Headline mutual-exclusion guarantee from v1 side."""
    u = _make_user(db)
    _open_v1_trade(db, user_id=u.id, ticker="NVDA")
    gate = check_autopilot_entry_gate(
        db, candidate=AUTOPILOT_MOMENTUM_NEURAL, symbol="NVDA", user_id=u.id
    )
    assert gate["allowed"] is False
    assert gate["reason"] == "symbol_owned_by_other"
    assert gate["owner"] == AUTOPILOT_AUTO_TRADER_V1


def test_gate_blocks_even_when_primary_config_missing(db, monkeypatch):
    """Foreign-owner block is unconditional — does NOT depend on the primary setting."""
    u = _make_user(db)
    monkeypatch.setattr(autopilot_scope, "get_primary_autopilot", lambda: "")
    _make_live_session(db, user_id=u.id, symbol="ETH-USD", state=STATE_LIVE_ENTERED)
    gate = check_autopilot_entry_gate(
        db, candidate=AUTOPILOT_AUTO_TRADER_V1, symbol="ETH-USD", user_id=u.id
    )
    assert gate["allowed"] is False
    assert gate["reason"] == "symbol_owned_by_other"


def test_gate_unknown_candidate_not_blocked(db):
    """Defensive behavior — unknown candidate names return allowed=True so the gate
    doesn't break unrelated callers."""
    u = _make_user(db)
    gate = check_autopilot_entry_gate(
        db, candidate="something_else", symbol="XYZ", user_id=u.id
    )
    assert gate["allowed"] is True
    assert gate["reason"] == "unknown_candidate_allowed"


# ---------------------------------------------------------------------------
# Integration test — AutoTrader v1 _process_one_alert records mutex block.
# ---------------------------------------------------------------------------


def test_auto_trader_v1_blocks_entry_when_momentum_owns_symbol(db, monkeypatch):
    """Headline wiring guarantee: a live AutoTrader v1 entry attempt records an
    `autopilot_mutex` AutoTraderRun when momentum_neural has an active live
    session on the same symbol — and does NOT place a broker order."""
    from app.services.trading import auto_trader

    u = _make_user(db, name="mutex_v1_wiring")
    # Momentum owns MUTEX1 with an active live session.
    _make_live_session(db, user_id=u.id, symbol="MUTEX1", state=STATE_LIVE_ENTERED)
    # Minimal pattern row for the alert's FK integrity.
    pat = ScanPattern(name="mutex pat", rules_json={}, origin="user", asset_class="stock")
    db.add(pat)
    db.flush()
    alert = BreakoutAlert(
        scan_pattern_id=pat.id,
        ticker="MUTEX1",
        asset_type="stock",
        alert_tier="premium",
        score_at_alert=0.9,
        price_at_alert=10.0,
        indicator_snapshot={},
        signals_snapshot={},
        stop_loss=9.0,
        target_price=12.0,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    # Minimal runtime scaffolding — pretend live is effective and the rule
    # gate would otherwise pass. We intercept right at the mutex check.
    out = {"scaled_in": 0, "skipped": 0, "entered": 0}
    runtime = {"live_orders_effective": True, "paper_mode_effective": False}

    # Bypass quote/LLM/rule gate — we want to prove the mutex fires before
    # those, which is why blocked skipped count should increment.
    with patch.object(auto_trader, "_current_price", return_value=10.0), patch.object(
        auto_trader, "count_autotrader_v1_open", return_value=0
    ), patch.object(
        auto_trader, "autotrader_realized_pnl_today_et", return_value=0.0
    ), patch.object(
        auto_trader, "find_open_autotrader_trade", return_value=None
    ), patch.object(
        auto_trader, "find_open_autotrader_paper", return_value=None
    ), patch.object(
        auto_trader, "maybe_scale_in", return_value=None
    ), patch.object(
        auto_trader, "passes_rule_gate", return_value=(True, "ok", {}),
    ), patch.object(
        auto_trader, "run_revalidation_llm", return_value=(True, {}),
    ):
        auto_trader._process_one_alert(db, u.id, alert, out, runtime)

    # The mutex block path hits `_audit(..., decision="blocked", reason=f"autopilot_mutex:...")`
    # and increments out["skipped"].
    assert out["skipped"] == 1
    runs = db.query(AutoTraderRun).filter(AutoTraderRun.breakout_alert_id == alert.id).all()
    assert len(runs) == 1
    assert runs[0].decision == "blocked"
    assert (runs[0].reason or "").startswith("autopilot_mutex:")
    assert "symbol_owned_by_other" in (runs[0].reason or "")
    # No Trade row should have been created.
    trades = db.query(Trade).filter(Trade.ticker == "MUTEX1").all()
    assert trades == []


def test_auto_trader_v1_allowed_when_symbol_is_free(db, monkeypatch):
    """Counter-case to the block test: free symbol → no mutex audit row.

    We still stub-out the entry flow at `_execute_new_entry` so this test
    doesn't depend on broker wiring; the point is that the mutex gate does
    NOT block when there's no foreign owner.
    """
    from app.services.trading import auto_trader

    u = _make_user(db, name="mutex_v1_allowed")
    pat = ScanPattern(name="mutex free", rules_json={}, origin="user", asset_class="stock")
    db.add(pat)
    db.flush()
    alert = BreakoutAlert(
        scan_pattern_id=pat.id,
        ticker="CLEAR",
        asset_type="stock",
        alert_tier="premium",
        score_at_alert=0.9,
        price_at_alert=10.0,
        indicator_snapshot={},
        signals_snapshot={},
        stop_loss=9.0,
        target_price=12.0,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    out = {"scaled_in": 0, "skipped": 0, "entered": 0}
    runtime = {"live_orders_effective": True, "paper_mode_effective": False}

    # Stub _execute_new_entry so we don't hit the broker; the mutex check
    # should NOT fire and flow should proceed into _execute_new_entry.
    exec_spy = []

    def _fake_exec(db, uid, alert, px, snap, llm_snap, live, out):
        exec_spy.append({"uid": uid, "alert_id": alert.id, "live": live})
        out["entered"] = out.get("entered", 0) + 1

    with patch.object(auto_trader, "_current_price", return_value=10.0), patch.object(
        auto_trader, "count_autotrader_v1_open", return_value=0
    ), patch.object(
        auto_trader, "autotrader_realized_pnl_today_et", return_value=0.0
    ), patch.object(
        auto_trader, "find_open_autotrader_trade", return_value=None
    ), patch.object(
        auto_trader, "find_open_autotrader_paper", return_value=None
    ), patch.object(
        auto_trader, "maybe_scale_in", return_value=None
    ), patch.object(
        auto_trader, "passes_rule_gate", return_value=(True, "ok", {}),
    ), patch.object(
        auto_trader, "run_revalidation_llm", return_value=(True, {}),
    ), patch.object(
        auto_trader, "_execute_new_entry", side_effect=_fake_exec
    ):
        auto_trader._process_one_alert(db, u.id, alert, out, runtime)

    # Entry flow reached _execute_new_entry; no mutex block was recorded.
    assert out["entered"] == 1
    assert out["skipped"] == 0
    blocked_runs = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == alert.id, AutoTraderRun.decision == "blocked")
        .all()
    )
    assert blocked_runs == []
    assert exec_spy[0]["alert_id"] == alert.id
