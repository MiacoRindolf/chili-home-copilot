from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models.trading import AutoTraderRun, BreakoutAlert, ScanPattern
from app.services.trading.auto_trader_rules import _realized_exit_geometry
from app.services.trading.learning import (
    EDGE_EXIT_CONFIG_SOURCE,
    EDGE_EXIT_PROMOTION_STATUS,
    EDGE_EXIT_VARIANT_ORIGIN,
    _edge_debt_loss_reports,
    _learned_exit_config_from_edge_report,
    _parent_eligible_for_variant_spawn,
    fork_edge_learned_exit_variants,
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


def test_edge_learned_exit_variant_starts_shadow_research_only(db):
    pat = _make_pattern(db)
    now = datetime.utcnow().replace(microsecond=0)
    for i in range(5):
        _add_edge_reject(db, pattern_id=pat.id, created_at=now - timedelta(minutes=i))
    db.commit()
    report = _edge_debt_loss_reports(db, now=now, lookback_days=1)[pat.id]

    ids = fork_edge_learned_exit_variants(db, pat.id, edge_loss_report=report)

    assert len(ids) == 1
    child = db.get(ScanPattern, ids[0])
    assert child is not None
    assert child.parent_id == pat.id
    assert child.origin == EDGE_EXIT_VARIANT_ORIGIN
    assert child.lifecycle_stage == "challenged"
    assert child.promotion_status == EDGE_EXIT_PROMOTION_STATUS
    cfg = child.exit_config if isinstance(child.exit_config, dict) else json.loads(child.exit_config)
    assert cfg["source"] == EDGE_EXIT_CONFIG_SOURCE
    assert cfg["edge_learned_exit_v1"] is True
    assert cfg["target_reward_fraction"] == 0.02211
    assert cfg["stop_loss_fraction"] == 0.0053
    assert cfg["total_edge_rejects"] == 5


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

    ids = fork_edge_learned_exit_variants(
        db,
        pat.id,
        edge_loss_report={"avg_losing_return": -0.8, "loss_count": 12},
    )

    assert ids == []


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
