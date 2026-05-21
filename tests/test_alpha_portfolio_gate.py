from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from app.models.trading import ScanPattern
from app.services.trading.alpha_portfolio_gate import (
    AlphaPortfolioConfig,
    candidate_base_score,
    infer_alpha_sleeve,
    persist_alpha_portfolio_snapshot,
    portfolio_gate_score,
    recert_reasons_for_pattern,
    scan_alpha_portfolio,
)
from tests.test_pattern_cohort_promote import _make_pattern, _truncate_phase4_state


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


def test_migration_263_registered():
    src = Path("app/migrations.py").read_text(encoding="utf-8")
    assert "def _migration_263_alpha_portfolio_gate" in src
    assert '"263_alpha_portfolio_gate"' in src
