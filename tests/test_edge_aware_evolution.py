from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.config import Settings
from app.models.trading import AutoTraderRun, BreakoutAlert, ScanPattern
from app.services.trading.auto_trader_rules import _realized_exit_geometry
from app.services.trading.learning import (
    EDGE_EXIT_CONFIG_SOURCE,
    EDGE_EXIT_PROMOTION_STATUS,
    EDGE_EXIT_VARIANT_ORIGIN,
    EDGE_GEOMETRY_REJECT_REASON,
    _edge_debt_loss_reports,
    _edge_report_root_cause,
    _learned_exit_config_from_edge_report,
    _parent_eligible_for_variant_spawn,
    _refresh_duplicate_time_decay_learned_exit_child,
    _request_edge_exit_child_backtest,
    fork_edge_learned_exit_variants,
    fork_entry_variants,
)


def _make_pattern(db, **overrides) -> ScanPattern:
    vals = dict(
        name="Edge Aware Parent",
        rules_json={"conditions": [{"indicator": "rsi_14", "op": ">", "value": 50}]},
        origin="brain",
        active=True,
        lifecycle_stage="pilot_promoted",
        promotion_status="promoted",
        corrected_trade_count=6,
        corrected_win_rate=1.0,
        corrected_avg_return_pct=0.77,
        avg_winner_pct=2.211,
        avg_loser_pct=-0.53,
        payoff_ratio=4.17,
        payoff_ratio_n=117,
    )
    vals.update(overrides)
    pat = ScanPattern(**vals)
    db.add(pat)
    db.flush()
    return pat


def _add_edge_reject(
    db,
    *,
    pattern_id: int,
    ticker: str = "EDGE",
    expected_net_pct: float = -0.01,
    created_at: datetime | None = None,
) -> None:
    now = created_at or datetime.utcnow()
    alert = BreakoutAlert(
        ticker=ticker,
        asset_type="crypto",
        alert_tier="pattern_imminent",
        scan_pattern_id=pattern_id,
        score_at_alert=0.65,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=97.0,
        target_price=108.0,
        indicator_snapshot={
            "imminent_scorecard": {
                "signal_lane": "shadow_near_miss",
                "readiness": 0.42,
                "min_readiness": 0.45,
            }
        },
        alerted_at=now,
    )
    db.add(alert)
    db.flush()
    db.add(
        AutoTraderRun(
            breakout_alert_id=alert.id,
            scan_pattern_id=pattern_id,
            ticker=ticker,
            decision="skipped",
            reason="non_positive_expected_edge",
            rule_snapshot={
                "entry_edge": {
                    "expected_net_pct": expected_net_pct,
                    "reward_fraction": 0.08,
                    "stop_loss_fraction": 0.03,
                    "probability_source": "directional_mfe_mae_pattern",
                    "probability_sample_n": 12,
                    "managed_exit_edge": {
                        "geometry": {"reason": "managed_reward_risk_below_floor"}
                    },
                }
            },
            created_at=now,
        )
    )


def _add_too_wide_execution_reject(
    db,
    *,
    pattern_id: int,
    ticker: str = "AAOX",
    expected_net_pct: float = 19.25,
    created_at: datetime | None = None,
) -> None:
    now = created_at or datetime.utcnow()
    alert = BreakoutAlert(
        ticker=ticker,
        asset_type="stock",
        alert_tier="pattern_imminent",
        scan_pattern_id=pattern_id,
        score_at_alert=0.9,
        price_at_alert=45.5,
        entry_price=45.5,
        stop_loss=10.21,
        target_price=87.85,
        indicator_snapshot={"imminent_scorecard": {"signal_lane": "standard"}},
        alerted_at=now,
    )
    db.add(alert)
    db.flush()
    db.add(
        AutoTraderRun(
            breakout_alert_id=alert.id,
            scan_pattern_id=pattern_id,
            ticker=ticker,
            decision="skipped",
            reason=EDGE_GEOMETRY_REJECT_REASON,
            rule_snapshot={
                "entry_edge": {
                    "expected_net_pct": expected_net_pct,
                    "reward_fraction": 0.93076923,
                    "stop_loss_fraction": 0.7756044,
                    "target_reward_fraction": 0.93076923,
                    "hard_stop_loss_fraction": 0.7756044,
                    "execution_stop_loss_fraction": 0.7756044,
                    "max_execution_stop_loss_fraction": 0.30,
                    "execution_stop_loss_source": "static_target_stop_geometry",
                    "probability_source": "directional_mfe_mae_pattern",
                    "probability_sample_n": 14,
                }
            },
            created_at=now,
        )
    )


def test_edge_learned_exit_parent_query_omits_bulk_scan_pattern_columns(db):
    pat = _make_pattern(db)
    now = datetime.utcnow().replace(microsecond=0)
    for i in range(5):
        _add_edge_reject(db, pattern_id=pat.id, created_at=now - timedelta(minutes=i))
    db.commit()
    pattern_id = int(pat.id)
    report = _edge_debt_loss_reports(db, now=now, lookback_days=1)[pattern_id]
    db.expunge_all()

    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        lowered = statement.lower()
        if (
            lowered.lstrip().startswith("select")
            and "from scan_patterns" in lowered
        ):
            statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", _capture)
    try:
        ids = fork_edge_learned_exit_variants(
            db,
            pattern_id,
            edge_loss_report=report,
        )
    finally:
        event.remove(db.bind, "before_cursor_execute", _capture)

    assert len(ids) == 1
    assert statements
    relevant = [
        statement.lower()
        for statement in statements
        if "limit" in statement.lower()
        or "count(" in statement.lower()
    ]
    assert relevant
    parent_select = next(
        statement
        for statement in relevant
        if "where scan_patterns.id" in statement
    )
    assert "where scan_patterns.id" in parent_select
    assert "rules_json" in parent_select
    assert "avg_winner_pct" in parent_select
    assert any("count(" in statement for statement in relevant)
    assert any("variant_label" in statement for statement in relevant)
    for statement in relevant:
        assert "bench_walk_forward_json" not in statement
        assert "portfolio_gate_json" not in statement
        assert "regime_affinity_json" not in statement


def test_edge_debt_loss_report_groups_autotrader_rejects(db):
    pat = _make_pattern(db)
    now = datetime.utcnow().replace(microsecond=0)
    for i in range(5):
        _add_edge_reject(
            db,
            pattern_id=pat.id,
            ticker=f"EDGE{i}",
            expected_net_pct=-0.01 * (i + 1),
            created_at=now - timedelta(minutes=i),
        )
    db.commit()

    report = _edge_debt_loss_reports(db, now=now, lookback_days=1)[pat.id]

    assert report["total_rejects"] == 5
    assert report["thin_sample"] is False
    assert report["avg_expected_net_pct"] == -0.03
    assert report["avg_static_reward_fraction"] == 0.08
    assert report["avg_static_stop_loss_fraction"] == 0.03
    assert report["signal_lanes"]["shadow_near_miss"] == 5
    assert report["root_cause"] == "shadow_near_miss_noise"


def test_edge_debt_loss_report_includes_positive_ev_unusable_execution_geometry(db):
    pat = _make_pattern(db)
    now = datetime.utcnow().replace(microsecond=0)
    for i in range(5):
        _add_too_wide_execution_reject(
            db,
            pattern_id=pat.id,
            ticker=f"AAOX{i}",
            expected_net_pct=19.25 + i,
            created_at=now - timedelta(minutes=i),
        )
    db.commit()

    report = _edge_debt_loss_reports(db, now=now, lookback_days=1)[pat.id]

    assert report["total_rejects"] == 5
    assert report["thin_sample"] is False
    assert report["reject_reasons"][EDGE_GEOMETRY_REJECT_REASON] == 5
    assert report["avg_expected_net_pct"] == 21.25
    assert report["avg_static_reward_fraction"] == 0.93076923
    assert report["avg_static_stop_loss_fraction"] == 0.7756044
    assert report["root_cause"] == EDGE_GEOMETRY_REJECT_REASON

    ids = fork_edge_learned_exit_variants(db, pat.id, edge_loss_report=report)

    assert len(ids) == 1
    child = db.get(ScanPattern, ids[0])
    assert child is not None
    assert child.parent_id == pat.id
    assert child.lifecycle_stage == "challenged"
    cfg = child.exit_config if isinstance(child.exit_config, dict) else json.loads(child.exit_config)
    assert cfg["source"] == EDGE_EXIT_CONFIG_SOURCE
    assert cfg["avg_rejected_expected_net_pct"] == 21.25
    assert cfg["total_edge_rejects"] == 5
    assert cfg["target_reward_fraction"] == 0.02211
    assert cfg["stop_loss_fraction"] == 0.0053


def test_edge_debt_loss_report_severe_negative_trumps_near_miss_noise(db):
    pat = _make_pattern(db)
    now = datetime.utcnow().replace(microsecond=0)
    for i in range(5):
        _add_edge_reject(
            db,
            pattern_id=pat.id,
            ticker=f"EDGE{i}",
            expected_net_pct=-9.0,
            created_at=now - timedelta(minutes=i),
        )
    db.commit()

    report = _edge_debt_loss_reports(db, now=now, lookback_days=1)[pat.id]

    assert report["signal_lanes"]["shadow_near_miss"] == 5
    assert report["avg_expected_net_pct"] == -9.0
    assert report["root_cause"] == "deep_negative_expected_edge"


def test_edge_root_cause_requires_material_insufficient_directional_evidence():
    report = {
        "total_rejects": 10,
        "avg_expected_net_pct": -0.35,
        "avg_probability_sample_n": 18.0,
        "signal_lanes": {"standard": 10},
        "managed_geometry_reasons": {
            "insufficient_directional_samples": 1,
            "managed_stop_not_tighter_than_base": 6,
            "managed_reward_risk_below_floor": 3,
        },
    }

    assert _edge_report_root_cause(report) == "managed_stop_not_tighter_than_base"


def test_edge_learned_exit_variant_starts_shadow_research_only(db, monkeypatch):
    pat = _make_pattern(db)
    now = datetime.utcnow().replace(microsecond=0)
    for i in range(5):
        _add_edge_reject(db, pattern_id=pat.id, created_at=now - timedelta(minutes=i))
    db.commit()
    report = _edge_debt_loss_reports(db, now=now, lookback_days=1)[pat.id]
    emissions: list[dict] = []

    def _emit(db_arg, scan_pattern_id, *, source, asset_class=None, expected_evidence_value=None, payload=None):
        emissions.append(
            {
                "db": db_arg,
                "scan_pattern_id": scan_pattern_id,
                "source": source,
                "asset_class": asset_class,
                "expected_evidence_value": expected_evidence_value,
                "payload": payload,
            }
        )
        return 9001

    monkeypatch.setattr(
        "app.services.trading.brain_work.emitters.emit_backtest_requested_for_pattern",
        _emit,
    )

    diag: dict = {}
    ids = fork_edge_learned_exit_variants(db, pat.id, edge_loss_report=report, diagnostics=diag)

    assert len(ids) == 1
    assert diag["skip_reason"] is None
    assert diag["created_child_ids"] == ids
    assert diag["created_count"] == 1
    assert diag["backtest_request_event_id"] == 9001
    child = db.get(ScanPattern, ids[0])
    assert child is not None
    assert child.parent_id == pat.id
    assert child.origin == EDGE_EXIT_VARIANT_ORIGIN
    assert child.lifecycle_stage == "challenged"
    assert child.promotion_status == EDGE_EXIT_PROMOTION_STATUS
    assert isinstance(child.exit_config, dict)
    cfg = child.exit_config if isinstance(child.exit_config, dict) else json.loads(child.exit_config)
    assert cfg["source"] == EDGE_EXIT_CONFIG_SOURCE
    assert cfg["edge_learned_exit_v1"] is True
    assert cfg["target_reward_fraction"] == 0.02211
    assert cfg["stop_loss_fraction"] == 0.0053
    assert cfg["total_edge_rejects"] == 5
    assert emissions == [
        {
            "db": db,
            "scan_pattern_id": child.id,
            "source": "edge_learned_exit_variant",
            "asset_class": child.asset_class,
            "expected_evidence_value": 0.0,
            "payload": {
                "parent_pattern_id": pat.id,
                "variant_label": child.variant_label,
                "origin": EDGE_EXIT_VARIANT_ORIGIN,
                "promotion_status": EDGE_EXIT_PROMOTION_STATUS,
                "edge_learned_exit_v1": True,
                "basis": "realized_parent_payoff_after_edge_debt",
                "target_pct": 2.211,
                "stop_pct": 0.53,
                "reward_risk": 4.171698,
                "total_edge_rejects": 5,
                "edge_loss_report_source": EDGE_EXIT_CONFIG_SOURCE,
                "root_cause": "shadow_near_miss_noise",
                "report_total_rejects": 5,
            },
        }
    ]


def test_time_decay_edge_miss_report_uses_paper_geometry_without_parent_payoff():
    pat = SimpleNamespace(
        id=123,
        corrected_trade_count=6,
        corrected_avg_return_pct=0.77,
        trade_count=6,
        avg_winner_pct=None,
        avg_loser_pct=None,
        payoff_ratio=None,
        payoff_ratio_n=0,
    )
    report = {
        "source": EDGE_EXIT_CONFIG_SOURCE,
        "original_source": "paper_time_decay_edge_miss",
        "paper_time_decay_edge_miss": True,
        "thin_sample": False,
        "total_rejects": 3,
        "min_rejects_for_variant": 2,
        "avg_expected_net_pct": 3.1,
        "avg_realized_return_pct": -2.0,
        "total_pnl": -7.5,
        "avg_static_reward_fraction": 0.06,
        "avg_static_stop_loss_fraction": 0.02,
        "root_cause": "paper_time_decay_exit_thesis_mismatch",
        "paper_trade_ids": [101, 102, 103],
    }

    cfg, reason = _learned_exit_config_from_edge_report(pat, report)

    assert reason == "ok"
    assert cfg is not None
    assert cfg["basis"] == "paper_time_decay_shadow_exit_geometry"
    assert cfg["paper_time_decay_edge_miss"] is True
    assert cfg["target_reward_fraction"] == 0.03
    assert cfg["stop_loss_fraction"] == 0.015
    assert cfg["reward_risk"] == 2.0
    assert cfg["sample_n"] == 3
    assert cfg["paper_trade_ids"] == [101, 102, 103]


def test_time_decay_tighten_fraction_setting_drives_learned_geometry(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CHILI_EDGE_EVOLUTION_TIME_DECAY_TIGHTEN_FRACTION", "0.60")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr("app.config.settings", settings)

    pat = SimpleNamespace(
        id=123,
        corrected_trade_count=6,
        corrected_avg_return_pct=0.77,
        trade_count=6,
        avg_winner_pct=None,
        avg_loser_pct=None,
        payoff_ratio=None,
        payoff_ratio_n=0,
    )

    cfg, reason = _learned_exit_config_from_edge_report(
        pat,
        _time_decay_edge_miss_report(),
    )

    assert settings.chili_edge_evolution_time_decay_tighten_fraction == pytest.approx(
        0.60
    )
    assert reason == "ok"
    assert cfg is not None
    assert cfg["basis"] == "paper_time_decay_shadow_exit_geometry"
    assert cfg["target_reward_fraction"] == pytest.approx(0.03)
    assert cfg["stop_loss_fraction"] == pytest.approx(0.012)
    assert cfg["reward_risk"] == pytest.approx(2.5)
    assert cfg["tighten_fraction"] == pytest.approx(0.60)


def _time_decay_edge_miss_report(**overrides) -> dict:
    report = {
        "source": EDGE_EXIT_CONFIG_SOURCE,
        "original_source": "paper_time_decay_edge_miss",
        "paper_time_decay_edge_miss": True,
        "thin_sample": False,
        "total_rejects": 3,
        "min_rejects_for_variant": 2,
        "avg_expected_net_pct": 3.1,
        "avg_realized_return_pct": -2.0,
        "avg_static_reward_fraction": 0.06,
        "avg_static_stop_loss_fraction": 0.02,
        "root_cause": "paper_time_decay_exit_thesis_mismatch",
    }
    report.update(overrides)
    return report


def test_edge_learned_exit_child_name_fits_scan_pattern_limit(db):
    parent_name = ("Long edge pattern " * 7).strip()[:110]
    assert len(parent_name) <= 120

    pat = _make_pattern(
        db,
        name=parent_name,
    )
    now = datetime.utcnow().replace(microsecond=0)
    for i in range(5):
        _add_edge_reject(db, pattern_id=pat.id, created_at=now - timedelta(minutes=i))
    db.commit()
    report = _edge_debt_loss_reports(db, now=now, lookback_days=1)[pat.id]

    ids = fork_edge_learned_exit_variants(db, pat.id, edge_loss_report=report)

    assert len(ids) == 1
    child = db.get(ScanPattern, ids[0])
    assert child is not None
    assert len(child.name) <= 120
    assert child.name.endswith(f" [{child.variant_label}]")


def test_edge_learned_exit_allows_mild_negative_with_strong_payoff(db):
    pat = _make_pattern(db)
    now = datetime.utcnow().replace(microsecond=0)
    for i in range(5):
        _add_edge_reject(
            db,
            pattern_id=pat.id,
            expected_net_pct=-0.35,
            created_at=now - timedelta(minutes=i),
        )
    db.commit()
    report = _edge_debt_loss_reports(db, now=now, lookback_days=1)[pat.id]

    ids = fork_edge_learned_exit_variants(db, pat.id, edge_loss_report=report)

    assert len(ids) == 1
    child = db.get(ScanPattern, ids[0])
    assert isinstance(child.exit_config, dict)
    cfg = child.exit_config if isinstance(child.exit_config, dict) else json.loads(child.exit_config)
    assert child.lifecycle_stage == "challenged"
    assert cfg["payoff_rescue_used"] is True
    assert cfg["avg_rejected_expected_net_pct"] == -0.35
    assert cfg["reward_risk"] == 4.171698


def test_edge_learned_exit_blocks_payoff_rescue_on_insufficient_directional_evidence(db):
    pat = _make_pattern(db)
    report = {
        "source": EDGE_EXIT_CONFIG_SOURCE,
        "thin_sample": False,
        "total_rejects": 20,
        "avg_expected_net_pct": -0.35,
        "avg_static_reward_fraction": 0.08,
        "avg_static_stop_loss_fraction": 0.03,
        "root_cause": "insufficient_directional_evidence",
    }

    cfg, reason = _learned_exit_config_from_edge_report(pat, report)

    assert cfg is None
    assert reason == "edge_debt_too_negative_for_exit_child:-0.350"


def test_edge_learned_exit_ignores_legacy_loss_report(db):
    pat = _make_pattern(db)
    db.commit()

    diag: dict = {}
    ids = fork_edge_learned_exit_variants(
        db,
        pat.id,
        edge_loss_report={"avg_losing_return": -0.8, "loss_count": 12},
        diagnostics=diag,
    )

    assert ids == []
    assert diag["skip_reason"] == "missing_edge_debt_report"


def test_entry_variant_accepts_jsonb_rules_and_stores_child_jsonb(db):
    pat = _make_pattern(
        db,
        rules_json={
            "conditions": [
                {"indicator": "rsi_14", "op": ">", "value": 50},
                {"indicator": "adx", "op": ">", "value": 20},
            ]
        },
    )
    db.commit()

    ids = fork_entry_variants(db, pat.id, max_variants=1)

    assert len(ids) == 1
    child = db.get(ScanPattern, ids[0])
    assert child is not None
    assert isinstance(child.rules_json, dict)
    assert child.rules_json["conditions"]


def test_edge_spawn_gate_blocks_deep_negative_parent():
    parent = SimpleNamespace(
        lifecycle_stage="pilot_promoted",
        promotion_status="promoted",
        backtest_count=0,
        win_rate=None,
        corrected_trade_count=6,
        corrected_avg_return_pct=-1.28,
        corrected_win_rate=0.0,
    )
    report = {
        "source": EDGE_EXIT_CONFIG_SOURCE,
        "thin_sample": False,
        "total_rejects": 25,
        "avg_expected_net_pct": -9.19,
    }

    ok, reason = _parent_eligible_for_variant_spawn(parent, edge_loss_report=report)

    assert ok is False
    assert reason.startswith("edge_debt_deep_negative")


def test_edge_spawn_gate_blocks_thin_directional_evidence_parent():
    parent = SimpleNamespace(
        lifecycle_stage="pilot_promoted",
        promotion_status="promoted",
        backtest_count=0,
        win_rate=None,
        corrected_trade_count=6,
        corrected_avg_return_pct=0.77,
        corrected_win_rate=1.0,
    )
    report = {
        "source": EDGE_EXIT_CONFIG_SOURCE,
        "thin_sample": False,
        "total_rejects": 30,
        "avg_expected_net_pct": -0.35,
        "avg_probability_sample_n": 2.0,
        "root_cause": "insufficient_directional_evidence",
    }

    ok, reason = _parent_eligible_for_variant_spawn(parent, edge_loss_report=report)

    assert ok is False
    assert reason == "edge_debt_insufficient_directional_evidence:avg_sample_n=2.000"


def test_edge_spawn_gate_ignores_legacy_loss_report():
    parent = SimpleNamespace(
        lifecycle_stage="pilot_promoted",
        promotion_status="promoted",
        backtest_count=0,
        win_rate=None,
        corrected_trade_count=6,
        corrected_avg_return_pct=-1.28,
        corrected_win_rate=0.0,
    )

    ok, reason = _parent_eligible_for_variant_spawn(
        parent,
        edge_loss_report={"avg_losing_return": -1.2, "loss_count": 12},
    )

    assert ok is True
    assert reason == ""


def test_edge_spawn_gate_allows_challenged_time_decay_shadow_repair():
    parent = SimpleNamespace(
        lifecycle_stage="challenged",
        promotion_status="promoted",
        backtest_count=80,
        win_rate=0.05,
        corrected_trade_count=12,
        corrected_avg_return_pct=-1.0,
        corrected_win_rate=0.0,
    )

    ok, reason = _parent_eligible_for_variant_spawn(
        parent,
        edge_loss_report=_time_decay_edge_miss_report(),
    )

    assert ok is True
    assert reason == ""


def test_edge_spawn_gate_keeps_hard_blocks_for_time_decay_reports():
    retired_parent = SimpleNamespace(
        lifecycle_stage="retired",
        promotion_status="promoted",
        backtest_count=0,
        win_rate=None,
    )
    ok, reason = _parent_eligible_for_variant_spawn(
        retired_parent,
        edge_loss_report=_time_decay_edge_miss_report(),
    )
    assert ok is False
    assert reason == "parent_lifecycle_blocked:retired"

    demoted_parent = SimpleNamespace(
        lifecycle_stage="challenged",
        promotion_status="demoted_evidence_gap",
        backtest_count=0,
        win_rate=None,
    )
    ok, reason = _parent_eligible_for_variant_spawn(
        demoted_parent,
        edge_loss_report=_time_decay_edge_miss_report(),
    )
    assert ok is False
    assert reason == "parent_lifecycle_blocked:challenged"

    weak_report_parent = SimpleNamespace(
        lifecycle_stage="challenged",
        promotion_status="promoted",
        backtest_count=0,
        win_rate=None,
    )
    ok, reason = _parent_eligible_for_variant_spawn(
        weak_report_parent,
        edge_loss_report=_time_decay_edge_miss_report(avg_expected_net_pct=0.0),
    )
    assert ok is False
    assert reason == "parent_lifecycle_blocked:challenged"


def test_duplicate_time_decay_learned_exit_child_refreshes_existing_shadow_child():
    parent = SimpleNamespace(id=123, backtest_priority=42)
    existing = SimpleNamespace(
        id=456,
        parent_id=123,
        origin=EDGE_EXIT_VARIANT_ORIGIN,
        variant_label="edge-exit-r3.00-s1.50",
        exit_config={
            "source": EDGE_EXIT_CONFIG_SOURCE,
            "edge_learned_exit_v1": True,
            "total_edge_rejects": 2,
            "paper_trade_ids": [1, 2],
        },
        backtest_priority=10,
        last_backtest_at=object(),
    )
    cfg = {
        "source": EDGE_EXIT_CONFIG_SOURCE,
        "edge_learned_exit_v1": True,
        "paper_time_decay_edge_miss": True,
        "target_reward_fraction": 0.03,
        "stop_loss_fraction": 0.015,
        "total_edge_rejects": 5,
        "paper_trade_ids": [3, 4, 5],
    }

    refreshed = _refresh_duplicate_time_decay_learned_exit_child(
        existing,
        parent,
        cfg=cfg,
        report=_time_decay_edge_miss_report(total_rejects=5),
    )

    assert refreshed is True
    assert existing.backtest_priority == 75
    assert existing.last_backtest_at is None
    assert existing.exit_config["refreshed_duplicate_label"] is True
    assert existing.exit_config["previous_total_edge_rejects"] == 2
    assert existing.exit_config["previous_paper_trade_ids"] == [1, 2]
    assert existing.exit_config["total_edge_rejects"] == 5
    assert existing.exit_config["paper_trade_ids"] == [3, 4, 5]


def test_duplicate_time_decay_refresh_bypasses_max_active_child_cap(monkeypatch):
    parent = SimpleNamespace(
        id=123,
        active=True,
        backtest_priority=42,
        avg_winner_pct=None,
        avg_loser_pct=None,
        corrected_trade_count=6,
        trade_count=6,
    )
    existing = SimpleNamespace(
        id=456,
        parent_id=123,
        origin=EDGE_EXIT_VARIANT_ORIGIN,
        variant_label="edge-exit-r3.00-s1.50",
        exit_config={
            "source": EDGE_EXIT_CONFIG_SOURCE,
            "edge_learned_exit_v1": True,
            "total_edge_rejects": 2,
            "paper_trade_ids": [1, 2],
        },
        backtest_priority=10,
        last_backtest_at=object(),
    )
    commits: list[bool] = []
    emissions: list[dict] = []

    def _emit(db_arg, scan_pattern_id, *, source, asset_class=None, expected_evidence_value=None, payload=None):
        emissions.append(
            {
                "scan_pattern_id": scan_pattern_id,
                "source": source,
                "asset_class": asset_class,
                "expected_evidence_value": expected_evidence_value,
                "payload": payload,
            }
        )
        return 9010

    monkeypatch.setattr(
        "app.services.trading.brain_work.emitters.emit_backtest_requested_for_pattern",
        _emit,
    )

    class _Query:
        def __init__(self, db):
            self._db = db

        def options(self, *_args):
            return self

        def filter(self, *_args):
            return self

        def first(self):
            return self._db.first_results.pop(0)

        def count(self):
            raise AssertionError("duplicate refresh should run before max-child cap")

    class _Db:
        def __init__(self):
            self.first_results = [parent, existing]

        def get(self, _model, _id):
            return parent

        def query(self, _model):
            return _Query(self)

        def commit(self):
            commits.append(True)

    diag: dict = {}

    ids = fork_edge_learned_exit_variants(
        _Db(),
        parent.id,
        edge_loss_report=_time_decay_edge_miss_report(total_rejects=5),
        diagnostics=diag,
    )

    assert ids == []
    assert commits == [True]
    assert diag["skip_reason"] == "refreshed_duplicate_learned_exit_label"
    assert diag["existing_child_id"] == 456
    assert diag["backtest_request_event_id"] == 9010
    assert existing.backtest_priority == 75
    assert existing.last_backtest_at is None
    assert existing.exit_config["refreshed_duplicate_label"] is True
    assert emissions == [
        {
            "scan_pattern_id": 456,
            "source": "edge_learned_exit_variant_refresh",
            "asset_class": None,
            "expected_evidence_value": 3.1,
            "payload": {
                "parent_pattern_id": 123,
                "variant_label": "edge-exit-r3.00-s1.50",
                "origin": EDGE_EXIT_VARIANT_ORIGIN,
                "edge_learned_exit_v1": True,
                "basis": "paper_time_decay_shadow_exit_geometry",
                "target_pct": 3.0,
                "stop_pct": 1.5,
                "reward_risk": 2.0,
                "total_edge_rejects": 5,
                "paper_time_decay_edge_miss": True,
                "total_time_decay_losses": 5,
                "edge_loss_report_source": EDGE_EXIT_CONFIG_SOURCE,
                "root_cause": "paper_time_decay_exit_thesis_mismatch",
                "report_total_rejects": 5,
            },
        }
    ]


def test_request_edge_exit_child_backtest_swallows_ledger_failures(monkeypatch):
    def _emit(*_args, **_kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(
        "app.services.trading.brain_work.emitters.emit_backtest_requested_for_pattern",
        _emit,
    )

    event_id = _request_edge_exit_child_backtest(
        object(),
        SimpleNamespace(
            id=456,
            parent_id=123,
            variant_label="edge-exit-r3.00-s1.50",
            origin=EDGE_EXIT_VARIANT_ORIGIN,
            promotion_status=EDGE_EXIT_PROMOTION_STATUS,
            asset_class="crypto",
        ),
        source="edge_learned_exit_variant",
        cfg={"target_pct": 3.0, "stop_pct": 1.5},
        report=_time_decay_edge_miss_report(total_rejects=5),
    )

    assert event_id is None


def test_realized_exit_geometry_prefers_edge_learned_exit_config():
    pattern = SimpleNamespace(
        exit_config={
            "source": EDGE_EXIT_CONFIG_SOURCE,
            "edge_learned_exit_v1": True,
            "target_reward_fraction": 0.02211,
            "stop_loss_fraction": 0.0053,
            "sample_n": 6,
            "total_edge_rejects": 5,
        },
        avg_winner_pct=None,
        avg_loser_pct=None,
    )

    reward, loss, snap = _realized_exit_geometry(
        pattern=pattern,
        static_reward=0.08,
        static_loss=0.03,
        settings=SimpleNamespace(),
    )

    assert reward == 0.02211
    assert loss == 0.0053
    assert snap["used"] is True
    assert snap["reason"] == "scan_pattern_edge_learned_exit_config"
    assert snap["source"] == EDGE_EXIT_CONFIG_SOURCE
