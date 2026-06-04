from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.trading.brain_work import emitters, execution_hooks
from app.services.trading.brain_work.execution_attribution import (
    paper_trade_close_attribution_dict,
    trade_close_attribution_dict,
)


def test_trade_close_attribution_uses_contract_aware_option_return() -> None:
    trade = SimpleNamespace(
        scan_pattern_id=42,
        strategy_proposal_id=99,
        pnl=40.0,
        entry_price=1.25,
        exit_price=716.0,
        quantity=2.0,
        direction="long",
        broker_source="robinhood",
        broker_order_id="order-1",
        asset_kind="option",
        tags=None,
        indicator_snapshot={"asset_type": "options"},
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )

    out = trade_close_attribution_dict(trade)

    assert out["pnl"] == pytest.approx(40.0)
    assert out["realized_return_pct"] == pytest.approx(16.0)
    assert out["tca_cost_pct"] == pytest.approx(0.30)
    assert out["net_return_pct"] == pytest.approx(15.70)
    assert out["exit_price"] == pytest.approx(716.0)


def test_trade_close_attribution_rejects_ambiguous_option_price_return() -> None:
    trade = SimpleNamespace(
        scan_pattern_id=42,
        strategy_proposal_id=None,
        pnl=None,
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        direction="long",
        broker_source="robinhood",
        broker_order_id="order-2",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )

    out = trade_close_attribution_dict(trade)

    assert out["realized_return_pct"] is None
    assert out["net_return_pct"] is None
    assert out["tca_cost_pct"] == pytest.approx(0.30)


def test_trade_close_attribution_ignores_unverified_extreme_tca() -> None:
    trade = SimpleNamespace(
        scan_pattern_id=42,
        strategy_proposal_id=99,
        pnl=10.0,
        entry_price=100.0,
        exit_price=110.0,
        quantity=1.0,
        direction="long",
        broker_source="coinbase",
        broker_order_id="",
        broker_status="",
        avg_fill_price=None,
        tca_entry_slippage_bps=1426.0,
        tca_exit_slippage_bps=1361.0,
    )

    out = trade_close_attribution_dict(trade)

    assert out["realized_return_pct"] == pytest.approx(10.0)
    assert out["tca_cost_pct"] is None
    assert out["net_return_pct"] is None
    assert out["tca_entry_slippage_bps"] is None
    assert out["tca_exit_slippage_bps"] is None


def test_trade_close_attribution_keeps_broker_backed_extreme_tca() -> None:
    trade = SimpleNamespace(
        scan_pattern_id=42,
        strategy_proposal_id=99,
        pnl=10.0,
        entry_price=100.0,
        exit_price=110.0,
        quantity=1.0,
        direction="long",
        broker_source="coinbase",
        broker_order_id="order-verified",
        broker_status="filled",
        avg_fill_price=None,
        tca_entry_slippage_bps=1426.0,
        tca_exit_slippage_bps=1361.0,
    )

    out = trade_close_attribution_dict(trade)

    assert out["tca_cost_pct"] == pytest.approx(27.87)
    assert out["net_return_pct"] == pytest.approx(-17.87)
    assert out["tca_entry_slippage_bps"] == pytest.approx(1426.0)
    assert out["tca_exit_slippage_bps"] == pytest.approx(1361.0)


def test_paper_trade_close_attribution_uses_contract_aware_option_return() -> None:
    trade = SimpleNamespace(
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        pnl=40.0,
        pnl_pct=9999.0,
        entry_price=1.25,
        exit_price=716.0,
        quantity=2.0,
        direction="long",
        exit_reason="target",
        signal_json={"asset_class": "robinhood_options"},
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )

    out = paper_trade_close_attribution_dict(trade)

    assert out["pnl"] == pytest.approx(40.0)
    assert out["paper_shadow_of_alert_id"] == 77
    assert out["realized_return_pct"] == pytest.approx(16.0)
    assert out["tca_cost_pct"] == pytest.approx(0.30)
    assert out["net_return_pct"] == pytest.approx(15.70)
    assert out["exit_price"] == pytest.approx(716.0)


def test_paper_trade_close_attribution_rejects_legacy_option_pct_without_pnl() -> None:
    trade = SimpleNamespace(
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        pnl=None,
        pnl_pct=17755.61,
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        direction="long",
        exit_reason="target",
        signal_json={"asset_type": "options"},
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )

    out = paper_trade_close_attribution_dict(trade)

    assert out["realized_return_pct"] is None
    assert out["net_return_pct"] is None
    assert out["tca_cost_pct"] == pytest.approx(0.30)


def test_emit_paper_trade_closed_outcome_preserves_core_fields_with_extra(
    monkeypatch,
) -> None:
    captured = {}

    def _fake_enqueue(_db, **kwargs):
        captured.update(kwargs)
        return 123

    monkeypatch.setattr(emitters, "enqueue_outcome_event", _fake_enqueue)

    out = emitters.emit_paper_trade_closed_outcome(
        None,
        paper_trade_id=5,
        user_id=9,
        scan_pattern_id=42,
        ticker="SPY",
        pnl=40.0,
        exit_reason="target",
        extra={
            "paper_trade_id": 999,
            "scan_pattern_id": 999,
            "pnl": -1.0,
            "realized_return_pct": 16.0,
        },
    )

    assert out == 123
    assert captured["event_type"] == "paper_trade_closed"
    payload = captured["payload"]
    assert payload["paper_trade_id"] == 5
    assert payload["scan_pattern_id"] == 42
    assert payload["pnl"] == pytest.approx(40.0)
    assert payload["realized_return_pct"] == pytest.approx(16.0)


def test_on_paper_trade_closed_emits_contract_aware_extra(monkeypatch) -> None:
    captured = {}

    def _fake_emit(_db, **kwargs):
        captured.update(kwargs)
        return 123

    monkeypatch.setattr(execution_hooks, "emit_paper_trade_closed_outcome", _fake_emit)
    monkeypatch.setattr(execution_hooks, "enqueue_or_refresh_debounced_work", lambda *a, **k: 1)
    monkeypatch.setattr(execution_hooks, "_record_venue_truth", lambda *a, **k: None)
    monkeypatch.setattr(
        execution_hooks,
        "_refresh_rolling_cost_estimate",
        lambda *a, **k: None,
    )
    paper_trade = SimpleNamespace(
        id=5,
        user_id=9,
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        ticker="SPY",
        pnl=40.0,
        pnl_pct=9999.0,
        entry_price=1.25,
        exit_price=716.0,
        quantity=2.0,
        direction="long",
        exit_reason="target",
        signal_json={"asset_class": "robinhood_options"},
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )

    execution_hooks.on_paper_trade_closed(None, paper_trade)

    assert captured["paper_trade_id"] == 5
    assert captured["scan_pattern_id"] == 42
    assert captured["pnl"] == pytest.approx(40.0)
    assert captured["extra"]["realized_return_pct"] == pytest.approx(16.0)
    assert captured["extra"]["net_return_pct"] == pytest.approx(15.70)


def test_on_paper_trade_closed_queues_exit_variant_for_time_decay_edge_miss(
    monkeypatch,
) -> None:
    closed: dict[str, object] = {}
    work: list[dict[str, object]] = []

    monkeypatch.setattr(
        execution_hooks,
        "emit_paper_trade_closed_outcome",
        lambda _db, **kwargs: closed.update(kwargs) or 123,
    )
    monkeypatch.setattr(
        execution_hooks,
        "enqueue_or_refresh_debounced_work",
        lambda *a, **k: 1,
    )
    monkeypatch.setattr(execution_hooks, "_record_venue_truth", lambda *a, **k: None)
    monkeypatch.setattr(
        execution_hooks,
        "_refresh_rolling_cost_estimate",
        lambda *a, **k: None,
    )

    def _fake_profitability_work(_db, **kwargs):
        work.append(kwargs)
        return 456

    monkeypatch.setattr(
        "app.services.trading.edge_reliability.emit_targeted_profitability_work",
        _fake_profitability_work,
    )

    paper_trade = SimpleNamespace(
        id=81,
        user_id=9,
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        ticker="EDGE-USD",
        pnl=-2.0,
        pnl_pct=None,
        entry_price=100.0,
        exit_price=98.0,
        quantity=1.0,
        direction="long",
        exit_reason="exit_engine_time_decay",
        signal_json={
            "paper_shadow": True,
            "entry_edge": {"expected_net_pct": 3.2},
            "_paper_meta": {
                "exit_config": {
                    "timeframe": "1m",
                    "max_bars": 20,
                    "target_reward_fraction": 0.06,
                    "stop_loss_fraction": 0.02,
                    "exit_defaults_source": "backtest_classifier",
                },
                "dynamic_monitor": {"last_reason": "no_dynamic_exit"},
            },
        },
        tca_entry_slippage_bps=None,
        tca_exit_slippage_bps=None,
    )

    execution_hooks.on_paper_trade_closed(object(), paper_trade)

    assert closed["paper_trade_id"] == 81
    assert work
    request = work[0]
    assert request["event_type"] == "exit_variant_refresh"
    assert request["scan_pattern_id"] == 42
    assert request["source"] == execution_hooks.TIME_DECAY_EXIT_VARIANT_SOURCE
    assert request["asset_class"] == "crypto"
    assert request["evidence_fingerprint"] == "td_loss_e3_crypto_v1"
    payload = request["payload"]
    assert payload["cash_deployment_category"] == "positive_ev_time_decay_loss"
    assert payload["expected_net_pct"] == pytest.approx(3.2)
    assert payload["realized_return_pct"] == pytest.approx(-2.0)
    assert payload["expected_evidence_value"] == pytest.approx(5.2)
    assert payload["paper_shadow"] is True
    assert payload["timeframe"] == "1m"
    assert payload["max_bars"] == 20
    assert payload["target_reward_fraction"] == pytest.approx(0.06)
    assert payload["stop_loss_fraction"] == pytest.approx(0.02)


def test_time_decay_exit_variant_enqueue_failure_does_not_block_digest(
    monkeypatch,
) -> None:
    digest_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        execution_hooks,
        "emit_paper_trade_closed_outcome",
        lambda *a, **k: 123,
    )
    monkeypatch.setattr(
        execution_hooks,
        "enqueue_or_refresh_debounced_work",
        lambda *a, **kwargs: digest_calls.append(kwargs) or 1,
    )
    monkeypatch.setattr(execution_hooks, "_record_venue_truth", lambda *a, **k: None)
    monkeypatch.setattr(
        execution_hooks,
        "_refresh_rolling_cost_estimate",
        lambda *a, **k: None,
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated enqueue failure")

    monkeypatch.setattr(
        "app.services.trading.edge_reliability.emit_targeted_profitability_work",
        _boom,
    )

    paper_trade = SimpleNamespace(
        id=82,
        user_id=9,
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        ticker="EDGE-USD",
        pnl=-2.0,
        pnl_pct=None,
        entry_price=100.0,
        exit_price=98.0,
        quantity=1.0,
        direction="long",
        exit_reason="exit_engine_time_decay",
        signal_json={
            "paper_shadow": True,
            "entry_edge": {"expected_net_pct": 3.2},
        },
        tca_entry_slippage_bps=None,
        tca_exit_slippage_bps=None,
    )

    execution_hooks.on_paper_trade_closed(object(), paper_trade)

    assert digest_calls
    assert digest_calls[0]["event_type"] == "execution_feedback_digest"


def test_time_decay_exit_variant_sweep_enqueues_bounded_positive_edge_misses(
    monkeypatch,
) -> None:
    work: list[dict[str, object]] = []

    def _fake_profitability_work(_db, **kwargs):
        work.append(kwargs)
        return 900 + len(work)

    monkeypatch.setattr(
        "app.services.trading.edge_reliability.emit_targeted_profitability_work",
        _fake_profitability_work,
    )

    eligible = SimpleNamespace(
        id=91,
        user_id=9,
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        ticker="EDGE-USD",
        pnl=-2.0,
        pnl_pct=None,
        entry_price=100.0,
        exit_price=98.0,
        quantity=1.0,
        direction="long",
        exit_reason="exit_engine_time_decay",
        signal_json={
            "paper_shadow": True,
            "entry_edge": {"expected_net_pct": 3.2},
        },
        tca_entry_slippage_bps=None,
        tca_exit_slippage_bps=None,
    )
    no_edge = SimpleNamespace(
        id=92,
        scan_pattern_id=43,
        paper_shadow_of_alert_id=78,
        ticker="NOEDGE-USD",
        pnl=-1.0,
        pnl_pct=None,
        entry_price=100.0,
        exit_price=99.0,
        quantity=1.0,
        direction="long",
        exit_reason="exit_engine_time_decay",
        signal_json={"paper_shadow": True},
        tca_entry_slippage_bps=None,
        tca_exit_slippage_bps=None,
    )
    not_shadow = SimpleNamespace(
        id=93,
        scan_pattern_id=44,
        paper_shadow_of_alert_id=None,
        ticker="PLAIN",
        pnl=-1.0,
        pnl_pct=None,
        entry_price=100.0,
        exit_price=99.0,
        quantity=1.0,
        direction="long",
        exit_reason="exit_engine_time_decay",
        signal_json={"entry_edge": {"expected_net_pct": 2.0}},
        tca_entry_slippage_bps=None,
        tca_exit_slippage_bps=None,
    )

    class _Query:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def limit(self, _limit):
            return self

        def all(self):
            return [eligible, no_edge, not_shadow]

    class _Db:
        def query(self, _model):
            return _Query([eligible, no_edge, not_shadow])

    result = execution_hooks.enqueue_recent_time_decay_exit_variant_work(
        _Db(),
        lookback_hours=24,
        limit=5,
    )

    assert result["ok"] is True
    assert result["candidate_rows"] == 3
    assert result["queued"] == 1
    assert result["skipped_no_positive_edge"] == 1
    assert result["skipped_not_shadow"] == 1
    assert work[0]["event_type"] == "exit_variant_refresh"
    assert work[0]["source"] == execution_hooks.TIME_DECAY_EXIT_VARIANT_SOURCE


def test_time_decay_exit_variant_sweep_settings_drive_window_and_limit(
    monkeypatch,
) -> None:
    work: list[dict[str, object]] = []
    captured: dict[str, int] = {}

    monkeypatch.setenv("BRAIN_WORK_TIME_DECAY_EXIT_VARIANT_SWEEP_ENABLED", "true")
    monkeypatch.setenv("BRAIN_WORK_TIME_DECAY_EXIT_VARIANT_SWEEP_LOOKBACK_HOURS", "12.5")
    monkeypatch.setenv("BRAIN_WORK_TIME_DECAY_EXIT_VARIANT_SWEEP_LIMIT", "1")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr(execution_hooks, "settings", settings)

    def _fake_profitability_work(_db, **kwargs):
        work.append(kwargs)
        return 900 + len(work)

    monkeypatch.setattr(
        "app.services.trading.edge_reliability.emit_targeted_profitability_work",
        _fake_profitability_work,
    )

    eligible = SimpleNamespace(
        id=91,
        user_id=9,
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        ticker="EDGE-USD",
        pnl=-2.0,
        pnl_pct=None,
        entry_price=100.0,
        exit_price=98.0,
        quantity=1.0,
        direction="long",
        exit_reason="exit_engine_time_decay",
        signal_json={
            "paper_shadow": True,
            "entry_edge": {"expected_net_pct": 3.2},
        },
        tca_entry_slippage_bps=None,
        tca_exit_slippage_bps=None,
    )

    class _Query:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def limit(self, limit):
            captured["fetch_limit"] = int(limit)
            return self

        def all(self):
            return [eligible, eligible]

    class _Db:
        def query(self, _model):
            return _Query()

    result = execution_hooks.enqueue_recent_time_decay_exit_variant_work(_Db())

    assert settings.brain_work_time_decay_exit_variant_sweep_lookback_hours == (
        pytest.approx(12.5)
    )
    assert settings.brain_work_time_decay_exit_variant_sweep_limit == 1
    assert result["lookback_hours"] == pytest.approx(12.5)
    assert result["limit"] == 1
    assert result["checked"] == 1
    assert result["queued"] == 1
    assert captured["fetch_limit"] == (
        execution_hooks.TIME_DECAY_EXIT_VARIANT_SWEEP_FETCH_MULTIPLIER
    )

    monkeypatch.setenv("BRAIN_WORK_TIME_DECAY_EXIT_VARIANT_SWEEP_ENABLED", "false")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr(execution_hooks, "settings", settings)

    assert execution_hooks.enqueue_recent_time_decay_exit_variant_work(_Db()) == {
        "ok": True,
        "skipped": True,
        "reason": "disabled_by_setting",
    }
