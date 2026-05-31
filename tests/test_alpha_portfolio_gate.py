from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from app.models.trading import ScanPattern
from app.services.trading.alpha_portfolio_gate import (
    AlphaPortfolioConfig,
    _candidate_floor_blocks,
    _realized_edge_fraction,
    broker_risk_probation_allows_live,
    candidate_base_score,
    infer_alpha_sleeve,
    pilot_bootstrap_recert_allows_live,
    persist_alpha_portfolio_snapshot,
    portfolio_gate_score,
    queue_recert_for_required,
    recert_reasons_for_pattern,
    run_alpha_portfolio_maintenance,
    scan_alpha_portfolio,
)
from tests.test_pattern_cohort_promote import _make_pattern, _truncate_phase4_state

STRONG_CPCV_SHARPE = 1.4
STRONG_REALIZED_TRADE_COUNT = 87
STRONG_REALIZED_AVG_RETURN_PCT = 1.56
PROBATION_MIN_CPCV_SHARPE = 1.0
PROBATION_MIN_REALIZED_TRADES = 5
WEAK_CPCV_DIVISOR = 2.0
PRIORITY_RECERT_PATTERN_IDS = "585"


def _settings(**overrides):
    base = dict(
        chili_cohort_score_realized_window_days=90,
        chili_alpha_portfolio_recert_stale_days=30,
        chili_alpha_portfolio_min_realized_trades=5,
        chili_alpha_portfolio_min_risk_sleeves=3,
        chili_alpha_portfolio_min_shadow_score=0.40,
        chili_alpha_portfolio_max_shadow_total=4,
        chili_alpha_portfolio_max_shadow_per_sleeve=1,
        chili_alpha_portfolio_execution_lookback_days=30,
        chili_alpha_portfolio_execution_min_samples=10,
        chili_alpha_portfolio_execution_max_p90_slippage_pct=0.75,
        chili_alpha_portfolio_gate_enabled=True,
        chili_alpha_portfolio_maintenance_enabled=True,
        chili_alpha_portfolio_auto_queue_recert_enabled=True,
        chili_alpha_portfolio_auto_stage_shadow_enabled=True,
        chili_alpha_portfolio_sync_realized_on_maintenance=True,
        chili_alpha_portfolio_refresh_quality_on_maintenance=True,
        brain_recert_queue_mode="shadow",
        brain_recert_queue_priority_pattern_ids=PRIORITY_RECERT_PATTERN_IDS,
        chili_pilot_promoted_allow_bootstrap_recert_live=True,
        chili_autotrader_probation_live_enabled=True,
        chili_autotrader_probation_min_cpcv_sharpe=PROBATION_MIN_CPCV_SHARPE,
        chili_autotrader_probation_min_realized_trades=PROBATION_MIN_REALIZED_TRADES,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_infer_alpha_sleeve_uses_asset_timeframe_and_setup_text():
    crypto_reversal = SimpleNamespace(
        name="Crypto RSI<35 + MACD histogram positive - reversal [1m]",
        asset_class="all",
        timeframe="1m",
        hypothesis_family=None,
        rules_json={},
    )
    short_setup = SimpleNamespace(
        name="RSI overbought (>70) + MACD histogram negative - sell signal [1m]",
        asset_class="all",
        timeframe="1m",
        hypothesis_family=None,
        rules_json={},
    )
    squeeze = SimpleNamespace(
        name="Intraday Squeeze + Declining Volume [guided-drop-bb_squeeze]",
        asset_class="all",
        timeframe="1d",
        hypothesis_family="compression_expansion",
        rules_json={},
    )

    assert infer_alpha_sleeve(crypto_reversal) == "crypto_intraday_reversal"
    assert infer_alpha_sleeve(short_setup) == "short_overbought"
    assert infer_alpha_sleeve(squeeze) == "volatility_breakout"


def test_recert_reasons_flag_promoted_pattern_585_shape():
    pattern = SimpleNamespace(
        id=585,
        lifecycle_stage="promoted",
        promotion_status="promoted",
        promotion_gate_passed=True,
        oos_evaluated_at=None,
        quality_composite_score=0.80,
        raw_realized_trade_count=87,
        raw_realized_avg_return_pct=1.56,
        payoff_ratio=4.5,
        payoff_ratio_n=87,
    )

    reasons = recert_reasons_for_pattern(
        pattern,
        now=datetime(2026, 5, 21),
        config=AlphaPortfolioConfig(recert_stale_days=30),
    )

    assert reasons == ["missing_oos_recert"]


def test_recert_reasons_keep_negative_oos_pattern_blocked():
    pattern = SimpleNamespace(
        lifecycle_stage="promoted",
        promotion_status="promoted",
        promotion_gate_passed=True,
        oos_evaluated_at=datetime(2026, 5, 25),
        oos_trade_count=47,
        oos_win_rate=0.4468,
        oos_avg_return_pct=-0.386,
        quality_composite_score=0.80,
        raw_realized_trade_count=87,
        raw_realized_avg_return_pct=1.56,
        payoff_ratio=4.5,
        payoff_ratio_n=87,
    )

    reasons = recert_reasons_for_pattern(
        pattern,
        now=datetime(2026, 5, 25),
        config=AlphaPortfolioConfig(min_oos_trades=5),
    )

    assert reasons == ["negative_oos_recert"]


def test_recert_reasons_flag_thin_oos_sample():
    pattern = SimpleNamespace(
        lifecycle_stage="promoted",
        promotion_status="promoted",
        promotion_gate_passed=True,
        oos_evaluated_at=datetime(2026, 5, 25),
        oos_trade_count=2,
        oos_win_rate=1.0,
        oos_avg_return_pct=3.0,
        quality_composite_score=0.80,
        raw_realized_trade_count=12,
        raw_realized_avg_return_pct=1.0,
    )

    reasons = recert_reasons_for_pattern(
        pattern,
        now=datetime(2026, 5, 25),
        config=AlphaPortfolioConfig(min_oos_trades=5),
    )

    assert reasons == ["thin_oos_recert"]


def test_pilot_bootstrap_recert_allows_live_only_for_soft_cert_debt():
    soft_pilot = SimpleNamespace(
        lifecycle_stage="pilot_promoted",
        recert_required=True,
        recert_reason="missing_oos_recert,missing_quality_composite_score,thin_realized_ev",
    )
    hard_pilot = SimpleNamespace(
        lifecycle_stage="pilot_promoted",
        recert_required=True,
        recert_reason="missing_oos_recert,negative_realized_ev",
    )
    full_risk = SimpleNamespace(
        lifecycle_stage="promoted",
        recert_required=True,
        recert_reason="missing_oos_recert",
    )

    assert pilot_bootstrap_recert_allows_live(soft_pilot, settings_=_settings())
    assert not pilot_bootstrap_recert_allows_live(hard_pilot, settings_=_settings())
    assert not pilot_bootstrap_recert_allows_live(full_risk, settings_=_settings())


def test_broker_risk_probation_allows_only_strong_promoted_oos_debt():
    strong_promoted = SimpleNamespace(
        lifecycle_stage="promoted",
        recert_required=True,
        recert_reason="missing_oos_recert",
        promotion_gate_passed=True,
        cpcv_median_sharpe=STRONG_CPCV_SHARPE,
        raw_realized_trade_count=STRONG_REALIZED_TRADE_COUNT,
        raw_realized_avg_return_pct=STRONG_REALIZED_AVG_RETURN_PCT,
    )
    hard_debt = SimpleNamespace(
        **{
            **strong_promoted.__dict__,
            "recert_reason": "missing_oos_recert,negative_realized_ev",
        }
    )
    weak_cpcv = SimpleNamespace(
        **{
            **strong_promoted.__dict__,
            "cpcv_median_sharpe": PROBATION_MIN_CPCV_SHARPE / WEAK_CPCV_DIVISOR,
        }
    )

    assert broker_risk_probation_allows_live(strong_promoted, settings_=_settings())
    assert not broker_risk_probation_allows_live(hard_debt, settings_=_settings())
    assert not broker_risk_probation_allows_live(weak_cpcv, settings_=_settings())


def test_broker_risk_probation_rejects_boolean_numeric_evidence():
    bogus_boolean_evidence = SimpleNamespace(
        lifecycle_stage="promoted",
        recert_required=True,
        recert_reason="missing_oos_recert",
        promotion_gate_passed=True,
        cpcv_median_sharpe=True,
        raw_realized_trade_count=True,
        raw_realized_avg_return_pct=True,
    )

    assert not broker_risk_probation_allows_live(
        bogus_boolean_evidence,
        settings_=_settings(
            chili_autotrader_probation_min_cpcv_sharpe=0.5,
            chili_autotrader_probation_min_realized_trades=1,
        ),
    )


def test_queue_recert_dry_run_skips_non_backtest_solvable_debt():
    out = queue_recert_for_required(
        None,
        {
            "recert_required_patterns": [
                {
                    "scan_pattern_id": 1,
                    "reasons": ["thin_realized_ev", "missing_quality_composite_score"],
                },
                {
                    "scan_pattern_id": 2,
                    "reasons": ["stale_oos_recert", "thin_realized_ev"],
                },
            ]
        },
        execute=False,
    )

    assert out["recert_planned"] == [2]
    assert out["recert_skipped"][0]["scan_pattern_id"] == 1
    assert out["recert_skipped"][0]["skip_reason"] == "no_backtest_solvable_recert_reason"


def test_alpha_portfolio_maintenance_dry_run_skips_quality_write(monkeypatch):
    import app.services.trading.alpha_portfolio_gate as apg

    monkeypatch.setattr(
        "app.services.trading.pattern_quality_score.compute_and_persist_scores",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not write")),
    )
    monkeypatch.setattr(
        apg,
        "scan_alpha_portfolio",
        lambda *_args, **_kwargs: {
            "run_id": "dry",
            "generated_at": datetime(2026, 5, 25),
            "active_pattern_count": 0,
            "recert_required_count": 0,
            "shadow_candidates": [],
            "full_promotion_blocked": False,
            "full_promotion_block_reasons": [],
        },
    )
    monkeypatch.setattr(
        apg,
        "persist_alpha_portfolio_snapshot",
        lambda *_args, **_kwargs: {"ok": True, "dry_run": True},
    )
    monkeypatch.setattr(
        apg,
        "queue_recert_for_required",
        lambda *_args, **_kwargs: {"ok": True, "dry_run": True},
    )
    monkeypatch.setattr(
        apg,
        "stage_shadow_candidates",
        lambda *_args, **_kwargs: {"ok": True, "dry_run": True},
    )

    out = run_alpha_portfolio_maintenance(
        None,
        settings_=_settings(),
        now=datetime(2026, 5, 25),
        execute=False,
    )

    assert out["dry_run"] is True
    assert out["realized_sync"]["skipped"] == "dry_run_write_free"
    assert out["quality_refresh"]["skipped"] == "dry_run_write_free"


def test_alpha_portfolio_maintenance_syncs_realized_stats_before_scan(monkeypatch):
    import app.services.trading.alpha_portfolio_gate as apg

    calls = []

    monkeypatch.setattr(
        "app.services.trading.realized_stats_sync.sync_realized_stats",
        lambda *_args, **_kwargs: calls.append("realized_sync") or {"updated": 1},
    )
    monkeypatch.setattr(
        apg,
        "scan_alpha_portfolio",
        lambda *_args, **_kwargs: calls.append("scan") or {
            "run_id": "sync-first",
            "generated_at": datetime(2026, 5, 25),
            "active_pattern_count": 0,
            "recert_required_count": 0,
            "shadow_candidates": [],
            "full_promotion_blocked": False,
            "full_promotion_block_reasons": [],
        },
    )
    monkeypatch.setattr(
        apg,
        "persist_alpha_portfolio_snapshot",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        apg,
        "queue_recert_for_required",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        apg,
        "stage_shadow_candidates",
        lambda *_args, **_kwargs: {"ok": True},
    )

    out = run_alpha_portfolio_maintenance(
        None,
        settings_=_settings(chili_alpha_portfolio_refresh_quality_on_maintenance=False),
        now=datetime(2026, 5, 25),
        execute=True,
    )

    assert out["realized_sync"]["updated"] == 1
    assert calls[:2] == ["realized_sync", "scan"]


def test_portfolio_score_rewards_uncrowded_sleeve():
    row = {
        "alpha_sleeve": "short_overbought",
        "promotion_gate_passed": True,
        "quality_composite_score": 0.70,
        "cpcv_median_sharpe": 2.0,
        "deflated_sharpe": 1.0,
        "pbo": 0.0,
        "realized_n_trades": 10,
        "realized_avg_pnl_pct": 0.01,
        "payoff_ratio": 2.0,
        "payoff_ratio_n": 10,
    }

    crowded = portfolio_gate_score(
        row,
        broker_risk_by_sleeve={"short_overbought": 3},
        shadow_by_sleeve={},
    )
    uncrowded = portfolio_gate_score(
        row,
        broker_risk_by_sleeve={},
        shadow_by_sleeve={},
    )

    assert uncrowded["portfolio_score"] > crowded["portfolio_score"]
    assert candidate_base_score(row)["score"] is not None


def test_candidate_base_score_rejects_boolean_numeric_evidence():
    row = {
        "alpha_sleeve": "short_overbought",
        "promotion_gate_passed": True,
        "quality_composite_score": True,
        "cpcv_median_sharpe": True,
        "deflated_sharpe": True,
        "pbo": False,
        "realized_n_trades": True,
        "realized_avg_pnl_pct": True,
        "payoff_ratio": True,
        "payoff_ratio_n": True,
    }

    out = candidate_base_score(
        row,
        config=AlphaPortfolioConfig(min_realized_trades=1),
    )

    assert out["score"] is None
    assert out["components"] == {}


def test_candidate_floor_blocks_boolean_gate_metrics_as_missing():
    row = {
        "promotion_gate_passed": True,
        "cpcv_median_sharpe": True,
        "deflated_sharpe": True,
        "pbo": False,
        "realized_n_trades": 5,
        "realized_avg_pnl_pct": 0.01,
    }

    reasons = _candidate_floor_blocks(
        row,
        AlphaPortfolioConfig(min_realized_trades=5),
    )

    assert "missing_cpcv_median_sharpe" in reasons
    assert "missing_deflated_sharpe" in reasons
    assert "missing_pbo" in reasons


def test_candidate_floor_blocks_negative_raw_realized_ev():
    row = {
        "promotion_gate_passed": True,
        "cpcv_median_sharpe": 2.0,
        "deflated_sharpe": 1.0,
        "pbo": 0.0,
        "realized_n_trades": 0,
        "realized_avg_pnl_pct": None,
        "raw_realized_trade_count": 12,
        "raw_realized_avg_return_pct": -1.2,
    }

    reasons = _candidate_floor_blocks(
        row,
        AlphaPortfolioConfig(min_realized_trades=5),
    )

    assert "negative_realized_floor" in reasons


def test_realized_edge_prefers_broader_raw_paper_dynamic_sample():
    row = {
        "realized_n_trades": 1,
        "realized_avg_pnl_pct": 0.05,
        "raw_realized_trade_count": 12,
        "raw_realized_avg_return_pct": -1.2,
    }

    edge, n = _realized_edge_fraction(row)

    assert edge is not None
    assert abs(edge - (-0.012)) < 1e-12
    assert n == 12


def test_candidate_floor_uses_negative_broader_paper_dynamic_ev():
    row = {
        "promotion_gate_passed": True,
        "cpcv_median_sharpe": 2.0,
        "deflated_sharpe": 1.0,
        "pbo": 0.0,
        "realized_n_trades": 1,
        "realized_avg_pnl_pct": 0.05,
        "raw_realized_trade_count": 12,
        "raw_realized_avg_return_pct": -1.2,
    }

    reasons = _candidate_floor_blocks(
        row,
        AlphaPortfolioConfig(min_realized_trades=5),
    )

    assert "negative_realized_floor" in reasons


def test_candidate_floor_preserves_valid_zero_as_nonpositive_ev():
    row = {
        "promotion_gate_passed": True,
        "cpcv_median_sharpe": 2.0,
        "deflated_sharpe": 1.0,
        "pbo": 0.0,
        "realized_n_trades": 5,
        "realized_avg_pnl_pct": 0.0,
        "raw_realized_trade_count": 12,
        "raw_realized_avg_return_pct": 3.0,
    }

    reasons = _candidate_floor_blocks(
        row,
        AlphaPortfolioConfig(min_realized_trades=5),
    )

    assert "negative_realized_floor" in reasons


def test_scan_alpha_portfolio_marks_recert_and_selects_sleeve_candidates(db):
    _truncate_phase4_state(db)
    promoted = ScanPattern(
        id=585,
        name="Intraday Squeeze + Declining Volume [guided-drop-bb_squeeze]",
        rules_json={"hold_hours": 24},
        origin="brain",
        asset_class="all",
        timeframe="1d",
        confidence=0.8,
        evidence_count=30,
        active=True,
        promotion_status="promoted",
        lifecycle_stage="promoted",
        cpcv_n_paths=35,
        cpcv_median_sharpe=1.4,
        deflated_sharpe=1.0,
        pbo=0.0,
        n_effective_trials=10,
        promotion_gate_passed=True,
        quality_composite_score=0.8,
        raw_realized_trade_count=87,
        raw_realized_avg_return_pct=1.5,
        payoff_ratio=4.5,
        payoff_ratio_n=87,
    )
    db.add(promoted)

    candidate_a = _make_pattern(
        db,
        name="RSI overbought (>70) + MACD histogram negative - sell signal [1m]",
        lifecycle="candidate",
        cpcv=2.8,
        quality_score=None,
    )
    candidate_b = _make_pattern(
        db,
        name="Another RSI overbought (>70) + MACD histogram negative - sell signal [1m]",
        lifecycle="candidate",
        cpcv=2.6,
        quality_score=None,
    )
    db.commit()

    snapshot = scan_alpha_portfolio(db, settings_=_settings(), now=datetime(2026, 5, 21))

    assert snapshot["pattern_585"]["reasons"] == ["missing_oos_recert"]
    assert snapshot["recert_required_count"] == 1
    selected_ids = [c["scan_pattern_id"] for c in snapshot["shadow_candidates"]]
    assert candidate_a.id in selected_ids
    assert candidate_b.id not in selected_ids
    assert snapshot["full_promotion_blocked"] is True


def test_scan_alpha_portfolio_does_not_stage_negative_oos_candidate(db):
    _truncate_phase4_state(db)
    candidate = _make_pattern(
        db,
        name="Price below lower Bollinger Band (<10%) + RSI oversold [15m]",
        lifecycle="challenged",
        cpcv=2.0,
        quality_score=0.8,
    )
    candidate.oos_evaluated_at = datetime(2026, 5, 25)
    candidate.oos_trade_count = 40
    candidate.oos_win_rate = 0.45
    candidate.oos_avg_return_pct = -1.2
    db.commit()

    snapshot = scan_alpha_portfolio(db, settings_=_settings(), now=datetime(2026, 5, 25))

    selected_ids = [c["scan_pattern_id"] for c in snapshot["shadow_candidates"]]
    assert candidate.id not in selected_ids
    row = next(
        c for c in snapshot["blocked_candidates"]
        if c["scan_pattern_id"] == candidate.id
    )
    assert "negative_oos_floor" in row["floor_blocks"]


def test_persist_alpha_portfolio_snapshot_writes_columns_and_audit(db):
    _truncate_phase4_state(db)
    pat = _make_pattern(
        db,
        name="Falling Wedge Breakout + Trend Reclaim",
        lifecycle="candidate",
        cpcv=2.0,
        quality_score=0.75,
    )
    db.commit()

    snapshot = scan_alpha_portfolio(db, settings_=_settings(), now=datetime.utcnow())
    dry = persist_alpha_portfolio_snapshot(db, snapshot, execute=False)
    assert dry["dry_run"] is True

    written = persist_alpha_portfolio_snapshot(db, snapshot, execute=True)
    assert written["updates_written"] >= 1

    db.refresh(pat)
    assert pat.alpha_sleeve == "volatility_breakout"
    assert pat.portfolio_gate_score is not None
    assert pat.portfolio_gate_json["alpha_sleeve"] == "volatility_breakout"

    audit_count = db.execute(text(
        "SELECT COUNT(*) FROM trading_alpha_portfolio_gate_audit "
        "WHERE run_id = :run_id"
    ), {"run_id": snapshot["run_id"]}).scalar_one()
    assert audit_count >= 1


def test_queue_recert_for_required_uses_scheduler_source(db):
    _truncate_phase4_state(db)
    db.execute(text("DELETE FROM trading_pattern_recert_log"))
    promoted = ScanPattern(
        id=8585,
        name="Scheduler Recert Source",
        rules_json={"hold_hours": 24},
        origin="brain",
        asset_class="stock",
        timeframe="1d",
        confidence=0.8,
        evidence_count=30,
        active=True,
        promotion_status="promoted",
        lifecycle_stage="promoted",
        cpcv_n_paths=35,
        cpcv_median_sharpe=1.4,
        deflated_sharpe=1.0,
        pbo=0.0,
        n_effective_trials=10,
        promotion_gate_passed=True,
        quality_composite_score=0.8,
        raw_realized_trade_count=87,
        raw_realized_avg_return_pct=1.5,
        payoff_ratio=4.5,
        payoff_ratio_n=87,
    )
    db.add(promoted)
    db.commit()

    snapshot = scan_alpha_portfolio(db, settings_=_settings(), now=datetime(2026, 5, 21))
    out = queue_recert_for_required(
        db,
        snapshot,
        execute=True,
        mode_override="shadow",
    )

    assert out["queued"][0]["scan_pattern_id"] == 8585
    row = db.execute(text(
        """
        SELECT source, mode, reason, payload_json
        FROM trading_pattern_recert_log
        WHERE scan_pattern_id = 8585
        """
    )).mappings().one()
    assert row["source"] == "scheduler"
    assert row["mode"] == "shadow"
    assert row["reason"].startswith("alpha_portfolio_gate:")
    assert row["payload_json"]["origin"] == "alpha_portfolio_gate"


def test_alpha_portfolio_maintenance_persists_queues_and_stages(db):
    _truncate_phase4_state(db)
    db.execute(text("DELETE FROM trading_pattern_recert_log"))
    promoted = ScanPattern(
        id=8686,
        name="Maintenance Recert",
        rules_json={"hold_hours": 24},
        origin="brain",
        asset_class="stock",
        timeframe="1d",
        confidence=0.8,
        evidence_count=30,
        active=True,
        promotion_status="promoted",
        lifecycle_stage="promoted",
        cpcv_n_paths=35,
        cpcv_median_sharpe=1.4,
        deflated_sharpe=1.0,
        pbo=0.0,
        n_effective_trials=10,
        promotion_gate_passed=True,
        quality_composite_score=0.8,
        raw_realized_trade_count=87,
        raw_realized_avg_return_pct=1.5,
        payoff_ratio=4.5,
        payoff_ratio_n=87,
    )
    db.add(promoted)
    candidate = _make_pattern(
        db,
        name="Maintenance Shadow Candidate Breakout",
        lifecycle="candidate",
        cpcv=2.4,
        quality_score=0.78,
    )
    db.commit()

    out = run_alpha_portfolio_maintenance(
        db,
        settings_=_settings(
            chili_alpha_portfolio_min_shadow_score=0.40,
            chili_alpha_portfolio_refresh_quality_on_maintenance=False,
        ),
        now=datetime(2026, 5, 21),
        execute=True,
    )

    db.refresh(promoted)
    db.refresh(candidate)
    assert out["recert_required_count"] == 1
    assert out["persist"]["updates_written"] >= 2
    assert out["recert_queue"]["queued"][0]["scan_pattern_id"] == 8686
    assert candidate.lifecycle_stage == "shadow_promoted"
    assert candidate.promotion_status == "shadow_collecting_ev"
    assert promoted.recert_required is True
    source = db.execute(text(
        "SELECT source FROM trading_pattern_recert_log WHERE scan_pattern_id = 8686"
    )).scalar_one()
    assert source == "scheduler"


def test_migration_263_registered():
    src = Path("app/migrations.py").read_text(encoding="utf-8")
    assert "def _migration_263_alpha_portfolio_gate" in src
    assert '"263_alpha_portfolio_gate"' in src


def test_realized_sync_counts_qualified_autotrader_paper_outcomes():
    src = Path("app/services/trading/realized_stats_sync.py").read_text(encoding="utf-8")
    assert "trading_paper_trades" in src
    assert "chili_realized_sync_include_paper_dynamic" in src
    assert "paper_shadow_of_alert_id IS NOT NULL" in src
    assert '{"auto_trader_v1": true}' in src
