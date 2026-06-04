"""Profitability brain-work handler regressions."""

from __future__ import annotations

from types import SimpleNamespace

from app.config import Settings
from app.services.trading.brain_work.handlers.profitability import (
    handle_exit_variant_refresh,
)
from app.services.trading.edge_reliability import (
    EDGE_RELIABILITY_REFRESH,
    EXIT_VARIANT_DIAGNOSTIC,
)


def test_exit_variant_refresh_fast_skips_negative_ev_without_learning(monkeypatch) -> None:
    from app.services.trading.brain_work import ledger
    import app.services.trading.learning as learning

    calls = {"loss_reports": 0, "forks": 0}
    outcomes: list[dict[str, object]] = []

    def _loss_reports(*args, **kwargs):
        calls["loss_reports"] += 1
        raise AssertionError("negative zero-value refresh should not scan loss reports")

    def _forks(*args, **kwargs):
        calls["forks"] += 1
        raise AssertionError("negative zero-value refresh should not fork variants")

    monkeypatch.setattr(learning, "_edge_debt_loss_reports", _loss_reports)
    monkeypatch.setattr(learning, "fork_edge_learned_exit_variants", _forks)

    def _enqueue_outcome_event(_db, **kwargs):
        outcomes.append(kwargs)
        return 1001

    monkeypatch.setattr(ledger, "enqueue_outcome_event", _enqueue_outcome_event)

    ev = SimpleNamespace(
        id=901,
        payload={
            "scan_pattern_id": 537,
            "asset_class": "crypto",
            "cash_deployment_category": "negative_ev",
            "calibrated_ev_after_cost_pct": -1.2,
            "expected_evidence_value": 0.0,
            "evidence_fingerprint": "neg-fp",
            "graduation_blocker": "execution_blocked",
        },
    )

    handle_exit_variant_refresh(object(), ev, user_id=None)

    assert calls == {"loss_reports": 0, "forks": 0}
    assert len(outcomes) == 1
    assert outcomes[0]["event_type"] == EXIT_VARIANT_DIAGNOSTIC
    payload = outcomes[0]["payload"]
    assert payload["scan_pattern_id"] == 537
    assert payload["created_count"] == 0
    assert payload["created_child_ids"] == []
    assert payload["fast_skipped"] is True
    assert payload["skip_reason"] == "negative_ev_no_exit_variant_birth"


def test_exit_variant_refresh_uses_evolution_for_positive_evidence(monkeypatch) -> None:
    from app.services.trading.brain_work import ledger
    import app.services.trading.learning as learning

    calls = {"loss_reports": 0, "forks": 0}
    outcomes: list[dict[str, object]] = []
    report = {"avg_expected_net_pct": 0.8, "closed_count": 9}

    def _loss_reports(_db, *, lookback_days):
        calls["loss_reports"] += 1
        assert lookback_days == 30
        return {537: report}

    def _forks(_db, pattern_id, *, edge_loss_report, diagnostics):
        calls["forks"] += 1
        assert pattern_id == 537
        assert edge_loss_report == report
        diagnostics["variant_label"] = "learned_exit_ev_positive"
        return [9001]

    monkeypatch.setattr(learning, "_edge_debt_loss_reports", _loss_reports)
    monkeypatch.setattr(learning, "fork_edge_learned_exit_variants", _forks)

    def _enqueue_outcome_event(_db, **kwargs):
        outcomes.append(kwargs)
        return 1002

    monkeypatch.setattr(ledger, "enqueue_outcome_event", _enqueue_outcome_event)

    ev = SimpleNamespace(
        id=902,
        payload={
            "scan_pattern_id": 537,
            "asset_class": "stock",
            "cash_deployment_category": "positive_ev_execution_blocked",
            "calibrated_ev_after_cost_pct": 1.2,
            "expected_evidence_value": 12.5,
            "evidence_fingerprint": "pos-fp",
            "graduation_blocker": "execution_blocked",
        },
    )

    handle_exit_variant_refresh(object(), ev, user_id=None)

    assert calls == {"loss_reports": 1, "forks": 1}
    assert len(outcomes) == 1
    assert outcomes[0]["event_type"] == EXIT_VARIANT_DIAGNOSTIC
    payload = outcomes[0]["payload"]
    assert payload["created_count"] == 1
    assert payload["created_child_ids"] == [9001]
    assert payload["variant_label"] == "learned_exit_ev_positive"
    assert payload.get("fast_skipped") is None


def test_exit_variant_refresh_routes_cost_gate_blocks_to_edge_refresh(monkeypatch) -> None:
    from app.services.trading.brain_work import ledger
    import app.services.trading.edge_reliability as edge_reliability
    import app.services.trading.learning as learning

    outcomes: list[dict[str, object]] = []
    refreshes: list[dict[str, object]] = []

    def _loss_reports(*args, **kwargs):
        raise AssertionError("cost-gate execution debt should not scan loss reports")

    def _forks(*args, **kwargs):
        raise AssertionError("cost-gate execution debt should not fork exit variants")

    monkeypatch.setattr(learning, "_edge_debt_loss_reports", _loss_reports)
    monkeypatch.setattr(learning, "fork_edge_learned_exit_variants", _forks)

    def _enqueue_outcome_event(_db, **kwargs):
        outcomes.append(kwargs)
        return 1005

    def _emit_edge_refresh(_db, scan_pattern_id, **kwargs):
        refreshes.append({"scan_pattern_id": scan_pattern_id, **kwargs})
        return 222

    monkeypatch.setattr(ledger, "enqueue_outcome_event", _enqueue_outcome_event)
    monkeypatch.setattr(
        edge_reliability,
        "emit_edge_reliability_refresh_requested",
        _emit_edge_refresh,
    )

    ev = SimpleNamespace(
        id=905,
        payload={
            "scan_pattern_id": 537,
            "asset_class": "stock",
            "source": "autotrader_cost_gate_execution_blocked",
            "cash_deployment_category": "positive_ev_execution_blocked",
            "graduation_blocker": "execution_blocked",
            "cost_gate_reason": "rh_below_tca_threshold",
            "cost_gate_edge_gap_pct": 0.9,
            "cost_gate_tca_cost_bps": 180,
            "evidence_fingerprint": "cost-gate-fp",
            "window_days": "not-a-number",
        },
    )

    handle_exit_variant_refresh(object(), ev, user_id=None)

    assert refreshes == [
        {
            "scan_pattern_id": 537,
            "source": "cost_gate_execution_blocked",
            "asset_class": "stock",
            "window_days": 30,
            "evidence_fingerprint": "cost-gate-fp",
        }
    ]
    assert len(outcomes) == 1
    assert outcomes[0]["event_type"] == EXIT_VARIANT_DIAGNOSTIC
    payload = outcomes[0]["payload"]
    assert payload["skip_reason"] == "execution_cost_block_routed_to_edge_reliability"
    assert payload["created_count"] == 0
    assert payload["created_child_ids"] == []
    assert payload["fast_skipped"] is True
    assert payload["edge_reliability_refresh"] == {
        "queued": True,
        "event_id": 222,
        "event_type": EDGE_RELIABILITY_REFRESH,
        "reason": "queued",
        "scan_pattern_id": 537,
        "asset_class": "stock",
        "window_days": 30,
    }


def test_exit_variant_refresh_builds_report_from_time_decay_edge_misses(
    monkeypatch,
) -> None:
    from app.services.trading.brain_work import ledger
    import app.services.trading.learning as learning

    outcomes: list[dict[str, object]] = []
    captured: dict[str, object] = {}

    eligible_1 = SimpleNamespace(
        id=10,
        scan_pattern_id=537,
        paper_shadow_of_alert_id=88,
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
            "asset_class": "crypto",
            "entry_edge": {"expected_net_pct": 3.0},
            "_paper_meta": {
                "exit_config": {
                    "target_reward_fraction": 0.06,
                    "stop_loss_fraction": 0.02,
                },
            },
        },
    )
    eligible_2 = SimpleNamespace(
        id=11,
        scan_pattern_id=537,
        paper_shadow_of_alert_id=89,
        ticker="EDGE-USD",
        pnl=-1.5,
        pnl_pct=None,
        entry_price=50.0,
        exit_price=49.0,
        quantity=1.0,
        direction="long",
        exit_reason="exit_engine_time_decay",
        signal_json={
            "paper_shadow": True,
            "asset_class": "crypto",
            "entry_edge": {"expected_net_pct": 2.0},
            "_paper_meta": {
                "exit_config": {
                    "target_reward_fraction": 0.04,
                    "stop_loss_fraction": 0.015,
                },
            },
        },
    )
    no_edge = SimpleNamespace(
        id=12,
        scan_pattern_id=537,
        paper_shadow_of_alert_id=90,
        ticker="NOEDGE-USD",
        pnl=-1.0,
        pnl_pct=None,
        entry_price=100.0,
        exit_price=99.0,
        quantity=1.0,
        direction="long",
        exit_reason="exit_engine_time_decay",
        signal_json={"paper_shadow": True},
    )

    class _Query:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def limit(self, _limit):
            return self

        def all(self):
            return [eligible_1, eligible_2, no_edge]

    class _Db:
        def query(self, _model):
            return _Query()

    def _loss_reports(*_args, **_kwargs):
        raise AssertionError("paper time-decay refresh should use the paper-loss report")

    def _forks(_db, pattern_id, *, edge_loss_report, diagnostics):
        captured["report"] = edge_loss_report
        assert pattern_id == 537
        assert edge_loss_report["paper_time_decay_edge_miss"] is True
        assert edge_loss_report["original_source"] == "paper_time_decay_edge_miss"
        assert edge_loss_report["total_rejects"] == 2
        assert edge_loss_report["thin_sample"] is False
        assert edge_loss_report["avg_expected_net_pct"] == 2.5
        diagnostics["variant_label"] = "edge-exit-time-decay"
        return [9010]

    monkeypatch.setattr(learning, "_edge_debt_loss_reports", _loss_reports)
    monkeypatch.setattr(learning, "fork_edge_learned_exit_variants", _forks)

    def _enqueue_outcome_event(_db, **kwargs):
        outcomes.append(kwargs)
        return 1003

    monkeypatch.setattr(ledger, "enqueue_outcome_event", _enqueue_outcome_event)

    ev = SimpleNamespace(
        id=903,
        payload={
            "scan_pattern_id": 537,
            "asset_class": "crypto",
            "source": "paper_time_decay_edge_miss",
            "cash_deployment_category": "positive_ev_time_decay_loss",
            "expected_evidence_value": 4.5,
            "expected_net_pct": 2.0,
            "realized_return_pct": -2.0,
            "pnl": -1.5,
            "paper_trade_id": 11,
            "ticker": "EDGE-USD",
            "exit_reason": "exit_engine_time_decay",
            "evidence_fingerprint": "td-loss-fp",
            "graduation_blocker": "exit_thesis_mismatch",
        },
    )

    handle_exit_variant_refresh(_Db(), ev, user_id=None)

    assert captured["report"]["paper_trade_ids"] == [10, 11]
    assert len(outcomes) == 1
    payload = outcomes[0]["payload"]
    assert payload["created_count"] == 1
    assert payload["created_child_ids"] == [9010]
    assert payload["variant_label"] == "edge-exit-time-decay"
    assert payload["loss_report"]["root_cause"] == "paper_time_decay_exit_thesis_mismatch"


def test_time_decay_exit_variant_min_losses_setting_marks_thin_sample(
    monkeypatch,
) -> None:
    from app.services.trading.brain_work.handlers import profitability

    monkeypatch.setenv("BRAIN_WORK_TIME_DECAY_EXIT_VARIANT_MIN_LOSSES", "3")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr("app.config.settings", settings)

    class _Query:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def limit(self, _limit):
            return self

        def all(self):
            return []

    class _Db:
        def query(self, _model):
            return _Query()

    report = profitability._paper_time_decay_edge_miss_report(
        _Db(),
        pattern_id=537,
        window_days=30,
        payload={
            "source": "paper_time_decay_edge_miss",
            "cash_deployment_category": "positive_ev_time_decay_loss",
            "asset_class": "crypto",
            "expected_net_pct": 2.0,
            "realized_return_pct": -2.0,
            "pnl": -1.5,
            "paper_trade_id": 11,
            "ticker": "EDGE-USD",
            "exit_reason": "exit_engine_time_decay",
            "graduation_blocker": "exit_thesis_mismatch",
        },
    )

    assert settings.brain_work_time_decay_exit_variant_min_losses == 3
    assert report is not None
    assert report["total_rejects"] == 1
    assert report["min_rejects_for_variant"] == 3
    assert report["thin_sample"] is True


def test_exit_variant_refresh_surfaces_duplicate_refresh_diagnostics(
    monkeypatch,
) -> None:
    from app.services.trading.brain_work import ledger
    import app.services.trading.learning as learning

    outcomes: list[dict[str, object]] = []
    report = {
        "source": "autotrader_edge_debt_v1",
        "original_source": "paper_time_decay_edge_miss",
        "paper_time_decay_edge_miss": True,
        "total_rejects": 5,
        "avg_expected_net_pct": 3.1,
    }

    def _loss_reports(_db, *, lookback_days):
        assert lookback_days == 30
        return {537: report}

    def _forks(_db, pattern_id, *, edge_loss_report, diagnostics):
        assert pattern_id == 537
        assert edge_loss_report == report
        diagnostics.update(
            skip_reason="refreshed_duplicate_learned_exit_label",
            variant_label="edge-exit-r3.00-s1.50",
            existing_child_id=456,
            refreshed_existing_child_id=456,
            refreshed_count=1,
        )
        return []

    monkeypatch.setattr(learning, "_edge_debt_loss_reports", _loss_reports)
    monkeypatch.setattr(learning, "fork_edge_learned_exit_variants", _forks)

    def _enqueue_outcome_event(_db, **kwargs):
        outcomes.append(kwargs)
        return 1004

    monkeypatch.setattr(ledger, "enqueue_outcome_event", _enqueue_outcome_event)

    ev = SimpleNamespace(
        id=904,
        payload={
            "scan_pattern_id": 537,
            "asset_class": "crypto",
            "cash_deployment_category": "positive_ev_time_decay_loss",
            "expected_evidence_value": 4.5,
            "evidence_fingerprint": "td-refresh-fp",
            "graduation_blocker": "exit_thesis_mismatch",
        },
    )

    handle_exit_variant_refresh(object(), ev, user_id=None)

    assert len(outcomes) == 1
    assert outcomes[0]["event_type"] == EXIT_VARIANT_DIAGNOSTIC
    assert (
        outcomes[0]["dedupe_key"]
        == "exit_variant_diagnostic:p537:3.1:refreshed_duplicate_learned_exit_label:456"
    )
    payload = outcomes[0]["payload"]
    assert payload["created_count"] == 0
    assert payload["refreshed_count"] == 1
    assert payload["existing_child_id"] == 456
    assert payload["refreshed_existing_child_id"] == 456
    assert payload["variant_diagnostics"]["refreshed_count"] == 1
