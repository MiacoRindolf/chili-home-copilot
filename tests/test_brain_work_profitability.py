"""Profitability brain-work handler regressions."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.brain_work.handlers.profitability import (
    handle_edge_reliability_refresh,
    handle_exit_variant_refresh,
)
from app.services.trading.edge_reliability import (
    EXIT_VARIANT_DIAGNOSTIC,
    RECERT_RESCUE_REFRESH,
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


def test_edge_reliability_refresh_skips_recent_blocked_recert_rescue(monkeypatch) -> None:
    import app.services.trading.brain_work.handlers.profitability as prof_mod
    import app.services.trading.edge_reliability as edge_mod

    calls: list[dict[str, object]] = []

    def _persist(_db, pattern_id, **kwargs):
        assert pattern_id == 537
        return {
            "recommended_work_event": RECERT_RESCUE_REFRESH,
            "snapshot_event_id": 7001,
            "slice_asset_class": "crypto",
            "graduation_blocker": "recert_blocked",
            "calibrated_ev_pct": 2.0,
            "edge_eval_count": 9,
            "closed_evidence_count": 6,
            "evidence_fingerprint": "edge-fp",
        }

    def _emit(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("blocked recert diagnostic should suppress edge snapshot requeue")

    monkeypatch.setattr(edge_mod, "persist_edge_reliability_snapshot", _persist)
    monkeypatch.setattr(edge_mod, "emit_targeted_profitability_work", _emit)
    monkeypatch.setattr(
        prof_mod,
        "_recent_blocked_recert_rescue_diagnostic",
        lambda _db, *, scan_pattern_id: scan_pattern_id == 537,
    )

    ev = SimpleNamespace(id=903, payload={"scan_pattern_id": 537, "window_days": 30})

    handle_edge_reliability_refresh(object(), ev, user_id=None)

    assert calls == []


def test_edge_reliability_refresh_emits_recert_rescue_without_blocker(monkeypatch) -> None:
    import app.services.trading.brain_work.handlers.profitability as prof_mod
    import app.services.trading.edge_reliability as edge_mod

    calls: list[dict[str, object]] = []

    def _persist(_db, pattern_id, **kwargs):
        assert pattern_id == 537
        return {
            "recommended_work_event": RECERT_RESCUE_REFRESH,
            "snapshot_event_id": 7002,
            "slice_asset_class": "crypto",
            "graduation_blocker": "recert_blocked",
            "calibrated_ev_pct": 2.0,
            "edge_eval_count": 9,
            "closed_evidence_count": 6,
            "evidence_fingerprint": "edge-fp",
        }

    def _emit(_db, **kwargs):
        calls.append(kwargs)
        return 8001

    monkeypatch.setattr(edge_mod, "persist_edge_reliability_snapshot", _persist)
    monkeypatch.setattr(edge_mod, "emit_targeted_profitability_work", _emit)
    monkeypatch.setattr(
        prof_mod,
        "_recent_blocked_recert_rescue_diagnostic",
        lambda _db, *, scan_pattern_id: False,
    )

    ev = SimpleNamespace(id=904, payload={"scan_pattern_id": 537, "window_days": 30})

    handle_edge_reliability_refresh(object(), ev, user_id=None)

    assert len(calls) == 1
    assert calls[0]["event_type"] == RECERT_RESCUE_REFRESH
    assert calls[0]["scan_pattern_id"] == 537
    assert calls[0]["source"] == "edge_reliability_snapshot"
