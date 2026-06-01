from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeBreakerDb:
    def __init__(self, row):
        self.row = row
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        self.params = params or {}
        return _FakeResult(self.row)

    def query(self, *_args, **_kwargs):
        raise AssertionError("persisted breaker must block before ORM queries")


class _EmptyQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return None


class _FakeResetBreakerDb(_FakeBreakerDb):
    def query(self, *_args, **_kwargs):
        return _EmptyQuery()


class _BrokenBreakerDb:
    def execute(self, *_args, **_kwargs):
        raise RuntimeError("risk-state table unavailable")

    def query(self, *_args, **_kwargs):
        raise AssertionError("durable breaker failure must block before ORM queries")


class _HeartbeatDb:
    def __init__(self):
        self.statements: list[str] = []
        self.params: list[dict] = []
        self.committed = False

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        self.params.append(params or {})

    def commit(self):
        self.committed = True

    def rollback(self):
        raise AssertionError("heartbeat snapshot should not roll back")


class _EmptyTradeQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return []


class _FakeDrawdownDb:
    def query(self, *_args, **_kwargs):
        return _EmptyTradeQuery()


def _reset_process_breaker() -> None:
    from app.services.trading import portfolio_risk

    portfolio_risk._breaker_tripped = False
    portfolio_risk._breaker_reason = None


def test_check_new_trade_allowed_blocks_latest_persisted_breaker_before_recompute() -> None:
    from app.services.trading import portfolio_risk
    from app.services.trading.portfolio_risk import check_new_trade_allowed

    reason = "test_persisted_breaker:entry_gate"
    db = _FakeBreakerDb((True, reason))
    _reset_process_breaker()

    try:
        with patch(
            "app.services.trading.governance.is_kill_switch_active",
            return_value=False,
        ), patch(
            "app.services.trading.portfolio_risk.check_drawdown_breaker",
            side_effect=AssertionError("persisted breaker must block before recompute"),
        ):
            ok, blocked_reason = check_new_trade_allowed(
                db,
                None,
                "SPY",
                capital=10_000.0,
            )

        assert ok is False
        assert blocked_reason == f"Circuit breaker active: {reason}"
        assert any("trading_risk_state" in stmt for stmt in db.statements)
        assert portfolio_risk.is_breaker_tripped() is True
        assert portfolio_risk.get_breaker_status()["reason"] == reason
    finally:
        _reset_process_breaker()


def test_circuit_breaker_entry_block_reason_uses_latest_durable_state() -> None:
    from app.services.trading import portfolio_risk
    from app.services.trading.portfolio_risk import (
        circuit_breaker_entry_block_reason,
    )

    reason = "test_persisted_breaker:final_submit_gate"
    db = _FakeBreakerDb((True, reason))
    _reset_process_breaker()

    try:
        blocked_reason = circuit_breaker_entry_block_reason(db, user_id=7)

        assert blocked_reason == f"Circuit breaker active: {reason}"
        assert db.params == {"uid": 7}
        assert any("trading_risk_state" in stmt for stmt in db.statements)
        assert portfolio_risk.is_breaker_tripped() is True
        assert portfolio_risk.get_breaker_status()["reason"] == reason
    finally:
        _reset_process_breaker()


def test_check_new_trade_allowed_scopes_persisted_breaker_to_user() -> None:
    from app.services.trading import portfolio_risk
    from app.services.trading.portfolio_risk import check_new_trade_allowed

    db = _FakeResetBreakerDb(None)
    budget = SimpleNamespace(
        can_open_new=True,
        crypto_positions=0,
        stock_positions=0,
    )
    _reset_process_breaker()

    try:
        with patch(
            "app.services.trading.governance.is_kill_switch_active",
            return_value=False,
        ), patch(
            "app.services.trading.portfolio_risk.check_drawdown_breaker",
            return_value=(False, None),
        ), patch(
            "app.services.trading.portfolio_risk.get_portfolio_risk_snapshot",
            return_value=budget,
        ), patch(
            "app.services.trading.portfolio_risk._broker_live_open_trades",
            return_value=[],
        ), patch(
            "app.services.trading.portfolio_risk.check_sector_concentration",
            return_value=(True, "ok"),
        ), patch(
            "app.services.trading.portfolio_risk.check_correlation_risk",
            return_value=(True, "ok"),
        ), patch(
            "app.services.trading.portfolio_optimizer.check_portfolio_drawdown",
            return_value={"breached": False, "dd_pct": 0.0, "reason": None},
        ):
            ok, reason = check_new_trade_allowed(
                db,
                42,
                "SPY",
                capital=10_000.0,
            )

        assert ok is True
        assert reason == "ok"
        assert db.params == {"uid": 42}
        assert "user_id = :uid" in db.statements[0]
        assert portfolio_risk.is_breaker_tripped() is False
    finally:
        _reset_process_breaker()


def test_unified_risk_check_blocks_latest_persisted_breaker_before_recompute() -> None:
    from app.services.trading.portfolio_risk import unified_risk_check

    reason = "test_persisted_breaker:unified_gate"
    db = _FakeBreakerDb((True, reason))
    _reset_process_breaker()

    try:
        with patch(
            "app.services.trading.governance.is_kill_switch_active",
            return_value=False,
        ), patch(
            "app.services.trading.portfolio_risk.check_drawdown_breaker",
            side_effect=AssertionError("persisted breaker must block before recompute"),
        ):
            ok, blocked_reason, detail = unified_risk_check(
                db,
                None,
                "SPY",
                capital=10_000.0,
            )

        assert ok is False
        assert blocked_reason == f"Circuit breaker: {reason}"
        assert detail["breaker_reason"] == reason
    finally:
        _reset_process_breaker()


def test_check_new_trade_allowed_honors_latest_persisted_reset_over_stale_memory() -> None:
    from app.services.trading import portfolio_risk
    from app.services.trading.portfolio_risk import check_new_trade_allowed

    db = _FakeResetBreakerDb((False, "reset"))
    budget = SimpleNamespace(
        can_open_new=True,
        crypto_positions=0,
        stock_positions=0,
    )
    portfolio_risk._breaker_tripped = True
    portfolio_risk._breaker_reason = "stale_process_breaker"

    try:
        with patch(
            "app.services.trading.governance.is_kill_switch_active",
            return_value=False,
        ), patch(
            "app.services.trading.portfolio_risk.check_drawdown_breaker",
            return_value=(False, None),
        ), patch(
            "app.services.trading.portfolio_risk.get_portfolio_risk_snapshot",
            return_value=budget,
        ), patch(
            "app.services.trading.portfolio_risk._broker_live_open_trades",
            return_value=[],
        ), patch(
            "app.services.trading.portfolio_risk.check_sector_concentration",
            return_value=(True, "ok"),
        ), patch(
            "app.services.trading.portfolio_risk.check_correlation_risk",
            return_value=(True, "ok"),
        ), patch(
            "app.services.trading.portfolio_optimizer.check_portfolio_drawdown",
            return_value={"breached": False, "dd_pct": 0.0, "reason": None},
        ):
            ok, reason = check_new_trade_allowed(
                db,
                None,
                "SPY",
                capital=10_000.0,
            )

        assert ok is True
        assert reason == "ok"
        assert portfolio_risk.is_breaker_tripped() is False
        assert portfolio_risk.get_breaker_status()["reason"] is None
    finally:
        _reset_process_breaker()


def test_check_new_trade_allowed_blocks_when_persisted_breaker_read_fails() -> None:
    from app.services.trading import portfolio_risk
    from app.services.trading.portfolio_risk import check_new_trade_allowed

    _reset_process_breaker()
    try:
        with patch(
            "app.services.trading.governance.is_kill_switch_active",
            return_value=False,
        ), patch(
            "app.services.trading.portfolio_risk.check_drawdown_breaker",
            side_effect=AssertionError("durable breaker failure must block first"),
        ):
            ok, blocked_reason = check_new_trade_allowed(
                _BrokenBreakerDb(),
                None,
                "SPY",
                capital=10_000.0,
            )

        assert ok is False
        assert blocked_reason == (
            "Circuit breaker active: durable_breaker_state_unavailable"
        )
        assert portfolio_risk.is_breaker_tripped() is True
    finally:
        _reset_process_breaker()


def test_breaker_heartbeat_records_capital_basis() -> None:
    from app.services.trading.portfolio_risk import (
        write_daily_breaker_liveness_snapshot,
    )

    db = _HeartbeatDb()

    with patch(
        "app.services.trading.portfolio_risk.check_drawdown_breaker",
        return_value=(False, None),
    ) as check:
        snapshot = write_daily_breaker_liveness_snapshot(db)

    check.assert_called_once_with(db, user_id=None, capital=100_000.0)
    assert snapshot["capital"] == 100_000.0
    assert db.params[-1]["capital"] == 100_000.0
    assert db.committed is True


def test_check_drawdown_breaker_persists_trip_capital_basis(monkeypatch) -> None:
    from app import config as app_config
    from app.services.trading import portfolio_risk
    from app.services.trading.portfolio_risk import (
        DrawdownLimits,
        check_drawdown_breaker,
    )

    persisted = []
    monkeypatch.setattr(
        app_config.settings, "chili_pattern_dd_breaker_enabled", True,
    )
    monkeypatch.setattr(
        portfolio_risk,
        "_compute_unrealized_pnl",
        lambda *_args, **_kwargs: 0.0,
    )
    monkeypatch.setattr(
        portfolio_risk,
        "_monthly_dd_threshold",
        lambda *_args, **_kwargs: (0.0, 30),
    )
    monkeypatch.setattr(
        portfolio_risk,
        "_monthly_attributed_pnl",
        lambda *_args, **_kwargs: -1.0,
    )
    monkeypatch.setattr(
        portfolio_risk,
        "_persist_breaker_state",
        lambda *args, **kwargs: persisted.append((args, kwargs)),
    )

    tripped, reason = check_drawdown_breaker(
        _FakeDrawdownDb(),
        user_id=77,
        capital=123_456.78,
        limits=DrawdownLimits(
            max_5day_dd_pct=99.0,
            max_30day_dd_pct=99.0,
            max_consecutive_losses=5,
        ),
    )

    assert tripped is True
    assert reason is not None and "monthly_dd_breaker" in reason
    assert persisted == [(
        (True, reason),
        {"user_id": 77, "capital": 123_456.78},
    )]
