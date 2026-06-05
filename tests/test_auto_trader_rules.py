"""Unit tests for AutoTrader v1 rule gate helpers."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.models.trading import AutoTraderRun, BreakoutAlert
from app.services.trading.auto_trader_rules import (
    EntryEdgeDecision,
    RuleGateContext,
    RuleGateSettings,
    _alert_confidence_probability,
    _max_execution_stop_loss_fraction,
    _non_positive_reprice_marker,
    _positive_reprice_entry_enabled_for,
    _selected_venue_entry_tca_cost_fraction,
    alert_confidence_from_score,
    autotrader_paper_realized_pnl_today_et,
    autotrader_realized_pnl_today_et,
    count_autotrader_v1_open,
    count_autotrader_v1_open_by_lane,
    evaluate_entry_edge,
    projected_profit_pct,
    passes_rule_gate,
)


@pytest.fixture(autouse=True)
def _strategy_parameters_use_test_defaults(monkeypatch):
    """Keep these unit tests pure; adaptive parameters have DB coverage elsewhere."""
    from app.services.trading import strategy_parameter

    strategy_parameter.invalidate_cache()

    def _default_parameter(*_args, default=None, **_kwargs):
        return default

    monkeypatch.setattr(strategy_parameter, "get_parameter", _default_parameter)
    monkeypatch.setattr(strategy_parameter, "register_parameter", lambda *_args, **_kwargs: -1)
    yield
    strategy_parameter.invalidate_cache()


def _option_quote_snapshot() -> dict[str, float]:
    return {"bid": 5.20, "ask": 5.40}


def test_projected_profit_pct():
    assert projected_profit_pct(100.0, 112.0) == 12.0
    assert projected_profit_pct(None, 112.0) is None
    assert projected_profit_pct(0, 112.0) is None


def test_alert_confidence_from_score():
    a = BreakoutAlert(
        ticker="X",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.4,
        price_at_alert=10.0,
    )
    assert abs(alert_confidence_from_score(a) - min(0.95, 0.55 + 0.2)) < 1e-6


def test_non_positive_reprice_marker_handles_numeric_snapshot_without_name_error():
    assert _non_positive_reprice_marker(
        {"slippage_reprice_expected_net_pct": "-0.1018"}
    )
    assert not _non_positive_reprice_marker(
        {"slippage_reprice_expected_net_pct": "Infinity"}
    )


class _FakeQuery:
    def __init__(self, result_count: int = 1):
        self.criteria = []
        self.result_count = result_count

    def filter(self, *criteria):
        self.criteria.extend(criteria)
        return self

    def count(self):
        return self.result_count


class _FakeDb:
    def __init__(self):
        self.query_obj = _FakeQuery()
        self.executed_sql = ""
        self.executed_params = None

    def query(self, _model):
        return self.query_obj

    def execute(self, sql, params=None):
        self.executed_sql = str(sql)
        self.executed_params = params
        class _Result:
            def fetchall(self):
                return [("option", 1), ("crypto", 2), ("stock", 3)]

        return _Result()


class _RowsQuery(_FakeQuery):
    def __init__(self, rows):
        super().__init__(len(rows))
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _RowsDb(_FakeDb):
    def __init__(self, rows):
        super().__init__()
        self.query_obj = _RowsQuery(rows)


def _rule_gate_settings(**overrides):
    values = {
        "chili_autotrader_rth_only": False,
        "chili_autotrader_allow_extended_hours": False,
        "chili_autotrader_crypto_enabled": False,
        "chili_autotrader_options_enabled": False,
        "chili_autotrader_confidence_floor": 0.5,
        "chili_autotrader_min_projected_profit_pct": 0.0,
        "chili_autotrader_max_symbol_price_usd": 500.0,
        "chili_autotrader_fractional_equity_enabled": True,
        "chili_autotrader_max_entry_slippage_pct": 5.0,
        "chili_autotrader_daily_loss_cap_usd": 500.0,
        "chili_autotrader_daily_loss_cap_pct": 0.0,
        "chili_autotrader_max_concurrent": 60,
        "chili_autotrader_max_concurrent_equity": 20,
        "chili_autotrader_max_concurrent_crypto": 20,
        "chili_autotrader_max_concurrent_options": 20,
        "chili_autotrader_assumed_capital_usd": 100_000.0,
        "chili_autotrader_broker_equity_cache_enabled": False,
        "chili_autotrader_broker_equity_cache_ttl_seconds": 300,
        "chili_autotrader_broker_equity_cache_max_stale_seconds": 900,
        "chili_autotrader_stock_momentum_context_gate_enabled": True,
        "chili_autotrader_stock_momentum_context_min_queue_pressure": 1.0,
        "chili_autotrader_stock_momentum_context_min_gap_pct": 5.0,
        "chili_autotrader_stock_momentum_context_min_volume_ratio": 2.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_rule_gate_settings_reads_typed_daily_loss_pct_env(monkeypatch):
    monkeypatch.setenv("CHILI_AUTOTRADER_DAILY_LOSS_CAP_PCT", "0.75")

    cfg = Settings(
        database_url="postgresql://chili:chili@localhost:5433/chili_test",
        _env_file=None,
    )
    gate_settings = RuleGateSettings.from_settings(cfg)

    assert cfg.chili_autotrader_daily_loss_cap_pct == 0.75
    assert gate_settings.daily_loss_cap_pct == 0.75


def test_positive_reprice_entry_reads_typed_env(monkeypatch):
    monkeypatch.setenv("CHILI_AUTOTRADER_POSITIVE_REPRICE_ENTRY_ENABLED", "true")
    monkeypatch.setenv("CHILI_AUTOTRADER_POSITIVE_REPRICE_ENTRY_ASSET_TYPES", "crypto")

    cfg = Settings(
        database_url="postgresql://chili:chili@localhost:5433/chili_test",
        _env_file=None,
    )

    assert _positive_reprice_entry_enabled_for(cfg, "crypto") is True
    assert _positive_reprice_entry_enabled_for(cfg, "stock") is False

    monkeypatch.setenv("CHILI_AUTOTRADER_POSITIVE_REPRICE_ENTRY_ENABLED", "false")
    disabled_cfg = Settings(
        database_url="postgresql://chili:chili@localhost:5433/chili_test",
        _env_file=None,
    )
    assert _positive_reprice_entry_enabled_for(disabled_cfg, "crypto") is False


def test_alert_confidence_probability_weight_reads_typed_env(monkeypatch):
    monkeypatch.setenv("CHILI_AUTOTRADER_ALERT_CONFIDENCE_PROBABILITY_WEIGHT", "0.5")
    cfg = Settings(
        database_url="postgresql://chili:chili@localhost:5433/chili_test",
        _env_file=None,
    )

    probability, details = _alert_confidence_probability(0.9, cfg)

    assert cfg.chili_autotrader_alert_confidence_probability_weight == 0.5
    assert probability == pytest.approx(0.7)
    assert details["weight"] == 0.5


def test_count_autotrader_v1_open_treats_working_as_active():
    db = _FakeDb()

    assert count_autotrader_v1_open(db, 123) == 1

    status_filters = [
        str(c) for c in db.query_obj.criteria if "trading_trades.status" in str(c)
    ]
    assert any(" IN " in c for c in status_filters)


def test_count_autotrader_v1_open_by_lane_counts_working_and_asset_kind():
    db = _FakeDb()

    counts = count_autotrader_v1_open_by_lane(db, 123)

    assert counts == {"equity": 3, "crypto": 2, "options": 1}
    assert "t.status IN ('open', 'working')" in db.executed_sql
    assert "t.asset_kind" in db.executed_sql
    assert db.executed_sql.find("t.asset_kind") < db.executed_sql.find("a.asset_type")


def test_autotrader_paper_realized_pnl_today_includes_partial_option_leg():
    rows = [
        SimpleNamespace(
            signal_json={"auto_trader_v1": True, "asset_type": "options"},
            entry_price=1.25,
            quantity=1.0,
            pnl=-10.0,
            direction="long",
            partial_taken=True,
            partial_taken_qty=1.0,
            partial_taken_price=1.45,
        ),
        SimpleNamespace(signal_json={"auto_trader_v1": True}, pnl=3.0),
        SimpleNamespace(signal_json={"auto_trader_v1": False}, pnl=999.0),
    ]
    db = _RowsDb(rows)

    assert autotrader_paper_realized_pnl_today_et(db, 123) == pytest.approx(13.0)


def test_autotrader_live_realized_pnl_today_includes_partial_option_leg():
    rows = [
        SimpleNamespace(
            asset_kind="option",
            tags=None,
            indicator_snapshot=None,
            entry_price=1.25,
            quantity=1.0,
            pnl=-10.0,
            direction="long",
            partial_taken=True,
            partial_taken_qty=1.0,
            partial_taken_price=1.45,
        ),
        SimpleNamespace(
            asset_kind="stock",
            tags=None,
            indicator_snapshot={},
            pnl=3.0,
        ),
    ]
    db = _RowsDb(rows)

    assert autotrader_realized_pnl_today_et(db, 123) == pytest.approx(13.0)


def test_passes_rule_gate_confidence_fail():
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="AAA",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.0,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=11.5,
        user_id=1,
    )
    ctx = RuleGateContext(current_price=10.0, autotrader_open_count=0, realized_loss_today_usd=0.0)
    settings = MagicMock()
    settings.chili_autotrader_rth_only = False
    settings.chili_autotrader_confidence_floor = 0.99
    settings.chili_autotrader_min_projected_profit_pct = 5.0
    settings.chili_autotrader_max_symbol_price_usd = 500.0
    settings.chili_autotrader_max_entry_slippage_pct = 5.0
    settings.chili_autotrader_daily_loss_cap_usd = 500.0
    settings.chili_autotrader_max_concurrent = 5

    ok, reason, _ = passes_rule_gate(db, alert, settings=settings, ctx=ctx, for_new_entry=True)
    assert not ok
    assert reason == "confidence_below_floor"


@patch("app.services.trading.pattern_imminent_alerts.us_stock_session_open", return_value=True)
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_slippage_fail(_mock_port, _mock_rth):
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="AAA",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.5,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        user_id=1,
    )
    ctx = RuleGateContext(current_price=12.0, autotrader_open_count=0, realized_loss_today_usd=0.0)
    settings = MagicMock()
    settings.chili_autotrader_rth_only = True
    # Required when chili_autotrader_rth_only is True: without this the gate
    # consults ``us_stock_extended_session_open`` (which is not patched here)
    # and short-circuits before the slippage check. MagicMock attrs default
    # to truthy MagicMock instances, so explicitly set to False.
    settings.chili_autotrader_allow_extended_hours = False
    settings.chili_autotrader_confidence_floor = 0.5
    settings.chili_autotrader_min_projected_profit_pct = 5.0
    settings.chili_autotrader_max_symbol_price_usd = 500.0
    settings.chili_autotrader_max_entry_slippage_pct = 1.0
    settings.chili_autotrader_daily_loss_cap_usd = 500.0
    settings.chili_autotrader_max_concurrent = 5
    settings.chili_autotrader_assumed_capital_usd = 100_000.0

    ok, reason, _ = passes_rule_gate(db, alert, settings=settings, ctx=ctx, for_new_entry=True)
    assert not ok
    assert reason == "missed_entry_slippage"


@patch("app.services.trading.pattern_imminent_alerts.us_stock_session_open", return_value=True)
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_expected_edge_fail(_mock_port, _mock_rth):
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="AAA",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.5,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=10.1,
        user_id=1,
    )
    ctx = RuleGateContext(current_price=10.0, autotrader_open_count=0, realized_loss_today_usd=0.0)
    settings = MagicMock()
    settings.chili_autotrader_rth_only = False
    settings.chili_autotrader_confidence_floor = 0.5
    settings.chili_autotrader_min_projected_profit_pct = 12.0
    settings.chili_autotrader_max_symbol_price_usd = 500.0
    settings.chili_autotrader_max_entry_slippage_pct = 5.0
    settings.chili_autotrader_daily_loss_cap_usd = 500.0
    settings.chili_autotrader_max_concurrent = 5
    settings.chili_autotrader_assumed_capital_usd = 100_000.0

    ok, reason, snap = passes_rule_gate(db, alert, settings=settings, ctx=ctx, for_new_entry=True)
    assert not ok
    assert reason == "non_positive_expected_edge"
    assert snap["entry_edge"]["expected_net_pct"] < 0
    assert snap["entry_edge"]["breakeven_probability"] is not None
    assert snap["entry_edge"]["probability_edge"] < 0


def test_passes_rule_gate_stock_momentum_context_blocks_weak_stock_when_batch_full():
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="WEAK",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=12.0,
        user_id=1,
        indicator_snapshot={
            "flat_indicators": {"gap_pct": 0.16, "vol_ratio": 0.54}
        },
    )
    ctx = RuleGateContext(
        current_price=10.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
        candidate_queue_pressure=1.0,
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.resolve_pattern_signal_context",
            side_effect=AssertionError("momentum context should reject first"),
        ),
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            side_effect=AssertionError("momentum context should reject first"),
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db,
            alert,
            settings=_rule_gate_settings(),
            ctx=ctx,
            for_new_entry=True,
        )

    gate = snap["stock_momentum_context_gate"]
    assert not ok
    assert reason == "stock_momentum_context_below_floor"
    assert gate["active"] is True
    assert gate["candidate_queue_pressure"] == 1.0
    assert gate["gap_passed"] is False
    assert gate["volume_ratio_passed"] is False


def test_passes_rule_gate_stock_momentum_context_waits_for_candidate_pressure():
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="QUIET",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        user_id=1,
        indicator_snapshot={"flat_indicators": {"gap_pct": 0.2, "rel_vol": 0.6}},
    )
    ctx = RuleGateContext(
        current_price=10.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
        candidate_queue_pressure=0.4,
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.resolve_pattern_signal_context",
            return_value={},
        ),
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            return_value=EntryEdgeDecision(
                True,
                "positive_expected_edge",
                {"expected_net_pct": 1.25},
            ),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_slippage_pct",
            return_value=(5.0, "test"),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_brain_risk_context",
            return_value={"dial_value": 1.0},
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_capital",
            return_value=(100_000.0, "test"),
        ),
        patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            return_value=(True, "ok"),
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db,
            alert,
            settings=_rule_gate_settings(),
            ctx=ctx,
            for_new_entry=True,
        )

    assert ok
    assert reason == "ok"
    assert snap["stock_momentum_context_gate"]["active"] is False
    assert (
        snap["stock_momentum_context_gate"]["inactive_reason"]
        == "queue_pressure_below_floor"
    )


def test_passes_rule_gate_stock_momentum_context_enforces_packet_below_candidate_pressure():
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="QUIETCTX",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        user_id=1,
        indicator_snapshot={
            "small_cap_momentum_context": {
                "gap_pct": 0.2,
                "rvol": 0.6,
                "news_count": 1,
                "news_sentiment": 0.2,
            },
        },
    )
    ctx = RuleGateContext(
        current_price=10.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
        candidate_queue_pressure=0.0,
    )

    with patch(
        "app.services.trading.auto_trader_rules.evaluate_entry_edge",
        side_effect=AssertionError("weak packet should reject before entry edge"),
    ):
        ok, reason, snap = passes_rule_gate(
            db,
            alert,
            settings=_rule_gate_settings(),
            ctx=ctx,
            for_new_entry=True,
        )

    gate = snap["stock_momentum_context_gate"]
    assert not ok
    assert reason == "stock_momentum_context_below_floor"
    assert gate["active"] is True
    assert gate["active_reason"] == "small_cap_momentum_context"
    assert gate["small_cap_momentum_context_present"] is True
    assert gate["gap_passed"] is False
    assert gate["volume_ratio_passed"] is False


def test_passes_rule_gate_stock_momentum_context_does_not_bypass_expected_edge():
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="GAPPER",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=12.0,
        user_id=1,
        indicator_snapshot={"flat_indicators": {"gap_pct": 7.0, "rel_vol": 3.2}},
    )
    ctx = RuleGateContext(
        current_price=10.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
        candidate_queue_pressure=1.0,
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.resolve_pattern_signal_context",
            return_value={},
        ),
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            return_value=EntryEdgeDecision(
                False,
                "non_positive_expected_edge",
                {"expected_net_pct": -0.25},
            ),
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db,
            alert,
            settings=_rule_gate_settings(),
            ctx=ctx,
            for_new_entry=True,
        )

    gate = snap["stock_momentum_context_gate"]
    assert not ok
    assert reason == "non_positive_expected_edge"
    assert gate["active"] is True
    assert gate["gap_passed"] is True
    assert gate["volume_ratio_passed"] is True


def test_passes_rule_gate_stock_momentum_context_reads_nested_packet():
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="NESTED",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=12.0,
        user_id=1,
        indicator_snapshot={
            "flat_indicators": {"price": 10.0},
            "small_cap_momentum_context": {
                "gap_pct": 7.0,
                "rvol": 3.2,
                "news_count": 3,
                "news_sentiment": 0.28,
                "float_bucket": "micro_float_proxy",
                "spread_bps": 35.0,
            },
        },
    )
    ctx = RuleGateContext(
        current_price=10.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
        candidate_queue_pressure=1.0,
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.resolve_pattern_signal_context",
            return_value={},
        ),
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            return_value=EntryEdgeDecision(
                False,
                "non_positive_expected_edge",
                {"expected_net_pct": -0.25},
            ),
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db,
            alert,
            settings=_rule_gate_settings(),
            ctx=ctx,
            for_new_entry=True,
        )

    gate = snap["stock_momentum_context_gate"]
    assert not ok
    assert reason == "non_positive_expected_edge"
    assert gate["small_cap_momentum_context_present"] is True
    assert gate["gap_passed"] is True
    assert gate["volume_ratio_passed"] is True
    assert gate["spread_passed"] is True
    assert gate["catalyst_passed"] is True
    assert gate["gap_source"] == "gap_pct"
    assert gate["volume_ratio_source"] == "rvol"
    assert gate["float_bucket"] == "micro_float_proxy"


def test_passes_rule_gate_stock_momentum_context_blocks_halted_packet_before_edge():
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="HALT",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=12.0,
        user_id=1,
        indicator_snapshot={
            "small_cap_momentum_context": {
                "gap_pct": 7.0,
                "relative_volume": 3.2,
                "halted": True,
                "news_count": 4,
                "news_sentiment": 0.4,
            },
        },
    )
    ctx = RuleGateContext(
        current_price=10.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
        candidate_queue_pressure=1.0,
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.resolve_pattern_signal_context",
            side_effect=AssertionError("halt should reject before pattern context"),
        ),
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            side_effect=AssertionError("halt should reject before entry edge"),
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db,
            alert,
            settings=_rule_gate_settings(),
            ctx=ctx,
            for_new_entry=True,
        )

    assert not ok
    assert reason == "stock_momentum_context_halted"
    assert snap["stock_momentum_context_gate"]["halt_passed"] is False


def test_passes_rule_gate_stock_momentum_context_blocks_wide_spread_under_pressure():
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="WIDE",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=12.0,
        user_id=1,
        indicator_snapshot={
            "small_cap_momentum_context": {
                "gap_pct": 7.0,
                "rvol": 3.2,
                "news_count": 2,
                "news_sentiment": 0.2,
                "spread_bps": 200.0,
            },
        },
    )
    ctx = RuleGateContext(
        current_price=10.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
        candidate_queue_pressure=1.0,
    )

    with patch(
        "app.services.trading.auto_trader_rules.evaluate_entry_edge",
        side_effect=AssertionError("wide spread should reject before entry edge"),
    ):
        ok, reason, snap = passes_rule_gate(
            db,
            alert,
            settings=_rule_gate_settings(),
            ctx=ctx,
            for_new_entry=True,
        )

    gate = snap["stock_momentum_context_gate"]
    assert not ok
    assert reason == "stock_momentum_context_wide_spread"
    assert gate["spread_passed"] is False
    assert gate["spread_bps"] == 200.0
    assert gate["max_spread_bps"] == 125.0


def test_passes_rule_gate_stock_momentum_context_blocks_explicit_no_catalyst_packet():
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="NOCAT",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=12.0,
        user_id=1,
        indicator_snapshot={
            "small_cap_momentum_context": {
                "gap_pct": 7.0,
                "rvol": 3.2,
                "news_count": 0,
                "news_sentiment": 0.0,
                "spread_bps": 25.0,
                "prescreen_source_tags": ["massive_momentum_gappers"],
                "prescreen_momentum_gapper": True,
            },
        },
    )
    ctx = RuleGateContext(
        current_price=10.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
        candidate_queue_pressure=1.0,
    )

    with patch(
        "app.services.trading.auto_trader_rules.evaluate_entry_edge",
        side_effect=AssertionError("no catalyst should reject before entry edge"),
    ):
        ok, reason, snap = passes_rule_gate(
            db,
            alert,
            settings=_rule_gate_settings(),
            ctx=ctx,
            for_new_entry=True,
        )

    assert not ok
    assert reason == "stock_momentum_context_no_catalyst"
    assert snap["stock_momentum_context_gate"]["catalyst_passed"] is False


def test_passes_rule_gate_skips_legacy_stock_price_cap_when_fractional_equity_enabled():
    high_price_stock = 250.0
    legacy_whole_share_cap = 200.0
    db = MagicMock()
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_allow_extended_hours=False,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=0.0,
        chili_autotrader_max_symbol_price_usd=legacy_whole_share_cap,
        chili_autotrader_fractional_equity_enabled=True,
        chili_autotrader_max_entry_slippage_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=0.0,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=20,
        chili_autotrader_max_concurrent_crypto=20,
        chili_autotrader_max_concurrent_options=20,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
        chili_autotrader_broker_equity_cache_ttl_seconds=300,
        chili_autotrader_broker_equity_cache_max_stale_seconds=900,
    )
    alert = BreakoutAlert(
        ticker="AAPL",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=high_price_stock,
        entry_price=high_price_stock,
        stop_loss=240.0,
        target_price=280.0,
        user_id=1,
    )
    ctx = RuleGateContext(
        current_price=high_price_stock,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            return_value=EntryEdgeDecision(
                True,
                "positive_expected_edge",
                {"expected_net_pct": 1.25},
            ),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_slippage_pct",
            return_value=(5.0, "test"),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_brain_risk_context",
            return_value={"dial_value": 1.0},
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_capital",
            return_value=(100_000.0, "test"),
        ),
        patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            return_value=(True, "ok"),
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db, alert, settings=settings, ctx=ctx, for_new_entry=True
        )

    assert ok
    assert reason == "ok"
    assert snap["fractional_equity_enabled"] is True
    assert snap["max_symbol_price_usd"] == legacy_whole_share_cap
    assert snap["symbol_price_cap_skipped_reason"] == "fractional_equity_enabled"


def test_passes_rule_gate_shadow_observation_skips_live_risk_authority():
    db = MagicMock()
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_allow_extended_hours=False,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=0.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_fractional_equity_enabled=True,
        chili_autotrader_max_entry_slippage_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=1.5,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=20,
        chili_autotrader_max_concurrent_crypto=20,
        chili_autotrader_max_concurrent_options=20,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
        chili_autotrader_broker_equity_cache_ttl_seconds=300,
        chili_autotrader_broker_equity_cache_max_stale_seconds=900,
    )
    alert = BreakoutAlert(
        ticker="SHADOW",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=115.0,
        user_id=1,
    )
    setattr(alert, "_chili_shadow_observation_only", True)
    ctx = RuleGateContext(
        current_price=100.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.resolve_pattern_signal_context",
            return_value={},
        ),
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            return_value=EntryEdgeDecision(
                True,
                "positive_expected_edge",
                {"expected_net_pct": 1.25},
            ),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_slippage_pct",
            return_value=(5.0, "test"),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_brain_risk_context",
            side_effect=AssertionError(
                "shadow observations should skip brain risk authority"
            ),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_capital",
            side_effect=AssertionError(
                "shadow observations should skip broker-backed capital"
            ),
        ),
        patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            side_effect=AssertionError(
                "shadow observations should skip live portfolio authority"
            ),
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db, alert, settings=settings, ctx=ctx, for_new_entry=True
        )

    assert ok
    assert reason == "ok"
    assert snap["entry_edge_expected_net_pct"] == 1.25
    assert snap["shadow_observation_risk_authority_skipped"] is True
    assert snap["portfolio_check"]["reason"] == "shadow_observation_only"


def test_rule_gate_passes_advisory_venue_to_entry_edge():
    db = MagicMock()
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_allow_extended_hours=False,
        chili_autotrader_crypto_enabled=True,
        chili_autotrader_options_enabled=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=0.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_fractional_equity_enabled=True,
        chili_autotrader_max_entry_slippage_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=1.5,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=20,
        chili_autotrader_max_concurrent_crypto=20,
        chili_autotrader_max_concurrent_options=20,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
        chili_autotrader_broker_equity_cache_ttl_seconds=300,
        chili_autotrader_broker_equity_cache_max_stale_seconds=900,
    )
    alert = BreakoutAlert(
        ticker="EDGE-USD",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=115.0,
        user_id=1,
    )
    ctx = RuleGateContext(
        current_price=100.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.resolve_pattern_signal_context",
            return_value={},
        ),
        patch(
            "app.services.trading.broker_selector.select_venue",
            return_value=SimpleNamespace(venue="coinbase", reason="test"),
        ),
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            return_value=EntryEdgeDecision(
                True,
                "positive_expected_edge",
                {"expected_net_pct": 1.25},
            ),
        ) as edge_mock,
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_slippage_pct",
            return_value=(5.0, "test"),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_brain_risk_context",
            return_value={"dial_value": 1.0},
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_capital",
            return_value=(100_000.0, "broker"),
        ),
        patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            return_value=(True, "ok"),
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db, alert, settings=settings, ctx=ctx, for_new_entry=True
        )

    assert ok
    assert reason == "ok"
    assert edge_mock.call_args.kwargs["selected_venue"] == "coinbase"
    assert snap["entry_edge_advisory_venue"] == "coinbase"
    assert snap["entry_edge_advisory_reason"] == "test"
    assert snap["entry_edge_selected_venue"] == "coinbase"


def test_daily_loss_cap_uses_static_dollar_cap_when_equity_is_unproven():
    db = MagicMock()
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_allow_extended_hours=False,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=0.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_fractional_equity_enabled=True,
        chili_autotrader_max_entry_slippage_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=1.5,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=20,
        chili_autotrader_max_concurrent_crypto=20,
        chili_autotrader_max_concurrent_options=20,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
        chili_autotrader_broker_equity_cache_ttl_seconds=300,
        chili_autotrader_broker_equity_cache_max_stale_seconds=900,
    )
    alert = BreakoutAlert(
        ticker="LOSS",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=115.0,
        user_id=1,
    )
    ctx = RuleGateContext(
        current_price=100.0,
        autotrader_open_count=0,
        realized_loss_today_usd=-600.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.resolve_pattern_signal_context",
            return_value={},
        ),
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            return_value=EntryEdgeDecision(
                True,
                "positive_expected_edge",
                {"expected_net_pct": 1.25},
            ),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_slippage_pct",
            return_value=(5.0, "test"),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_brain_risk_context",
            return_value={"dial_value": 1.0},
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_capital",
            return_value=(100_000.0, "fallback:broker_disconnected"),
        ),
        patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            side_effect=AssertionError("portfolio gate must not run after loss cap"),
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db, alert, settings=settings, ctx=ctx, for_new_entry=True
        )

    assert not ok
    assert reason == "daily_loss_cap_already_hit"
    assert snap["daily_loss_cap_source"] == "env_dollar_dial"
    assert snap["daily_loss_cap_capital_source"] == "fallback:broker_disconnected"
    assert snap["daily_loss_cap_unproven_equity_usd"] == 100_000.0
    assert snap["daily_loss_cap_usd"] == 500.0


def test_passes_rule_gate_blocks_new_entry_on_unproven_fallback_capital():
    db = MagicMock()
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_allow_extended_hours=False,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=0.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_fractional_equity_enabled=True,
        chili_autotrader_max_entry_slippage_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=1.5,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=20,
        chili_autotrader_max_concurrent_crypto=20,
        chili_autotrader_max_concurrent_options=20,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
        chili_autotrader_broker_equity_cache_ttl_seconds=300,
        chili_autotrader_broker_equity_cache_max_stale_seconds=900,
    )
    alert = BreakoutAlert(
        ticker="CAP",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=115.0,
        user_id=1,
    )
    ctx = RuleGateContext(
        current_price=100.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.resolve_pattern_signal_context",
            return_value={},
        ),
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            return_value=EntryEdgeDecision(
                True,
                "positive_expected_edge",
                {"expected_net_pct": 1.25},
            ),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_slippage_pct",
            return_value=(5.0, "test"),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_brain_risk_context",
            return_value={"dial_value": 1.0},
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_capital",
            return_value=(100_000.0, "fallback:broker_disconnected"),
        ),
        patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            side_effect=AssertionError("fallback capital must not reach portfolio gate"),
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db, alert, settings=settings, ctx=ctx, for_new_entry=True
        )

    assert ok is False
    assert reason == "capital_unavailable:fallback:broker_disconnected"
    assert snap["capital_source"] == "fallback:broker_disconnected"
    assert snap["capital_proven"] is False
    assert snap["portfolio_check"] == {"ok": False, "reason": reason}


def test_passes_rule_gate_keeps_stock_price_cap_when_fractional_equity_disabled():
    high_price_stock = 250.0
    legacy_whole_share_cap = 200.0
    db = MagicMock()
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_allow_extended_hours=False,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=0.0,
        chili_autotrader_max_symbol_price_usd=legacy_whole_share_cap,
        chili_autotrader_fractional_equity_enabled=False,
        chili_autotrader_max_entry_slippage_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=0.0,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=20,
        chili_autotrader_max_concurrent_crypto=20,
        chili_autotrader_max_concurrent_options=20,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
        chili_autotrader_broker_equity_cache_ttl_seconds=300,
        chili_autotrader_broker_equity_cache_max_stale_seconds=900,
    )
    alert = BreakoutAlert(
        ticker="AAPL",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=high_price_stock,
        entry_price=high_price_stock,
        stop_loss=240.0,
        target_price=280.0,
        user_id=1,
    )
    ctx = RuleGateContext(
        current_price=high_price_stock,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )

    with patch(
        "app.services.trading.auto_trader_rules.evaluate_entry_edge",
        return_value=EntryEdgeDecision(
            True,
            "positive_expected_edge",
            {"expected_net_pct": 1.25},
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db, alert, settings=settings, ctx=ctx, for_new_entry=True
        )

    assert not ok
    assert reason == "symbol_price_above_cap"
    assert snap["fractional_equity_enabled"] is False
    assert snap["max_symbol_price_usd"] == legacy_whole_share_cap
    assert "symbol_price_cap_skipped_reason" not in snap


def test_passes_rule_gate_accepts_bounded_favorable_stock_drift_after_edge_recheck():
    db = MagicMock()
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_allow_extended_hours=False,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=0.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_fractional_equity_enabled=True,
        chili_autotrader_max_entry_slippage_pct=2.0,
        chili_autotrader_favorable_entry_drift_enabled=True,
        chili_autotrader_favorable_entry_drift_asset_types="stock",
        chili_autotrader_favorable_entry_drift_slippage_multiple=2.5,
        chili_autotrader_favorable_entry_drift_max_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=0.0,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=20,
        chili_autotrader_max_concurrent_crypto=20,
        chili_autotrader_max_concurrent_options=20,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
        chili_autotrader_broker_equity_cache_ttl_seconds=300,
        chili_autotrader_broker_equity_cache_max_stale_seconds=900,
    )
    alert = BreakoutAlert(
        ticker="PULLBACK",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=115.0,
        user_id=1,
    )
    ctx = RuleGateContext(
        current_price=96.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )
    initial_edge = EntryEdgeDecision(
        True,
        "positive_expected_edge",
        {"expected_net_pct": 0.75},
    )
    rechecked_edge = EntryEdgeDecision(
        True,
        "positive_expected_edge",
        {"expected_net_pct": 1.4, "entry_price": 96.0},
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            side_effect=[initial_edge, rechecked_edge],
        ) as edge_mock,
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_slippage_pct",
            return_value=(2.0, "test"),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_brain_risk_context",
            return_value={"dial_value": 1.0},
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_capital",
            return_value=(100_000.0, "test"),
        ),
        patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            return_value=(True, "ok"),
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db, alert, settings=settings, ctx=ctx, for_new_entry=True
        )

    assert ok
    assert reason == "ok"
    assert edge_mock.call_count == 2
    assert snap["entry_slippage_direction"] == "favorable"
    assert snap["entry_reference_price_adjusted"] is True
    assert snap["entry_edge_expected_net_pct"] == 1.4
    assert snap["favorable_entry_drift_edge_reason"] == "positive_expected_edge"


def test_passes_rule_gate_accepts_bounded_positive_reprice_after_edge_recheck():
    db = MagicMock()
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_allow_extended_hours=False,
        chili_autotrader_crypto_enabled=True,
        chili_autotrader_options_enabled=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=0.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_fractional_equity_enabled=True,
        chili_autotrader_max_entry_slippage_pct=1.0,
        chili_autotrader_favorable_entry_drift_enabled=True,
        chili_autotrader_favorable_entry_drift_asset_types="stock",
        chili_autotrader_favorable_entry_drift_slippage_multiple=2.5,
        chili_autotrader_favorable_entry_drift_max_pct=5.0,
        chili_autotrader_positive_reprice_entry_enabled=True,
        chili_autotrader_positive_reprice_entry_asset_types="stock,crypto",
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=0.0,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=20,
        chili_autotrader_max_concurrent_crypto=20,
        chili_autotrader_max_concurrent_options=20,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
        chili_autotrader_broker_equity_cache_ttl_seconds=300,
        chili_autotrader_broker_equity_cache_max_stale_seconds=900,
    )
    alert = BreakoutAlert(
        ticker="EDGE-USD",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        score_at_alert=0.72,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=120.0,
        user_id=1,
    )
    ctx = RuleGateContext(
        current_price=102.4,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )
    initial_edge = EntryEdgeDecision(
        True,
        "positive_expected_edge",
        {"expected_net_pct": 0.65},
    )
    rechecked_edge = EntryEdgeDecision(
        True,
        "positive_expected_edge",
        {"expected_net_pct": 0.31, "entry_price": 102.4},
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            side_effect=[initial_edge, rechecked_edge],
        ) as edge_mock,
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_slippage_pct",
            return_value=(1.0, "test"),
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_brain_risk_context",
            return_value={"dial_value": 1.0},
        ),
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_capital",
            return_value=(100_000.0, "test"),
        ),
        patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            return_value=(True, "ok"),
        ),
    ):
        ok, reason, snap = passes_rule_gate(
            db, alert, settings=settings, ctx=ctx, for_new_entry=True
        )

    assert ok
    assert reason == "ok"
    assert edge_mock.call_count == 2
    assert snap["entry_slippage_direction"] == "adverse"
    assert snap["slippage_reprice_positive_edge"] is True
    assert snap["slippage_reprice_positive_edge_enabled"] is True
    assert snap["slippage_reprice_accepted"] is True
    assert snap["entry_reference_price_adjusted"] is True
    assert snap["entry_edge_expected_net_pct"] == 0.31


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({"slippage_reprice_expected_net_pct": -0.01}, True),
        ({"slippage_reprice_expected_net_pct": "0.25"}, False),
        ({"slippage_reprice_expected_net_pct": "not-a-number"}, False),
        ({}, False),
        (
            {
                "slippage_reprice_expected_net_pct": "not-a-number",
                "slippage_reprice_positive_edge": False,
            },
            True,
        ),
        ({"slippage_reprice_edge_reason": "non_positive_expected_edge"}, True),
    ],
)
def test_non_positive_reprice_marker_handles_expected_net_pct_values(
    snapshot, expected
):
    assert _non_positive_reprice_marker(snapshot) is expected


def test_passes_rule_gate_cools_down_repeated_non_positive_reprice():
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.all.return_value = [
        AutoTraderRun(
            ticker="EDGE-USD",
            scan_pattern_id=77,
            reason="missed_entry_slippage",
            created_at=datetime.utcnow() - timedelta(minutes=2),
            rule_snapshot={
                "slippage_reprice_positive_edge": False,
                "slippage_reprice_expected_net_pct": -0.21,
                "slippage_reprice_edge_reason": "non_positive_expected_edge",
            },
        ),
        AutoTraderRun(
            ticker="EDGE-USD",
            scan_pattern_id=77,
            reason="missed_entry_slippage",
            created_at=datetime.utcnow() - timedelta(minutes=4),
            rule_snapshot={
                "slippage_reprice_positive_edge": False,
                "slippage_reprice_expected_net_pct": -0.08,
            },
        ),
    ]
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_allow_extended_hours=False,
        chili_autotrader_crypto_enabled=True,
        chili_autotrader_options_enabled=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=0.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_fractional_equity_enabled=True,
        chili_autotrader_max_entry_slippage_pct=1.0,
        chili_autotrader_favorable_entry_drift_enabled=True,
        chili_autotrader_favorable_entry_drift_asset_types="stock",
        chili_autotrader_favorable_entry_drift_slippage_multiple=2.5,
        chili_autotrader_favorable_entry_drift_max_pct=5.0,
        chili_autotrader_positive_reprice_entry_enabled=True,
        chili_autotrader_positive_reprice_entry_asset_types="stock,crypto",
        chili_autotrader_slippage_reprice_cooldown_enabled=True,
        chili_autotrader_slippage_reprice_cooldown_minutes=20,
        chili_autotrader_slippage_reprice_cooldown_threshold=2,
        chili_autotrader_slippage_reprice_cooldown_asset_types="crypto",
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=0.0,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=20,
        chili_autotrader_max_concurrent_crypto=20,
        chili_autotrader_max_concurrent_options=20,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
        chili_autotrader_broker_equity_cache_ttl_seconds=300,
        chili_autotrader_broker_equity_cache_max_stale_seconds=900,
    )
    alert = BreakoutAlert(
        ticker="EDGE-USD",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        scan_pattern_id=77,
        score_at_alert=0.72,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=120.0,
        user_id=1,
    )
    ctx = RuleGateContext(
        current_price=102.4,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )
    initial_edge = EntryEdgeDecision(
        True,
        "positive_expected_edge",
        {"expected_net_pct": 0.65},
    )

    with (
        patch(
            "app.services.trading.auto_trader_rules.evaluate_entry_edge",
            return_value=initial_edge,
        ) as edge_mock,
        patch(
            "app.services.trading.auto_trader_rules.resolve_effective_slippage_pct",
            return_value=(1.0, "test"),
        ),
        patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            return_value=(True, "ok"),
        ) as portfolio_mock,
    ):
        ok, reason, snap = passes_rule_gate(
            db, alert, settings=settings, ctx=ctx, for_new_entry=True
        )

    assert not ok
    assert reason == "slippage_reprice_cooldown"
    assert edge_mock.call_count == 1
    assert snap["slippage_reprice_positive_edge_enabled"] is True
    assert snap["slippage_reprice_cooldown_active"] is True
    assert snap["slippage_reprice_cooldown_count"] == 2
    assert snap["slippage_reprice_cooldown_threshold"] == 2
    assert snap["slippage_reprice_cooldown_reason"] == "repeated_non_positive_reprice_edge"
    assert "slippage_reprice_expected_net_pct" not in snap
    portfolio_mock.assert_not_called()


def test_evaluate_entry_edge_uses_dynamic_exit_payoff_distribution():
    class _EmptyExec:
        def mappings(self):
            return self

        def first(self):
            return None

    class _Query:
        def __init__(self, pattern):
            self._pattern = pattern

        def filter(self, *_args, **_kwargs):
            return self

        def one_or_none(self):
            return self._pattern

    class _Db:
        def __init__(self, pattern):
            self._pattern = pattern

        def query(self, *_args, **_kwargs):
            return _Query(self._pattern)

        def execute(self, *_args, **_kwargs):
            return _EmptyExec()

    pattern = SimpleNamespace(
        corrected_trade_count=87,
        corrected_win_rate=0.3908,
        corrected_avg_return_pct=1.56,
        trade_count=87,
        win_rate=0.3908,
        avg_return_pct=1.56,
        avg_winner_pct=0.06830689055415255,
        avg_loser_pct=-0.015172948222159992,
        payoff_ratio=4.501886486002159,
        payoff_ratio_n=87,
    )
    alert = BreakoutAlert(
        ticker="TRUMP-USD",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        scan_pattern_id=585,
        score_at_alert=0.5,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=80.0,
        target_price=120.0,
        user_id=1,
    )
    settings = SimpleNamespace(chili_realized_ev_min_trades=5)

    decision = evaluate_entry_edge(
        _Db(pattern),
        alert,
        settings=settings,
        pat_ctx={},
        confidence=0.5,
    )

    assert decision.allowed
    assert decision.reason == "positive_expected_edge"
    assert decision.snapshot["edge_geometry_source"] == "realized_dynamic_exit_blend"
    assert decision.snapshot["dynamic_exit_geometry"]["used"] is True
    assert decision.snapshot["breakeven_probability"] < 0.4
    assert decision.snapshot["expected_net_pct"] > 0


def test_evaluate_entry_edge_blocks_absurd_stock_execution_stop_geometry():
    class _EmptyExec:
        def mappings(self):
            return self

        def all(self):
            return []

        def first(self):
            return None

    class _Query:
        def filter(self, *_args, **_kwargs):
            return self

        def one_or_none(self):
            return None

    class _Db:
        def query(self, *_args, **_kwargs):
            return _Query()

        def execute(self, *_args, **_kwargs):
            return _EmptyExec()

    alert = BreakoutAlert(
        ticker="AAOX",
        asset_type="stock",
        alert_tier="pattern_imminent",
        scan_pattern_id=1256,
        score_at_alert=0.7,
        price_at_alert=42.87,
        entry_price=42.87,
        stop_loss=7.58,
        target_price=85.22,
        user_id=1,
    )

    decision = evaluate_entry_edge(
        _Db(),
        alert,
        settings=SimpleNamespace(chili_autotrader_stock_max_execution_stop_loss_pct=30.0),
        pat_ctx={},
        confidence=0.95,
    )

    assert not decision.allowed
    assert decision.reason == "execution_stop_loss_too_wide"
    assert decision.snapshot["execution_stop_loss_fraction"] > 0.8
    assert decision.snapshot["max_execution_stop_loss_pct"] == 30.0


def test_execution_stop_loss_caps_read_typed_env(monkeypatch):
    monkeypatch.setenv("CHILI_AUTOTRADER_STOCK_MAX_EXECUTION_STOP_LOSS_PCT", "22.5")
    monkeypatch.setenv("CHILI_AUTOTRADER_CRYPTO_MAX_EXECUTION_STOP_LOSS_PCT", "44")
    monkeypatch.setenv("CHILI_AUTOTRADER_OPTIONS_MAX_EXECUTION_STOP_LOSS_PCT", "0")

    cfg = Settings(
        database_url="postgresql://chili:chili@localhost:5433/chili_test",
        _env_file=None,
    )

    assert _max_execution_stop_loss_fraction(cfg, "stock") == pytest.approx(0.225)
    assert _max_execution_stop_loss_fraction(cfg, "crypto") == pytest.approx(0.44)
    assert _max_execution_stop_loss_fraction(cfg, "options") is None


def test_evaluate_entry_edge_blocks_thin_margin_after_empirical_cost():
    class _Result:
        def __init__(self, rows=None, first=None):
            self._rows = rows or []
            self._first = first

        def mappings(self):
            return self

        def all(self):
            return list(self._rows)

        def first(self):
            return self._first

    class _Db:
        def __init__(self):
            self.sqls = []
            self.params = []

        def execute(self, sql, params=None):
            text = str(sql)
            self.sqls.append(text)
            self.params.append(params or {})
            if "trading_execution_cost_estimates" in text:
                return _Result(
                    first={
                        "side": "long",
                        "sample_trades": 12,
                        "p90_spread_bps": 25.0,
                        "p90_slippage_bps": 100.0,
                        "median_spread_bps": 10.0,
                        "median_slippage_bps": 40.0,
                        "last_updated_at": datetime(2026, 6, 3, 12, 0),
                    }
                )
            return _Result(rows=[])

    alert = BreakoutAlert(
        ticker="EDGE",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.9,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=99.0,
        target_price=102.0,
        user_id=1,
    )
    settings = SimpleNamespace(
        chili_autotrader_alert_confidence_probability_weight=1.0,
        chili_autotrader_min_expected_net_after_empirical_cost_pct=0.25,
        chili_coinbase_cost_gate_min_tca_samples=5,
    )
    db = _Db()

    decision = evaluate_entry_edge(
        db,
        alert,
        settings=settings,
        pat_ctx={},
        confidence=0.8,
    )

    assert not decision.allowed
    assert decision.reason == "empirical_cost_edge_margin_too_thin"
    assert decision.snapshot["empirical_cost_used"] is True
    assert decision.snapshot["empirical_cost"]["matched_side"] == "long"
    assert decision.snapshot["empirical_cost"]["side_aliases"] == ["long", "buy"]
    assert decision.snapshot["empirical_cost"]["total_cost_bps"] == 125.0
    assert decision.snapshot["expected_net_pct"] == pytest.approx(0.15)
    assert decision.snapshot["min_expected_net_after_empirical_cost_pct"] == 0.25
    assert decision.snapshot["empirical_cost_edge_margin_pct"] == pytest.approx(-0.1)
    assert "LOWER(side) IN ('long', 'buy')" in db.sqls[0]
    assert "COALESCE(sample_trades, 0) >= :min_samples" in db.sqls[0]
    assert db.params[0]["min_samples"] == 5


def test_evaluate_entry_edge_prefers_selected_venue_broker_source_tca():
    class _Result:
        def __init__(self, rows=None, first=None):
            self._rows = rows or []
            self._first = first

        def mappings(self):
            return self

        def all(self):
            return list(self._rows)

        def first(self):
            return self._first

    class _Db:
        def __init__(self):
            self.sqls = []
            self.params = []

        def execute(self, sql, params=None):
            text = str(sql)
            self.sqls.append(text)
            self.params.append(params or {})
            if "LOWER(COALESCE(broker_source, '')) IN ('robinhood', 'rh')" in text:
                return _Result(
                    first={
                        "sample_trades": 12,
                        "avg_entry_slippage_bps": 80.0,
                        "p90_entry_slippage_bps": 125.0,
                    }
                )
            if "trading_execution_cost_estimates" in text:
                raise AssertionError("aggregate estimate should not be queried")
            return _Result(rows=[])

    alert = BreakoutAlert(
        ticker="EDGE",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.9,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=99.0,
        target_price=102.0,
        user_id=1,
    )
    settings = SimpleNamespace(
        chili_autotrader_alert_confidence_probability_weight=1.0,
        chili_autotrader_min_expected_net_after_empirical_cost_pct=0.25,
        chili_robinhood_cost_gate_min_tca_samples=5,
        chili_robinhood_cost_gate_window_days=30,
        brain_execution_cost_unverified_tca_outlier_bps=500.0,
    )
    db = _Db()

    decision = evaluate_entry_edge(
        db,
        alert,
        settings=settings,
        pat_ctx={},
        confidence=0.8,
        selected_venue="robinhood",
    )

    assert not decision.allowed
    assert decision.reason == "empirical_cost_edge_margin_too_thin"
    assert decision.snapshot["empirical_cost_used"] is True
    assert decision.snapshot["empirical_cost"]["selected_venue"] == "robinhood"
    assert decision.snapshot["empirical_cost"]["source"] == "trading_trades_broker_source"
    assert decision.snapshot["empirical_cost"]["sample_basis"] == (
        "usable_robinhood_adverse_entry_tca_trades"
    )
    assert decision.snapshot["empirical_cost"]["tca_cost_bps"] == 125
    assert decision.snapshot["expected_net_pct"] == pytest.approx(0.15)
    assert "broker_source" in db.sqls[0]


def test_coinbase_cost_gate_window_reads_typed_env(monkeypatch):
    class _Result:
        def __init__(self, row):
            self._row = row

        def mappings(self):
            return self

        def first(self):
            return self._row

    class _Db:
        def __init__(self):
            self.params = []

        def execute(self, _sql, params=None):
            self.params.append(params or {})
            return _Result(
                {
                    "sample_trades": 8,
                    "avg_entry_slippage_bps": 12.0,
                    "p90_entry_slippage_bps": 34.0,
                }
            )

    monkeypatch.setenv("CHILI_COINBASE_COST_GATE_MIN_TCA_SAMPLES", "7")
    monkeypatch.setenv("CHILI_COINBASE_COST_GATE_WINDOW_DAYS", "17")
    cfg = Settings(
        database_url="postgresql://chili:chili@localhost:5433/chili_test",
        _env_file=None,
    )
    db = _Db()

    cost_fraction, snap = _selected_venue_entry_tca_cost_fraction(
        db,
        ticker="BTC-USD",
        settings=cfg,
        selected_venue="coinbase",
    )

    assert cost_fraction == pytest.approx(0.0034)
    assert db.params[0]["window_days"] == 17
    assert snap["selected_venue"] == "coinbase"
    assert snap["window_days"] == 17
    assert snap["min_samples"] == 7


def test_evaluate_entry_edge_guards_probability_sample_count_to_closed_trades():
    class _EmptyExec:
        def mappings(self):
            return self

        def first(self):
            return None

    class _Query:
        def __init__(self, pattern):
            self._pattern = pattern

        def filter(self, *_args, **_kwargs):
            return self

        def one_or_none(self):
            return self._pattern

    class _Db:
        def __init__(self, pattern):
            self._pattern = pattern

        def query(self, *_args, **_kwargs):
            return _Query(self._pattern)

        def execute(self, *_args, **_kwargs):
            return _EmptyExec()

    pattern = SimpleNamespace(
        corrected_trade_count=6,
        corrected_win_rate=0.0,
        corrected_avg_return_pct=-1.28,
        raw_realized_trade_count=1,
        raw_realized_win_rate=0.0,
        raw_realized_avg_return_pct=-1.275,
        avg_winner_pct=None,
        avg_loser_pct=-0.01275,
    )
    alert = BreakoutAlert(
        ticker="00-USD",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        scan_pattern_id=1248,
        score_at_alert=0.55,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=85.0,
        target_price=118.0,
        user_id=1,
    )
    settings = SimpleNamespace(chili_realized_ev_min_trades=5)

    decision = evaluate_entry_edge(
        _Db(pattern),
        alert,
        settings=settings,
        pat_ctx={},
        confidence=0.82,
    )

    assert decision.snapshot["probability_sample_n"] == 1
    assert decision.snapshot["probability"] == pytest.approx(2.5 / 6, rel=1e-6)
    assert "closed_sample_count_guard" in decision.snapshot["probability_source"]
    assert decision.snapshot["expected_net_pct"] > -2.0


# VV — per-lane concurrency cap tests. The rule gate should bucket the
# alert into one of {equity, crypto, options} and consult the matching
# per-lane cap from RuleGateSettings (or strategy_parameter when
# available). Each lane's cap is independent: the equity lane filling
# up must NOT block crypto or options entries.

@patch("app.services.trading.pattern_imminent_alerts.us_stock_session_open", return_value=True)
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_per_lane_equity_blocks_only_equity(_mock_port, _mock_rth):
    """Equity lane at cap → equity alert blocked, crypto alert still passes."""
    db = MagicMock()
    settings = MagicMock()
    settings.chili_autotrader_rth_only = False
    settings.chili_autotrader_confidence_floor = 0.5
    settings.chili_autotrader_min_projected_profit_pct = 5.0
    settings.chili_autotrader_max_symbol_price_usd = 500.0
    settings.chili_autotrader_max_entry_slippage_pct = 5.0
    settings.chili_autotrader_daily_loss_cap_usd = 500.0
    settings.chili_autotrader_max_concurrent = 60  # global outer ceiling
    settings.chili_autotrader_max_concurrent_equity = 2
    settings.chili_autotrader_max_concurrent_crypto = 2
    settings.chili_autotrader_max_concurrent_options = 2
    settings.chili_autotrader_crypto_enabled = True
    settings.chili_autotrader_options_enabled = True
    settings.chili_autotrader_assumed_capital_usd = 100_000.0

    # Equity lane at its cap (2/2). Crypto lane has 0/2 open.
    ctx = RuleGateContext(
        current_price=10.0,
        autotrader_open_count=2,  # global counter
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 2, "crypto": 0, "options": 0},
    )

    eq_alert = BreakoutAlert(
        ticker="EQX", asset_type="stock", alert_tier="pattern_imminent",
        score_at_alert=0.7, price_at_alert=10.0, entry_price=10.0,
        stop_loss=9.5, target_price=11.5, user_id=1,
    )
    ok, reason, snap = passes_rule_gate(
        db, eq_alert, settings=settings, ctx=ctx, for_new_entry=True
    )
    assert not ok
    assert reason == "max_concurrent_equity"
    assert snap.get("concurrency_lane") == "equity"

    # Same context — but a crypto alert. Should NOT be blocked by the
    # equity lane filling up.
    cr_alert = BreakoutAlert(
        ticker="BTC-USD", asset_type="crypto", alert_tier="pattern_imminent",
        score_at_alert=0.7, price_at_alert=50000.0, entry_price=50000.0,
        stop_loss=48000.0, target_price=55000.0, user_id=1,
    )
    crypto_ctx = RuleGateContext(
        current_price=50000.0,
        autotrader_open_count=2,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 2, "crypto": 0, "options": 0},
    )
    ok, reason, snap = passes_rule_gate(
        db, cr_alert, settings=settings, ctx=crypto_ctx, for_new_entry=True
    )
    # Crypto path bypasses RTH/price-cap; concurrency lane is 'crypto'
    # with 0/2 open → should pass the lane gate (may still pass or fail
    # downstream checks, but the reason MUST NOT be max_concurrent_*).
    assert snap.get("concurrency_lane") == "crypto"
    assert not (reason or "").startswith("max_concurrent")


@patch("app.services.trading.auto_trader_rules.resolve_effective_capital", return_value=(100_000.0, "test"))
@patch("app.services.trading.auto_trader_rules.resolve_brain_risk_context", return_value={"dial_value": 1.0})
@patch("app.services.trading.auto_trader_rules.resolve_effective_slippage_pct", return_value=(2.0, "test"))
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_options_skips_underlying_stop_target_validation(
    _mock_port,
    _mock_slippage,
    _mock_brain,
    _mock_capital,
):
    """Options substitutions carry underlying stop/target levels, while
    entry_price is the option premium.
    """
    from app.services.trading.strategy_parameter import invalidate_cache

    invalidate_cache()
    db = None
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=5.0,
        chili_autotrader_max_symbol_price_usd=50.0,
        chili_autotrader_max_entry_slippage_pct=2.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=0.0,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=2,
        chili_autotrader_max_concurrent_crypto=2,
        chili_autotrader_max_concurrent_options=2,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=True,
        chili_autotrader_options_min_underlying_reward_risk=1.0,
        chili_autotrader_options_min_option_reward_risk=1.0,
        chili_autotrader_options_min_expected_value_pct=0.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
    )

    ctx = RuleGateContext(
        current_price=113.25,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )
    alert = BreakoutAlert(
        ticker="A",
        asset_type="options",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=113.25,
        entry_price=5.40,
        stop_loss=110.0,
        target_price=130.0,
        user_id=1,
        indicator_snapshot={
            "option_meta": {
                "strike": 115.0,
                "expiration": "2026-06-18",
                "option_type": "call",
                "limit_price": 5.40,
                "quantity": 1,
                "quote_snapshot": _option_quote_snapshot(),
                "delta": 0.42,
                "gamma": 0.03,
                "theta": -0.08,
                "vega": 0.11,
            }
        },
    )

    ok, reason, snap = passes_rule_gate(
        db, alert, settings=settings, ctx=ctx, for_new_entry=True
    )

    assert ok
    assert reason == "ok"
    assert snap.get("options_path") is True
    assert snap.get("projected_profit_pct") is None
    assert snap.get("projected_profit_pct_source") == "options_entry_quality"
    assert snap["option_entry_quality"]["option_reward_risk"] > 1.0
    assert snap.get("stop_target_validation_skipped_reason") == "options_underlying_levels"
    _mock_port.assert_called_once()
    assert _mock_port.call_args.kwargs.get("asset_type") == "options"
    assert snap.get("portfolio_asset_type") == "options"


@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_options_blocks_missing_quote_spread_before_risk(
    _mock_port,
):
    db = None
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=5.0,
        chili_autotrader_max_symbol_price_usd=50.0,
        chili_autotrader_max_entry_slippage_pct=2.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=0.0,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=2,
        chili_autotrader_max_concurrent_crypto=2,
        chili_autotrader_max_concurrent_options=2,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=True,
        chili_autotrader_options_min_underlying_reward_risk=1.0,
        chili_autotrader_options_min_option_reward_risk=1.0,
        chili_autotrader_options_min_expected_value_pct=0.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
    )
    ctx = RuleGateContext(
        current_price=113.25,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )
    alert = BreakoutAlert(
        ticker="A",
        asset_type="options",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=113.25,
        entry_price=5.40,
        stop_loss=110.0,
        target_price=130.0,
        user_id=1,
        indicator_snapshot={
            "option_meta": {
                "strike": 115.0,
                "expiration": "2026-06-18",
                "option_type": "call",
                "limit_price": 5.40,
                "quantity": 1,
                "delta": 0.42,
                "gamma": 0.03,
                "theta": -0.08,
                "vega": 0.11,
            }
        },
    )

    ok, reason, snap = passes_rule_gate(
        db, alert, settings=settings, ctx=ctx, for_new_entry=True
    )

    assert ok is False
    assert reason == "options_entry_quality:missing_option_quote_spread"
    assert snap["option_entry_quality"]["entry_bid"] is None
    assert snap["option_entry_quality"]["entry_ask"] is None
    assert "options_budget_check" not in snap
    _mock_port.assert_not_called()


@patch("app.services.trading.auto_trader_rules.resolve_effective_capital", return_value=(100_000.0, "test"))
@patch("app.services.trading.auto_trader_rules.resolve_brain_risk_context", return_value={"dial_value": 1.0})
@patch("app.services.trading.auto_trader_rules.resolve_effective_slippage_pct", return_value=(2.0, "test"))
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_options_blocks_missing_complete_greeks(
    _mock_port,
    _mock_slippage,
    _mock_brain,
    _mock_capital,
    monkeypatch,
):
    monkeypatch.delenv("CHILI_OPTIONS_BUDGET_BYPASS", raising=False)
    db = None
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=5.0,
        chili_autotrader_max_symbol_price_usd=50.0,
        chili_autotrader_max_entry_slippage_pct=2.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=0.0,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=2,
        chili_autotrader_max_concurrent_crypto=2,
        chili_autotrader_max_concurrent_options=2,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=True,
        chili_autotrader_options_min_underlying_reward_risk=1.0,
        chili_autotrader_options_min_option_reward_risk=1.0,
        chili_autotrader_options_min_expected_value_pct=0.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
    )
    ctx = RuleGateContext(
        current_price=113.25,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )
    alert = BreakoutAlert(
        ticker="A",
        asset_type="options",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=113.25,
        entry_price=5.40,
        stop_loss=110.0,
        target_price=130.0,
        user_id=1,
        indicator_snapshot={
            "option_meta": {
                "strike": 115.0,
                "expiration": "2026-06-18",
                "option_type": "call",
                "limit_price": 5.40,
                "quantity": 1,
                "quote_snapshot": _option_quote_snapshot(),
            }
        },
    )

    ok, reason, snap = passes_rule_gate(
        db, alert, settings=settings, ctx=ctx, for_new_entry=True
    )

    assert not ok
    assert reason == "options_budget:missing_complete_greeks"
    assert snap["options_budget_check"]["ok"] is False
    assert snap["options_budget_check"]["reasons"] == [
        "missing_complete_greeks:delta,gamma,theta,vega"
    ]
    _mock_port.assert_not_called()


def test_passes_rule_gate_options_blocks_budget_book_error(monkeypatch):
    monkeypatch.delenv("CHILI_OPTIONS_BUDGET_BYPASS", raising=False)
    db = MagicMock()
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=5.0,
        chili_autotrader_max_symbol_price_usd=50.0,
        chili_autotrader_max_entry_slippage_pct=2.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=0.0,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=2,
        chili_autotrader_max_concurrent_crypto=2,
        chili_autotrader_max_concurrent_options=2,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=True,
        chili_autotrader_options_min_underlying_reward_risk=1.0,
        chili_autotrader_options_min_option_reward_risk=1.0,
        chili_autotrader_options_min_expected_value_pct=0.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
    )
    ctx = RuleGateContext(
        current_price=113.25,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )
    alert = BreakoutAlert(
        ticker="A",
        asset_type="options",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=113.25,
        entry_price=5.40,
        stop_loss=110.0,
        target_price=130.0,
        user_id=1,
        indicator_snapshot={
            "option_meta": {
                "strike": 115.0,
                "expiration": "2026-06-18",
                "option_type": "call",
                "limit_price": 5.40,
                "quantity": 1,
                "quote_snapshot": _option_quote_snapshot(),
                "delta": 0.42,
                "gamma": 0.03,
                "theta": -0.08,
                "vega": 0.11,
            }
        },
    )

    with patch(
        "app.services.trading.options.portfolio_budget._sum_open_position_greeks",
        side_effect=RuntimeError("budget unavailable"),
    ) as open_book, patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        side_effect=AssertionError("portfolio gate must not run after budget failure"),
    ):
        ok, reason, snap = passes_rule_gate(
            db, alert, settings=settings, ctx=ctx, for_new_entry=True
        )

    assert not ok
    assert reason == "options_budget:budget_error:RuntimeError"
    assert snap["options_budget_check"]["ok"] is False
    assert snap["options_budget_check"]["reasons"] == ["budget_error:RuntimeError"]
    open_book.assert_called_once()


@patch("app.services.trading.auto_trader_rules.resolve_effective_capital", return_value=(100_000.0, "test"))
@patch("app.services.trading.auto_trader_rules.resolve_brain_risk_context", return_value={"dial_value": 1.0})
@patch("app.services.trading.auto_trader_rules.resolve_effective_slippage_pct", return_value=(2.0, "test"))
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_options_blocks_when_target_cannot_pay_premium(
    _mock_port,
    _mock_slippage,
    _mock_brain,
    _mock_capital,
):
    db = None
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=5.0,
        chili_autotrader_max_symbol_price_usd=50.0,
        chili_autotrader_max_entry_slippage_pct=2.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=0.0,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=2,
        chili_autotrader_max_concurrent_crypto=2,
        chili_autotrader_max_concurrent_options=2,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=True,
        chili_autotrader_options_min_underlying_reward_risk=1.0,
        chili_autotrader_options_min_option_reward_risk=1.0,
        chili_autotrader_options_min_expected_value_pct=0.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
    )
    ctx = RuleGateContext(
        current_price=113.25,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )
    alert = BreakoutAlert(
        ticker="A",
        asset_type="options",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=113.25,
        entry_price=5.40,
        stop_loss=110.0,
        target_price=119.0,
        user_id=1,
        indicator_snapshot={
            "option_meta": {
                "strike": 115.0,
                "expiration": "2026-06-18",
                "option_type": "call",
                "limit_price": 5.40,
                "quantity": 1,
                "quote_snapshot": _option_quote_snapshot(),
                "delta": 0.42,
                "gamma": 0.03,
                "theta": -0.08,
                "vega": 0.11,
            }
        },
    )

    ok, reason, snap = passes_rule_gate(
        db, alert, settings=settings, ctx=ctx, for_new_entry=True
    )

    assert not ok
    assert reason == "options_entry_quality:option_target_profit_non_positive"
    assert snap.get("projected_profit_pct") is None
    assert snap["option_entry_quality"]["option_profit_at_target"] < 0.0


@patch("app.services.trading.pattern_imminent_alerts.us_stock_session_open", return_value=True)
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_global_outer_ceiling_still_enforced(_mock_port, _mock_rth):
    """Even if the equity lane has headroom, the global outer ceiling on
    the SUM of all open trades still fires. Belt-and-braces."""
    db = MagicMock()
    settings = MagicMock()
    settings.chili_autotrader_rth_only = False
    settings.chili_autotrader_confidence_floor = 0.5
    settings.chili_autotrader_min_projected_profit_pct = 5.0
    settings.chili_autotrader_max_symbol_price_usd = 500.0
    settings.chili_autotrader_max_entry_slippage_pct = 5.0
    settings.chili_autotrader_daily_loss_cap_usd = 500.0
    settings.chili_autotrader_max_concurrent = 5  # global ceiling
    settings.chili_autotrader_max_concurrent_equity = 100
    settings.chili_autotrader_max_concurrent_crypto = 100
    settings.chili_autotrader_max_concurrent_options = 100
    settings.chili_autotrader_assumed_capital_usd = 100_000.0

    ctx = RuleGateContext(
        current_price=10.0,
        autotrader_open_count=5,  # at global ceiling
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 5, "crypto": 0, "options": 0},
    )

    alert = BreakoutAlert(
        ticker="EQY", asset_type="stock", alert_tier="pattern_imminent",
        score_at_alert=0.7, price_at_alert=10.0, entry_price=10.0,
        stop_loss=9.5, target_price=11.5, user_id=1,
    )
    ok, reason, _ = passes_rule_gate(
        db, alert, settings=settings, ctx=ctx, for_new_entry=True
    )
    assert not ok
    # Equity lane is at 5/100 (headroom), but global is at 5/5 (full).
    # The equity lane check fires first only when lane_open >= lane_cap,
    # which is NOT the case here (5 < 100). The global ceiling fires
    # next.
    assert reason == "max_concurrent_global"


def test_evaluate_entry_edge_uses_directional_alert_outcomes_as_cold_start():
    class _Result:
        def __init__(self, rows=None, first=None):
            self._rows = rows or []
            self._first = first

        def mappings(self):
            return self

        def all(self):
            return self._rows

        def first(self):
            return self._first

    class _Query:
        def filter(self, *_args, **_kwargs):
            return self

        def one_or_none(self):
            return None

    class _Db:
        def query(self, *_args, **_kwargs):
            return _Query()

        def execute(self, sql, params=None):
            text = str(sql)
            if "pattern_alert_directional_outcome" in text:
                if "UPPER(ticker) = UPPER" in text:
                    return _Result(rows=[
                        {
                            "ticker": "EDGE",
                            "window_max_favorable_pct": 14.0,
                            "window_max_adverse_pct": -3.0,
                            "directional_correct": True,
                        }
                        for _ in range(8)
                    ])
                return _Result(rows=[])
            return _Result(first=None)

    alert = BreakoutAlert(
        ticker="EDGE",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        scan_pattern_id=777,
        score_at_alert=0.4,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=112.0,
        user_id=1,
    )
    settings = SimpleNamespace(
        chili_realized_ev_min_trades=5,
        chili_autotrader_directional_probability_z=1.0,
        chili_autotrader_directional_probability_max_rows=30,
    )

    decision = evaluate_entry_edge(
        _Db(),
        alert,
        settings=settings,
        pat_ctx={},
        confidence=0.55,
    )

    assert decision.allowed
    assert decision.snapshot["probability_source"] == "directional_mfe_mae_ticker"
    assert decision.snapshot["probability"] > 0.65
    assert decision.snapshot["probability_details"]["directional_evidence"]["ticker"]["reward_hits"] == 8
    assert decision.snapshot["expected_net_pct"] > 0


def test_evaluate_entry_edge_directional_outcomes_must_match_payoff_geometry():
    class _Result:
        def __init__(self, rows=None, first=None):
            self._rows = rows or []
            self._first = first

        def mappings(self):
            return self

        def all(self):
            return self._rows

        def first(self):
            return self._first

    class _Query:
        def filter(self, *_args, **_kwargs):
            return self

        def one_or_none(self):
            return None

    class _Db:
        def query(self, *_args, **_kwargs):
            return _Query()

        def execute(self, sql, params=None):
            text = str(sql)
            if "pattern_alert_directional_outcome" in text:
                if "UPPER(ticker) = UPPER" in text:
                    return _Result(rows=[
                        {
                            "ticker": "EDGE",
                            "window_max_favorable_pct": 2.0,
                            "window_max_adverse_pct": -3.0,
                            "directional_correct": True,
                        }
                        for _ in range(12)
                    ])
                return _Result(rows=[])
            return _Result(first=None)

    alert = BreakoutAlert(
        ticker="EDGE",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        scan_pattern_id=777,
        score_at_alert=0.9,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=112.0,
        user_id=1,
    )
    settings = SimpleNamespace(
        chili_realized_ev_min_trades=5,
        chili_autotrader_directional_probability_z=1.0,
        chili_autotrader_directional_probability_max_rows=30,
    )

    decision = evaluate_entry_edge(
        _Db(),
        alert,
        settings=settings,
        pat_ctx={},
        confidence=0.95,
    )

    assert not decision.allowed
    assert decision.reason == "non_positive_expected_edge"
    assert decision.snapshot["probability_source"] == "directional_mfe_mae_ticker"
    assert decision.snapshot["probability_details"]["directional_evidence"]["ticker"]["reward_hits"] == 0
    assert decision.snapshot["expected_net_pct"] < 0


def test_evaluate_entry_edge_selects_managed_exit_for_overextended_crypto_bracket():
    class _Result:
        def __init__(self, rows=None, first=None):
            self._rows = rows or []
            self._first = first

        def mappings(self):
            return self

        def all(self):
            return self._rows

        def first(self):
            return self._first

    class _Query:
        def filter(self, *_args, **_kwargs):
            return self

        def one_or_none(self):
            return None

    class _Db:
        def query(self, *_args, **_kwargs):
            return _Query()

        def execute(self, sql, params=None):
            text = str(sql)
            if "pattern_alert_directional_outcome" in text:
                if "UPPER(ticker) = UPPER" in text:
                    return _Result(rows=[
                        {
                            "ticker": "EDGE",
                            "window_max_favorable_pct": 2.0,
                            "window_max_adverse_pct": -0.2,
                            "directional_correct": True,
                        }
                        for _ in range(12)
                    ])
                return _Result(rows=[])
            return _Result(first=None)

    alert = BreakoutAlert(
        ticker="EDGE",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        scan_pattern_id=777,
        score_at_alert=0.9,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=112.0,
        user_id=1,
    )
    settings = SimpleNamespace(
        chili_realized_ev_min_trades=5,
        chili_autotrader_directional_probability_z=1.0,
        chili_autotrader_directional_probability_max_rows=30,
        chili_autotrader_managed_edge_mode="authoritative",
        chili_autotrader_managed_edge_asset_types="crypto",
        chili_autotrader_managed_edge_min_directional_samples=8,
        chili_autotrader_managed_edge_capture_fraction=0.60,
        chili_autotrader_managed_edge_adverse_buffer=1.50,
        chili_autotrader_managed_edge_static_to_managed_reward_ratio=1.50,
        chili_autotrader_managed_edge_min_reward_fraction=0.005,
        chili_autotrader_managed_edge_max_reward_fraction=0.08,
        chili_autotrader_managed_edge_min_reward_risk=1.25,
        chili_autotrader_managed_edge_min_expected_net_pct=0.0,
    )

    decision = evaluate_entry_edge(
        _Db(),
        alert,
        settings=settings,
        pat_ctx={},
        confidence=0.95,
    )

    assert decision.allowed
    assert decision.reason == "positive_expected_edge"
    assert decision.snapshot["edge_geometry_source"] == "managed_directional_exit"
    assert decision.snapshot["managed_exit_edge"]["selected"] is True
    assert decision.snapshot["full_bracket_edge"]["expected_net_pct"] < 0
    assert (
        decision.snapshot["full_bracket_edge"]["probability_details"]
        ["directional_evidence"]["ticker"]["reward_hits"]
    ) == 0
    assert (
        decision.snapshot["probability_details"]["directional_evidence"]
        ["ticker"]["reward_hits"]
    ) == 12
    assert decision.snapshot["managed_exit_edge"]["geometry"]["managed_target_price"] < 112.0
    assert decision.snapshot["managed_exit_edge"]["geometry"]["managed_stop_price"] > 90.0
    assert decision.snapshot["expected_net_pct"] > 0


def test_evaluate_entry_edge_selects_managed_exit_for_overextended_stock_by_default():
    class _Result:
        def __init__(self, rows=None, first=None):
            self._rows = rows or []
            self._first = first

        def mappings(self):
            return self

        def all(self):
            return self._rows

        def first(self):
            return self._first

    class _Query:
        def filter(self, *_args, **_kwargs):
            return self

        def one_or_none(self):
            return None

    class _Db:
        def query(self, *_args, **_kwargs):
            return _Query()

        def execute(self, sql, params=None):
            text = str(sql)
            if "pattern_alert_directional_outcome" in text:
                if "UPPER(ticker) = UPPER" in text:
                    return _Result(rows=[
                        {
                            "ticker": "STOCKEDGE",
                            "window_max_favorable_pct": 2.0,
                            "window_max_adverse_pct": -0.2,
                            "directional_correct": True,
                        }
                        for _ in range(12)
                    ])
                return _Result(rows=[])
            return _Result(first=None)

    alert = BreakoutAlert(
        ticker="STOCKEDGE",
        asset_type="stock",
        alert_tier="pattern_imminent",
        scan_pattern_id=777,
        score_at_alert=0.9,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=112.0,
        user_id=1,
    )
    default_stock_settings = SimpleNamespace(
        chili_autotrader_managed_edge_mode="authoritative",
    )

    decision = evaluate_entry_edge(
        _Db(),
        alert,
        settings=default_stock_settings,
        pat_ctx={},
        confidence=0.95,
    )

    assert decision.allowed
    assert decision.reason == "positive_expected_edge"
    assert decision.snapshot["edge_geometry_source"] == "managed_directional_exit"
    assert decision.snapshot["managed_exit_edge"]["selected"] is True
    assert (
        "stock"
        in decision.snapshot["managed_exit_edge"]["geometry"]["allowed_asset_types"]
    )
    assert decision.snapshot["full_bracket_edge"]["expected_net_pct"] < 0

    crypto_only_settings = SimpleNamespace(
        chili_autotrader_managed_edge_mode="authoritative",
        chili_autotrader_managed_edge_asset_types="crypto",
    )
    blocked = evaluate_entry_edge(
        _Db(),
        alert,
        settings=crypto_only_settings,
        pat_ctx={},
        confidence=0.95,
    )

    assert not blocked.allowed
    assert blocked.reason == "non_positive_expected_edge"
    assert (
        blocked.snapshot["managed_exit_edge"]["geometry"]["reason"]
        == "asset_type_not_enabled"
    )


def test_evaluate_entry_edge_shrinks_uncalibrated_alert_confidence():
    class _Result:
        def mappings(self):
            return self

        def all(self):
            return []

        def first(self):
            return None

    class _Db:
        def execute(self, *_args, **_kwargs):
            return _Result()

    alert = BreakoutAlert(
        ticker="NOEVID",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.9,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=110.0,
        user_id=1,
    )
    settings = SimpleNamespace(chili_autotrader_alert_confidence_probability_weight=0.25)

    decision = evaluate_entry_edge(
        _Db(),
        alert,
        settings=settings,
        pat_ctx={},
        confidence=0.9,
    )

    assert decision.snapshot["probability_source"] == "alert_confidence_shrunk"
    assert decision.snapshot["probability"] == pytest.approx(0.6, rel=1e-6)
    assert (
        decision.snapshot["probability_details"]["alert_confidence"]["reason"]
        == "score_confidence_is_not_a_calibrated_win_probability"
    )


def test_evaluate_entry_edge_uses_regime_effective_sample_n_not_dimension_sum():
    class _Result:
        def mappings(self):
            return self

        def all(self):
            return []

        def first(self):
            return None

    class _Db:
        def execute(self, *_args, **_kwargs):
            return _Result()

    alert = BreakoutAlert(
        ticker="REGIME",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.5,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=115.0,
        user_id=1,
    )
    settings = SimpleNamespace(chili_realized_ev_min_trades=5)

    decision = evaluate_entry_edge(
        _Db(),
        alert,
        settings=settings,
        pat_ctx={
            "hit_rate": 0.8,
            "n_cells": 8,
            "n_trades_sum": 80,
            "n_trades_effective": 10,
        },
        confidence=0.5,
    )

    assert decision.snapshot["probability_source"] == "pattern_regime_hit_rate_shrunk"
    assert decision.snapshot["probability_sample_n"] == 10
    assert decision.snapshot["probability"] == pytest.approx(0.7, rel=1e-6)


def test_evaluate_entry_edge_guards_dynamic_exit_geometry_sample_count():
    class _Result:
        def mappings(self):
            return self

        def all(self):
            return []

        def first(self):
            return None

    class _Query:
        def __init__(self, pattern):
            self._pattern = pattern

        def filter(self, *_args, **_kwargs):
            return self

        def one_or_none(self):
            return self._pattern

    class _Db:
        def __init__(self, pattern):
            self._pattern = pattern

        def query(self, *_args, **_kwargs):
            return _Query(self._pattern)

        def execute(self, *_args, **_kwargs):
            return _Result()

    pattern = SimpleNamespace(
        corrected_trade_count=6,
        corrected_win_rate=0.7,
        corrected_avg_return_pct=1.0,
        trade_count=6,
        win_rate=0.7,
        avg_return_pct=1.0,
        raw_realized_trade_count=1,
        avg_winner_pct=0.06,
        avg_loser_pct=-0.02,
        payoff_ratio=3.0,
        payoff_ratio_n=6,
    )
    alert = BreakoutAlert(
        ticker="GEOM",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        scan_pattern_id=888,
        score_at_alert=0.5,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=90.0,
        target_price=115.0,
        user_id=1,
    )

    decision = evaluate_entry_edge(
        _Db(pattern),
        alert,
        settings=SimpleNamespace(chili_realized_ev_min_trades=5),
        pat_ctx={},
        confidence=0.5,
    )

    geom = decision.snapshot["dynamic_exit_geometry"]
    assert geom["used"] is True
    assert geom["realized_sample_n"] == 1
    assert geom["realized_sample_n_guard"] == "closed_sample_count_guard"
