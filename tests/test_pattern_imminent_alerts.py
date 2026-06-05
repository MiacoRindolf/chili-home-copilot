"""Tests for ScanPattern imminent breakout alert helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.core import User
from app.models.trading import (
    AlertHistory,
    AutoTraderRun,
    BreakoutAlert,
    MarketSnapshot,
    PrescreenCandidate,
    ScanPattern,
    Trade,
)
from app.services.trading import pattern_imminent_alerts as imminent_mod
from app.services.trading.alerts import PATTERN_BREAKOUT_IMMINENT
from app.services.trading.pattern_imminent_alerts import (
    _cooldown_active,
    _shadow_poor_edge_pattern_ids,
    estimate_breakout_eta_hours,
    evaluate_imminent_readiness,
    flat_indicators_from_score,
    format_eta_range,
    gather_imminent_candidate_rows,
    run_pattern_imminent_scan,
    timeframe_to_hours_per_step,
    us_stock_session_open,
)

TEST_POOR_EDGE_MIN_REJECTS = 2
TEST_CACHE_TICKER = "CACHE-USD"
TEST_SCORE_PRICE = 100.0
TEST_SCORE_STOP_LOSS = 95.0
TEST_SCORE_TAKE_PROFIT = 110.0
TEST_SCORE_RESISTANCE = 112.0
TEST_SCORE_RSI = 55.0
TEST_SCORE_ADX = 20.0
TEST_SCORE_ATR = 2.0
TEST_RSI_BREAKOUT_TRIGGER = 60
TEST_EXECUTABLE_CRYPTO_TICKER = "GOOD-USD"
TEST_OPEN_POSITION_TICKER = "OPEN-USD"
TEST_UNSUPPORTED_CRYPTO_TICKER = "BAD-USD"
TEST_SCORE_FAILURE_TICKER = "FAIL-USD"
TEST_PATTERN_AVG_RETURN_PCT = 0.5
TEST_PATTERN_WIN_RATE = 0.6
TEST_PATTERN_EVIDENCE_COUNT = 10
TEST_MIN_READINESS = 0.0
TEST_FULL_READINESS_CAP = 1.0
TEST_UNIVERSE_CAP = 10
TEST_SCORE_FAILURE_COOLDOWN_MINUTES = 30.0
TEST_SCORE_FAILURE_MIN_FAILURES = 1
TEST_SCORE_TIME_BUDGET_SECONDS = 1.0
TEST_SCORE_BUDGET_EXPIRED_SECONDS = 2.0
TEST_PER_PATTERN_TICKER_CAP = 1
TEST_LOW_READINESS_TICKER = "LOWRD-USD"
TEST_CAP_READINESS_TICKER = "CAPRD-USD"
TEST_EXCLUDED_LIFECYCLE_TICKER = "CHALLENGED-USD"
TEST_DIAGNOSTIC_MIN_READINESS = 0.3
TEST_DIAGNOSTIC_READINESS_CAP = 0.49
TEST_DIAGNOSTIC_RSI_TRIGGER = 50
TEST_DIAGNOSTIC_ADX_TRIGGER = 10
TEST_LOW_READINESS_RSI = 55.0
TEST_CAP_READINESS_RSI = 100.0
TEST_FAILED_ADX = 0.0
TEST_SHADOW_NEAR_MISS_TICKER = "NEARMISS-USD"
TEST_SHADOW_NEAR_MISS_ADAPTIVE_TICKER = "ADAPTNEAR-USD"
TEST_SHADOW_NEAR_MISS_ADAPTIVE_LOW_TICKER = "ADAPTLOW-USD"
TEST_PILOT_NEAR_MISS_TICKER = "PILOTNEAR-USD"
TEST_HARD_RECERT_TICKER = "HARDRECERT"
TEST_OFFSESSION_STOCK_TICKER = "OFFSTOCK"
TEST_HARD_RECERT_REASON = "negative_oos_recert"
TEST_SOFT_RECERT_REASON = "missing_oos_recert"
TEST_SHADOW_NEAR_MISS_RSI = 70.0
TEST_SHADOW_NEAR_MISS_MIN_READINESS = 0.45
TEST_SHADOW_NEAR_MISS_MAX_GAP = 0.10
TEST_SHADOW_NEAR_MISS_STRICT_GAP = 0.01
TEST_SHADOW_NEAR_MISS_ADAPTIVE_MIN_FRACTION = 0.80
TEST_SHADOW_NEAR_MISS_ADAPTIVE_STRICT_FRACTION = 0.95
TEST_SHADOW_NEAR_MISS_ADAPTIVE_MAX_PER_RUN = 1
TEST_MIN_COMPOSITE_DISABLED = 0.0
TEST_HARD_RECERT_MIN_READINESS = 0.30
TEST_ROTATION_CAP = 2
TEST_ROTATION_WINDOW_MINUTES = 1
TEST_ROTATION_EPOCH_SECONDS = 0
TEST_ROTATION_NEXT_WINDOW_SECONDS = 60
TEST_ROTATION_SECOND_WINDOW_SECONDS = 120
TEST_ROTATION_FIRST_START = 0
TEST_ROTATION_SECOND_START = TEST_ROTATION_CAP
TEST_ROTATION_THIRD_START = TEST_ROTATION_CAP * 2
TEST_ROTATION_TICKERS = ["A-USD", "B-USD", "C-USD", "D-USD", "E-USD", "F-USD"]
TEST_ROTATION_STABLE_CAP = 4
TEST_ROTATION_EXPLORE_TICKERS = 1
TEST_ROTATION_STABLE_PREFIX = ["A-USD", "B-USD", "C-USD"]
TEST_ALIAS_MACD_HIST = 0.0123
TEST_ALIAS_STOCH_K = 21.0
TEST_ALIAS_STOCH_D = 19.5
TEST_ALIAS_BB_PCT_PERCENT = 12.5
TEST_ALIAS_BB_PCT_FRACTION = 0.125
TEST_ALIAS_VOLUME_RATIO = 1.7
TEST_STRUCTURAL_IBS = 0.1
TEST_STRUCTURAL_VCP_COUNT = 2
TEST_USER_ID = 1
TEST_POSITION_QUANTITY = 1.0


def test_timeframe_to_hours_per_step_defaults() -> None:
    assert timeframe_to_hours_per_step("1h") == 1.0
    assert timeframe_to_hours_per_step("15m") == 0.25
    assert timeframe_to_hours_per_step("unknown") == 6.5


def test_estimate_breakout_eta_hours_clamped() -> None:
    lo, hi = estimate_breakout_eta_hours(0.9, "1h", k=1.5, max_eta_hours=4.0)
    assert 5 / 60 <= lo <= hi <= 4.0
    lo2, hi2 = estimate_breakout_eta_hours(0.2, "1d", k=1.5, max_eta_hours=4.0)
    assert lo2 <= hi2 <= 4.0


def test_format_eta_range_minutes() -> None:
    s = format_eta_range(0.08, 0.2)
    assert "min" in s


def test_evaluate_imminent_readiness_all_pass_excluded_by_caller() -> None:
    """When every evaluable condition passes strictly, readiness is high; caller skips all_pass."""
    conditions = [
        {"indicator": "rsi_14", "op": ">", "value": 50},
    ]
    flat = {"rsi_14": 60.0, "price": 100.0}
    readiness, all_pass, ratio = evaluate_imminent_readiness(
        conditions, flat, evaluable_ratio_floor=0.5,
    )
    assert readiness is not None
    assert readiness > 0
    assert all_pass is True
    assert ratio == 1.0


def test_evaluate_imminent_readiness_partial() -> None:
    conditions = [
        {"indicator": "rsi_14", "op": ">", "value": 40},
        {"indicator": "rsi_14", "op": ">", "value": 95},
    ]
    flat = {"rsi_14": 60.0, "price": 100.0}
    readiness, all_pass, ratio = evaluate_imminent_readiness(
        conditions, flat, evaluable_ratio_floor=0.5,
    )
    assert readiness is not None
    assert all_pass is False
    assert 0.0 < readiness < 1.0


def test_evaluate_imminent_readiness_low_evaluable_ratio() -> None:
    conditions = [
        {"indicator": "rsi_14", "op": ">", "value": 50},
        {"indicator": "bb_squeeze", "op": "==", "value": True},
    ]
    flat = {"rsi_14": 60.0, "price": 100.0}
    readiness, _all_pass, ratio = evaluate_imminent_readiness(
        conditions, flat, evaluable_ratio_floor=0.99,
    )
    assert readiness is None
    assert ratio < 0.99


def test_evaluate_imminent_two_evaluable_low_ratio_ok() -> None:
    """Two evaluable clauses suffice even when coverage ratio is below floor."""
    conditions = [
        {"indicator": "rsi_14", "op": ">", "value": 50},
        {"indicator": "adx", "op": ">", "value": 20},
        {"indicator": "bb_squeeze", "op": "==", "value": True},
        {"indicator": "vwap_reclaim", "op": "==", "value": True},
    ]
    flat = {"rsi_14": 55.0, "adx": 18.0, "price": 100.0}
    readiness, all_pass, ratio = evaluate_imminent_readiness(
        conditions, flat, evaluable_ratio_floor=0.5,
    )
    assert readiness is not None
    assert ratio == 0.5
    assert all_pass is False


def test_flat_indicators_exposes_pattern_condition_aliases() -> None:
    flat = flat_indicators_from_score(
        {
            "price": TEST_SCORE_PRICE,
            "indicators": {
                "rsi": TEST_SCORE_RSI,
                "macd_hist": TEST_ALIAS_MACD_HIST,
                "stoch_k": TEST_ALIAS_STOCH_K,
                "stoch_d": TEST_ALIAS_STOCH_D,
                "bb_pct": TEST_ALIAS_BB_PCT_PERCENT,
                "vol_ratio": TEST_ALIAS_VOLUME_RATIO,
            },
        },
        resistance=None,
    )
    conditions = [
        {"indicator": "macd_histogram", "op": ">", "value": 0},
        {"indicator": "stochastic_k", "op": "<", "value": 25},
        {"indicator": "bb_pct", "op": "<", "value": 0.15},
        {"indicator": "volume_ratio", "op": ">", "value": 1.5},
    ]

    readiness, all_pass, ratio = evaluate_imminent_readiness(
        conditions,
        flat,
        evaluable_ratio_floor=1.0,
    )

    assert flat["macd_histogram"] == TEST_ALIAS_MACD_HIST
    assert flat["stochastic_k"] == TEST_ALIAS_STOCH_K
    assert flat["stochastic_d"] == TEST_ALIAS_STOCH_D
    assert flat["bb_pct"] == TEST_ALIAS_BB_PCT_FRACTION
    assert flat["bb_pct_percent"] == TEST_ALIAS_BB_PCT_PERCENT
    assert readiness is not None
    assert all_pass is True
    assert ratio == 1.0


def test_flat_indicators_maps_rvol_alias_to_all_volume_keys() -> None:
    flat = flat_indicators_from_score(
        {
            "price": TEST_SCORE_PRICE,
            "indicators": {
                "rvol": TEST_ALIAS_VOLUME_RATIO,
            },
        },
        resistance=None,
    )
    conditions = [
        {"indicator": "rvol", "op": ">", "value": 1.5},
        {"indicator": "rel_vol", "op": ">", "value": 1.5},
        {"indicator": "volume_ratio", "op": ">", "value": 1.5},
    ]

    readiness, all_pass, ratio = evaluate_imminent_readiness(
        conditions,
        flat,
        evaluable_ratio_floor=1.0,
    )

    assert flat["rvol"] == TEST_ALIAS_VOLUME_RATIO
    assert flat["rel_vol"] == TEST_ALIAS_VOLUME_RATIO
    assert flat["volume_ratio"] == TEST_ALIAS_VOLUME_RATIO
    assert readiness is not None
    assert all_pass is True
    assert ratio == 1.0


def test_flat_indicators_preserves_structural_pattern_fields() -> None:
    flat = flat_indicators_from_score(
        {
            "price": TEST_SCORE_PRICE,
            "indicators": {
                "ibs": TEST_STRUCTURAL_IBS,
                "pullback_stretch_entry": False,
                "vcp_count": TEST_STRUCTURAL_VCP_COUNT,
                "narrow_range": "NR7",
            },
        },
        resistance=None,
    )
    conditions = [
        {"indicator": "ibs", "op": "<", "value": 0.2},
        {"indicator": "pullback_stretch_entry", "op": "==", "value": True},
        {"indicator": "vcp_count", "op": ">=", "value": 2},
        {"indicator": "narrow_range", "op": "any_of", "value": ["NR4", "NR7"]},
    ]

    readiness, all_pass, ratio = evaluate_imminent_readiness(
        conditions,
        flat,
        evaluable_ratio_floor=1.0,
    )

    assert flat["ibs"] == TEST_STRUCTURAL_IBS
    assert flat["pullback_stretch_entry"] is False
    assert flat["vcp_count"] == TEST_STRUCTURAL_VCP_COUNT
    assert flat["narrow_range"] == "NR7"
    assert readiness is not None
    assert all_pass is False
    assert ratio == 1.0


def test_rotated_ticker_cap_slice_advances_by_window() -> None:
    pattern = SimpleNamespace(id=0)

    first, first_meta = imminent_mod._rotated_ticker_cap_slice(
        TEST_ROTATION_TICKERS,
        cap=TEST_ROTATION_CAP,
        pat=pattern,
        enabled=True,
        window_minutes=TEST_ROTATION_WINDOW_MINUTES,
        explore_count=TEST_ROTATION_CAP,
        now_utc=datetime.fromtimestamp(TEST_ROTATION_EPOCH_SECONDS, tz=timezone.utc),
    )
    second, second_meta = imminent_mod._rotated_ticker_cap_slice(
        TEST_ROTATION_TICKERS,
        cap=TEST_ROTATION_CAP,
        pat=pattern,
        enabled=True,
        window_minutes=TEST_ROTATION_WINDOW_MINUTES,
        explore_count=TEST_ROTATION_CAP,
        now_utc=datetime.fromtimestamp(TEST_ROTATION_NEXT_WINDOW_SECONDS, tz=timezone.utc),
    )
    third, third_meta = imminent_mod._rotated_ticker_cap_slice(
        TEST_ROTATION_TICKERS,
        cap=TEST_ROTATION_CAP,
        pat=pattern,
        enabled=True,
        window_minutes=TEST_ROTATION_WINDOW_MINUTES,
        explore_count=TEST_ROTATION_CAP,
        now_utc=datetime.fromtimestamp(TEST_ROTATION_SECOND_WINDOW_SECONDS, tz=timezone.utc),
    )
    disabled, disabled_meta = imminent_mod._rotated_ticker_cap_slice(
        TEST_ROTATION_TICKERS,
        cap=TEST_ROTATION_CAP,
        pat=pattern,
        enabled=False,
        window_minutes=TEST_ROTATION_WINDOW_MINUTES,
        explore_count=TEST_ROTATION_CAP,
        now_utc=datetime.fromtimestamp(TEST_ROTATION_SECOND_WINDOW_SECONDS, tz=timezone.utc),
    )

    assert first == ["A-USD", "B-USD"]
    assert second == ["C-USD", "D-USD"]
    assert third == ["E-USD", "F-USD"]
    assert first_meta["start"] == TEST_ROTATION_FIRST_START
    assert second_meta["start"] == TEST_ROTATION_SECOND_START
    assert third_meta["start"] == TEST_ROTATION_THIRD_START
    assert disabled == ["A-USD", "B-USD"]
    assert disabled_meta is None


def test_rotated_ticker_cap_slice_preserves_stable_prefix() -> None:
    pattern = SimpleNamespace(id=0)

    first, first_meta = imminent_mod._rotated_ticker_cap_slice(
        TEST_ROTATION_TICKERS,
        cap=TEST_ROTATION_STABLE_CAP,
        pat=pattern,
        enabled=True,
        window_minutes=TEST_ROTATION_WINDOW_MINUTES,
        explore_count=TEST_ROTATION_EXPLORE_TICKERS,
        now_utc=datetime.fromtimestamp(TEST_ROTATION_EPOCH_SECONDS, tz=timezone.utc),
    )
    second, second_meta = imminent_mod._rotated_ticker_cap_slice(
        TEST_ROTATION_TICKERS,
        cap=TEST_ROTATION_STABLE_CAP,
        pat=pattern,
        enabled=True,
        window_minutes=TEST_ROTATION_WINDOW_MINUTES,
        explore_count=TEST_ROTATION_EXPLORE_TICKERS,
        now_utc=datetime.fromtimestamp(TEST_ROTATION_NEXT_WINDOW_SECONDS, tz=timezone.utc),
    )

    assert first[:len(TEST_ROTATION_STABLE_PREFIX)] == TEST_ROTATION_STABLE_PREFIX
    assert second[:len(TEST_ROTATION_STABLE_PREFIX)] == TEST_ROTATION_STABLE_PREFIX
    assert first[-TEST_ROTATION_EXPLORE_TICKERS:] == ["D-USD"]
    assert second[-TEST_ROTATION_EXPLORE_TICKERS:] == ["E-USD"]
    assert first_meta["stable_count"] == len(TEST_ROTATION_STABLE_PREFIX)
    assert first_meta["explore_count"] == TEST_ROTATION_EXPLORE_TICKERS
    assert second_meta["stable_count"] == len(TEST_ROTATION_STABLE_PREFIX)
    assert second_meta["explore_count"] == TEST_ROTATION_EXPLORE_TICKERS


def test_us_stock_session_open_saturday_utc() -> None:
    sat = datetime(2026, 3, 21, 14, 0, 0, tzinfo=timezone.utc)
    assert us_stock_session_open(sat) is False


def test_cooldown_ignores_failed_imminent_delivery(db) -> None:
    pattern = ScanPattern(
        name="Cooldown test pattern",
        rules_json={},
        origin="test",
        asset_class="stock",
    )
    db.add(pattern)
    db.flush()

    row = AlertHistory(
        user_id=1,
        alert_type=PATTERN_BREAKOUT_IMMINENT,
        ticker="SPY",
        message="failed delivery",
        scan_pattern_id=pattern.id,
        sent_via="sms_failed",
        success=False,
    )
    db.add(row)
    db.commit()

    assert _cooldown_active(db, 1, "SPY", pattern.id, 3.0) is False

    row.success = True
    db.commit()

    assert _cooldown_active(db, 1, "SPY", pattern.id, 3.0) is True


def test_shadow_poor_edge_cooldown_requires_negative_stored_return(
    db,
    monkeypatch,
) -> None:
    poor = ScanPattern(
        name="Poor shadow pattern",
        rules_json={},
        origin="test",
        asset_class="crypto",
        lifecycle_stage="shadow_promoted",
        avg_return_pct=-0.5,
    )
    healthy = ScanPattern(
        name="Healthy shadow pattern",
        rules_json={},
        origin="test",
        asset_class="crypto",
        lifecycle_stage="shadow_promoted",
        avg_return_pct=0.25,
    )
    db.add_all([poor, healthy])
    db.flush()
    now = datetime.utcnow()
    for pattern in (poor, healthy):
        for _ in range(TEST_POOR_EDGE_MIN_REJECTS):
            db.add(AutoTraderRun(
                user_id=1,
                scan_pattern_id=pattern.id,
                ticker="EDGE-USD",
                decision="skipped",
                reason="non_positive_expected_edge",
                created_at=now,
            ))
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_poor_edge_cooldown_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_poor_edge_min_rejects",
        TEST_POOR_EDGE_MIN_REJECTS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_poor_edge_lookback_hours",
        2.0,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_poor_edge_max_avg_return_pct",
        0.0,
    )

    cooldown_ids, counts, details = _shadow_poor_edge_pattern_ids(
        db,
        [poor, healthy],
        user_id=1,
    )

    assert int(poor.id) in cooldown_ids
    assert int(healthy.id) not in cooldown_ids
    assert counts[int(poor.id)] == TEST_POOR_EDGE_MIN_REJECTS
    assert details[int(poor.id)]["cooldown_basis"] == "stored_avg_return"
    assert details[int(poor.id)]["max_avg_return_pct"] == 0.0


def test_shadow_poor_edge_cooldown_uses_recent_expected_net(
    db,
    monkeypatch,
) -> None:
    old_positive = ScanPattern(
        name="Old positive but recent edge debt",
        rules_json={},
        origin="test",
        asset_class="crypto",
        lifecycle_stage="shadow_promoted",
        avg_return_pct=1.25,
    )
    db.add(old_positive)
    db.flush()
    now = datetime.utcnow()
    for _ in range(TEST_POOR_EDGE_MIN_REJECTS):
        db.add(AutoTraderRun(
            user_id=1,
            scan_pattern_id=old_positive.id,
            ticker="EDGE-USD",
            decision="skipped",
            reason="non_positive_expected_edge",
            rule_snapshot={"entry_edge": {"expected_net_pct": -1.1}},
            created_at=now,
        ))
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_poor_edge_cooldown_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_poor_edge_expected_net_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_poor_edge_min_rejects",
        TEST_POOR_EDGE_MIN_REJECTS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_poor_edge_lookback_hours",
        2.0,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_poor_edge_max_avg_return_pct",
        0.0,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_poor_edge_max_avg_expected_net_pct",
        -0.75,
    )

    cooldown_ids, counts, details = _shadow_poor_edge_pattern_ids(
        db,
        [old_positive],
        user_id=1,
    )

    assert int(old_positive.id) in cooldown_ids
    assert counts[int(old_positive.id)] == TEST_POOR_EDGE_MIN_REJECTS
    assert details[int(old_positive.id)]["cooldown_basis"] == "recent_expected_net"
    assert details[int(old_positive.id)]["avg_expected_net_pct"] == pytest.approx(-1.1)
    assert details[int(old_positive.id)]["expected_net_sample_n"] == TEST_POOR_EDGE_MIN_REJECTS


def test_gather_imminent_skips_poor_shadow_pattern_but_keeps_healthy(
    db,
    monkeypatch,
) -> None:
    rules = {"conditions": [{"indicator": "rsi_14", "op": ">", "value": 60}]}
    poor = ScanPattern(
        name="Poor shadow scanner",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="shadow_promoted",
        avg_return_pct=-0.5,
        ticker_scope="explicit_list",
        scope_tickers='["POOR-USD"]',
        win_rate=0.1,
        evidence_count=10,
    )
    healthy = ScanPattern(
        name="Healthy shadow scanner",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="shadow_promoted",
        avg_return_pct=0.5,
        ticker_scope="explicit_list",
        scope_tickers='["GOOD-USD"]',
        win_rate=0.6,
        evidence_count=10,
    )
    db.add_all([poor, healthy])
    db.flush()
    for _ in range(TEST_POOR_EDGE_MIN_REJECTS):
        db.add(AutoTraderRun(
            user_id=1,
            scan_pattern_id=poor.id,
            ticker="POOR-USD",
            decision="skipped",
            reason="non_positive_expected_edge",
            created_at=datetime.utcnow(),
        ))
    db.commit()

    monkeypatch.setattr(imminent_mod.settings, "chili_shadow_promoted_lifecycle_enabled", True)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_min_readiness", 0.0)
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_composite_main",
        TEST_MIN_COMPOSITE_DISABLED,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_poor_edge_min_rejects",
        TEST_POOR_EDGE_MIN_REJECTS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_poor_edge_lookback_hours",
        2.0,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: (["POOR-USD", "GOOD-USD"], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({"POOR-USD", "GOOD-USD"}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_score_ticker",
        lambda ticker, skip_fundamentals=True, skip_pattern_engine=False: {
            "price": 100.0,
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "signals": ["test"],
            "indicators": {"rsi": 55.0, "adx": 20.0, "atr": 2.0},
        },
    )
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=1,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    assert [c["ticker"] for c in candidates] == ["GOOD-USD"]
    assert meta["skip_reasons"]["shadow_poor_edge_cooldown"] == 1
    assert meta["top_suppressed"][0]["reason"] == "shadow_poor_edge_cooldown"
    assert meta["top_suppressed"][0]["cooldown_basis"] == "stored_avg_return"


def test_gather_imminent_reports_readiness_band_and_lifecycle_diagnostics(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {
                "indicator": "rsi_14",
                "op": ">",
                "value": TEST_DIAGNOSTIC_RSI_TRIGGER,
            },
            {
                "indicator": "adx",
                "op": ">",
                "value": TEST_DIAGNOSTIC_ADX_TRIGGER,
            },
        ],
    }
    below = ScanPattern(
        name="Below readiness diagnostic",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_LOW_READINESS_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    above = ScanPattern(
        name="Above readiness cap diagnostic",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_CAP_READINESS_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    challenged = ScanPattern(
        name="Excluded lifecycle diagnostic",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="challenged",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_EXCLUDED_LIFECYCLE_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add_all([below, above, challenged])
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_DIAGNOSTIC_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_DIAGNOSTIC_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_near_miss_limit",
        TEST_UNIVERSE_CAP,
    )
    tickers = [
        TEST_LOW_READINESS_TICKER,
        TEST_CAP_READINESS_TICKER,
        TEST_EXCLUDED_LIFECYCLE_TICKER,
    ]
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: (tickers, {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset(tickers),
    )
    score_calls: list[str] = []
    rsi_by_ticker = {
        TEST_LOW_READINESS_TICKER: TEST_LOW_READINESS_RSI,
        TEST_CAP_READINESS_TICKER: TEST_CAP_READINESS_RSI,
    }

    def _fake_score_ticker(
        ticker: str,
        skip_fundamentals: bool = True,
        skip_pattern_engine: bool = False,
    ):
        score_calls.append(ticker)
        return {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": rsi_by_ticker[ticker],
                "adx": TEST_FAILED_ADX,
                "atr": TEST_SCORE_ATR,
            },
        }

    monkeypatch.setattr(imminent_mod, "_score_ticker", _fake_score_ticker)
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=1,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    assert candidates == []
    assert score_calls == [TEST_LOW_READINESS_TICKER, TEST_CAP_READINESS_TICKER]
    skip = meta["skip_reasons"]
    assert skip["readiness_outside_band"] >= 2
    assert skip["readiness_below_min"] >= 1
    assert skip["readiness_at_or_above_cap"] >= 1
    assert meta["excluded_lifecycle_by_stage"]["challenged"] >= 1
    near_misses = {
        row["ticker"]: row["reason"]
        for row in meta["readiness_band_near_misses"]
    }
    assert near_misses[TEST_LOW_READINESS_TICKER] == "readiness_below_min"
    assert near_misses[TEST_CAP_READINESS_TICKER] == "readiness_at_or_above_cap"


def test_gather_imminent_admits_shadow_near_miss_observation_lane(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {
                "indicator": "rsi_14",
                "op": ">",
                "value": TEST_DIAGNOSTIC_RSI_TRIGGER,
            },
            {
                "indicator": "adx",
                "op": ">",
                "value": TEST_DIAGNOSTIC_ADX_TRIGGER,
            },
        ],
    }
    pattern = ScanPattern(
        name="Shadow near-miss observation",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="shadow_promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_SHADOW_NEAR_MISS_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add(pattern)
    db.commit()

    monkeypatch.setattr(imminent_mod.settings, "chili_shadow_promoted_lifecycle_enabled", True)
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_max_gap",
        TEST_SHADOW_NEAR_MISS_MAX_GAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_SHADOW_NEAR_MISS_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_min_composite_main", 0.0)
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_SHADOW_NEAR_MISS_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_SHADOW_NEAR_MISS_TICKER}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_score_ticker",
        lambda ticker, skip_fundamentals=True, skip_pattern_engine=False: {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SHADOW_NEAR_MISS_RSI,
                "adx": TEST_FAILED_ADX,
                "atr": TEST_SCORE_ATR,
            },
        },
    )
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=1,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    matches = [
        c for c in candidates
        if c["ticker"] == TEST_SHADOW_NEAR_MISS_TICKER
    ]
    assert len(matches) == 1
    candidate = matches[0]
    assert candidate["signal_lane"] == "shadow_near_miss"
    assert candidate["readiness"] < TEST_SHADOW_NEAR_MISS_MIN_READINESS
    assert candidate["readiness_gap_to_min"] <= TEST_SHADOW_NEAR_MISS_MAX_GAP
    assert meta["shadow_near_miss_eligible"] >= 1
    assert meta["shadow_near_miss_admitted"] >= 1


def test_gather_imminent_routes_hard_recert_debt_to_shadow_lane(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {
                "indicator": "rsi_14",
                "op": ">",
                "value": TEST_DIAGNOSTIC_RSI_TRIGGER,
            },
            {
                "indicator": "adx",
                "op": ">",
                "value": TEST_DIAGNOSTIC_ADX_TRIGGER,
            },
        ],
    }
    pattern = ScanPattern(
        name="Hard recert shadow evidence",
        rules_json=rules,
        origin="test",
        asset_class="stocks",
        lifecycle_stage="promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_HARD_RECERT_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
        recert_required=True,
        recert_reason=f"{TEST_HARD_RECERT_REASON},{TEST_SOFT_RECERT_REASON}",
    )
    db.add(pattern)
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_hard_recert_shadow_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_HARD_RECERT_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_composite_main",
        TEST_MIN_COMPOSITE_DISABLED,
    )
    monkeypatch.setattr(
        imminent_mod,
        "us_stock_extended_session_open",
        lambda: True,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_HARD_RECERT_TICKER], {}),
    )
    monkeypatch.setattr(imminent_mod, "_coinbase_spot_ticker_set", frozenset)
    monkeypatch.setattr(
        imminent_mod,
        "_score_ticker",
        lambda ticker, skip_fundamentals=True, skip_pattern_engine=False: {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SHADOW_NEAR_MISS_RSI,
                "adx": TEST_FAILED_ADX,
                "atr": TEST_SCORE_ATR,
            },
        },
    )
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=1,
        equity_session_open=True,
        apply_main_dispatch_filters=True,
    )

    matches = [c for c in candidates if c["ticker"] == TEST_HARD_RECERT_TICKER]
    assert len(matches) == 1
    candidate = matches[0]
    assert candidate["signal_lane"] == imminent_mod.HARD_RECERT_SHADOW_SIGNAL_LANE
    assert candidate["hard_recert_reasons"] == [TEST_HARD_RECERT_REASON]
    assert meta["hard_recert_shadow_patterns"] >= 1
    assert meta["hard_recert_shadow_eligible"] >= 1
    assert meta["hard_recert_shadow_admitted"] >= 1
    assert meta["hard_recert_shadow_reason_counts"][TEST_HARD_RECERT_REASON] >= 1


def test_gather_imminent_admits_adaptive_shadow_near_miss_buffer(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {
                "indicator": "rsi_14",
                "op": ">",
                "value": TEST_DIAGNOSTIC_RSI_TRIGGER,
            },
            {
                "indicator": "adx",
                "op": ">",
                "value": TEST_DIAGNOSTIC_ADX_TRIGGER,
            },
        ],
    }
    pattern = ScanPattern(
        name="Adaptive shadow near-miss observation",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="shadow_promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_SHADOW_NEAR_MISS_ADAPTIVE_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add(pattern)
    db.commit()

    monkeypatch.setattr(imminent_mod.settings, "chili_shadow_promoted_lifecycle_enabled", True)
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_max_gap",
        TEST_SHADOW_NEAR_MISS_STRICT_GAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_adaptive_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_adaptive_max_per_run",
        TEST_SHADOW_NEAR_MISS_ADAPTIVE_MAX_PER_RUN,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_adaptive_min_readiness_fraction",
        TEST_SHADOW_NEAR_MISS_ADAPTIVE_MIN_FRACTION,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_SHADOW_NEAR_MISS_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_min_composite_main", 0.0)
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_SHADOW_NEAR_MISS_ADAPTIVE_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_SHADOW_NEAR_MISS_ADAPTIVE_TICKER}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_score_ticker",
        lambda ticker, skip_fundamentals=True, skip_pattern_engine=False: {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SHADOW_NEAR_MISS_RSI,
                "adx": TEST_FAILED_ADX,
                "atr": TEST_SCORE_ATR,
            },
        },
    )
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=1,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["ticker"] == TEST_SHADOW_NEAR_MISS_ADAPTIVE_TICKER
    assert candidate["signal_lane"] == "shadow_near_miss"
    assert (
        candidate["shadow_near_miss_source"]
        == imminent_mod.SHADOW_NEAR_MISS_SOURCE_ADAPTIVE_BUFFER
    )
    assert candidate["readiness_gap_to_min"] > TEST_SHADOW_NEAR_MISS_STRICT_GAP
    assert meta["shadow_near_miss_gap_eligible"] == 0
    assert meta["shadow_near_miss_adaptive_eligible"] == 1
    assert meta["shadow_near_miss_adaptive_selected"] == 1
    assert meta["shadow_near_miss_adaptive_admitted"] == 1


def test_gather_imminent_adaptive_shadow_near_miss_respects_readiness_floor(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {
                "indicator": "rsi_14",
                "op": ">",
                "value": TEST_DIAGNOSTIC_RSI_TRIGGER,
            },
            {
                "indicator": "adx",
                "op": ">",
                "value": TEST_DIAGNOSTIC_ADX_TRIGGER,
            },
        ],
    }
    pattern = ScanPattern(
        name="Adaptive shadow near-miss too weak",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="shadow_promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_SHADOW_NEAR_MISS_ADAPTIVE_LOW_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add(pattern)
    db.commit()

    monkeypatch.setattr(imminent_mod.settings, "chili_shadow_promoted_lifecycle_enabled", True)
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_max_gap",
        TEST_SHADOW_NEAR_MISS_STRICT_GAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_adaptive_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_adaptive_max_per_run",
        TEST_SHADOW_NEAR_MISS_ADAPTIVE_MAX_PER_RUN,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_adaptive_min_readiness_fraction",
        TEST_SHADOW_NEAR_MISS_ADAPTIVE_STRICT_FRACTION,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_SHADOW_NEAR_MISS_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_min_composite_main", 0.0)
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_SHADOW_NEAR_MISS_ADAPTIVE_LOW_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_SHADOW_NEAR_MISS_ADAPTIVE_LOW_TICKER}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_score_ticker",
        lambda ticker, skip_fundamentals=True, skip_pattern_engine=False: {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SHADOW_NEAR_MISS_RSI,
                "adx": TEST_FAILED_ADX,
                "atr": TEST_SCORE_ATR,
            },
        },
    )
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=1,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    assert candidates == []
    assert meta["shadow_near_miss_adaptive_eligible"] == 0
    assert meta["skip_reasons"]["readiness_below_min"] == 1


def test_gather_imminent_admits_pilot_near_miss_as_shadow_observation_lane(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {
                "indicator": "rsi_14",
                "op": ">",
                "value": TEST_DIAGNOSTIC_RSI_TRIGGER,
            },
            {
                "indicator": "adx",
                "op": ">",
                "value": TEST_DIAGNOSTIC_ADX_TRIGGER,
            },
        ],
    }
    pattern = ScanPattern(
        name="Pilot near-miss observation",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="pilot_promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_PILOT_NEAR_MISS_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add(pattern)
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_lifecycle_stages",
        "shadow_promoted,pilot_promoted",
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_shadow_near_miss_max_gap",
        TEST_SHADOW_NEAR_MISS_MAX_GAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_SHADOW_NEAR_MISS_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_min_composite_main", 0.0)
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_PILOT_NEAR_MISS_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_PILOT_NEAR_MISS_TICKER}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_score_ticker",
        lambda ticker, skip_fundamentals=True, skip_pattern_engine=False: {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SHADOW_NEAR_MISS_RSI,
                "adx": TEST_FAILED_ADX,
                "atr": TEST_SCORE_ATR,
            },
        },
    )
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=1,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    assert len(candidates) == 1
    assert candidates[0]["ticker"] == TEST_PILOT_NEAR_MISS_TICKER
    assert candidates[0]["signal_lane"] == "shadow_near_miss"
    assert candidates[0]["readiness"] < TEST_SHADOW_NEAR_MISS_MIN_READINESS
    assert "pilot_promoted" in meta["shadow_near_miss_lifecycle_stages"]
    assert meta["shadow_near_miss_eligible"] >= 1
    assert meta["shadow_near_miss_admitted"] >= 1


def test_gather_imminent_memoizes_ticker_scores_across_patterns(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {"indicator": "rsi_14", "op": ">", "value": TEST_RSI_BREAKOUT_TRIGGER}
        ]
    }
    first = ScanPattern(
        name="First cached pattern",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_CACHE_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    second = ScanPattern(
        name="Second cached pattern",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_CACHE_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add_all([first, second])
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_CACHE_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_CACHE_TICKER}),
    )
    score_calls: list[str] = []

    def _fake_score_ticker(
        ticker: str,
        skip_fundamentals: bool = True,
        skip_pattern_engine: bool = False,
    ):
        score_calls.append(ticker)
        return {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SCORE_RSI,
                "adx": TEST_SCORE_ADX,
                "atr": TEST_SCORE_ATR,
            },
        }

    monkeypatch.setattr(imminent_mod, "_score_ticker", _fake_score_ticker)
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=1,
        equity_session_open=False,
    )

    assert score_calls == [TEST_CACHE_TICKER]
    assert {int(c["pattern"].id) for c in candidates} == {int(first.id), int(second.id)}
    assert meta["tickers_scored"] == len(candidates)
    assert meta["score_cache_size"] == 1
    assert meta["score_cache_misses"] == 1
    assert meta["score_cache_hits"] == 1


def test_gather_imminent_releases_read_transaction_before_scoring(
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {"indicator": "rsi_14", "op": ">", "value": TEST_RSI_BREAKOUT_TRIGGER}
        ]
    }
    pattern = ScanPattern(
        name="Released read transaction pattern",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_CACHE_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
        active=True,
        id=1001,
    )

    class _FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return [pattern]

    class _FakeSession:
        new = ()
        dirty = ()
        deleted = ()

        def __init__(self) -> None:
            self._in_transaction = True

        def query(self, *args, **kwargs):
            return _FakeQuery()

        def in_transaction(self) -> bool:
            return self._in_transaction

        def rollback(self) -> None:
            self._in_transaction = False

    fake_db = _FakeSession()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_CACHE_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_shadow_poor_edge_pattern_ids",
        lambda *args, **kwargs: (set(), {}, {}),
    )
    transaction_checks: list[tuple[str, bool]] = []

    def _fake_coinbase_spot_ticker_set() -> frozenset[str]:
        transaction_checks.append(("coinbase", fake_db.in_transaction()))
        return frozenset({TEST_CACHE_TICKER})

    def _fake_score_ticker(
        ticker: str,
        skip_fundamentals: bool = True,
        skip_pattern_engine: bool = False,
    ):
        transaction_checks.append(("score", fake_db.in_transaction()))
        return {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SCORE_RSI,
                "adx": TEST_SCORE_ADX,
                "atr": TEST_SCORE_ATR,
            },
        }

    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        _fake_coinbase_spot_ticker_set,
    )
    monkeypatch.setattr(imminent_mod, "_score_ticker", _fake_score_ticker)
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        fake_db,
        user_id=1,
        equity_session_open=False,
    )

    assert [c["ticker"] for c in candidates] == [TEST_CACHE_TICKER]
    assert transaction_checks == [("coinbase", False), ("score", False)]
    assert meta["read_transaction_released_before_scoring"] is True


def test_build_imminent_universe_filters_non_coinbase_crypto(
    db,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_filter_crypto_to_coinbase_spot",
        True,
    )
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_use_prescreener_universe", False)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_use_predictions_universe", False)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_use_scanner_universe", False)
    monkeypatch.setattr(imminent_mod, "DEFAULT_SCAN_TICKERS", [])
    monkeypatch.setattr(
        imminent_mod,
        "DEFAULT_CRYPTO_TICKERS",
        [TEST_EXECUTABLE_CRYPTO_TICKER, TEST_UNSUPPORTED_CRYPTO_TICKER],
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_EXECUTABLE_CRYPTO_TICKER}),
    )

    tickers, counts = imminent_mod.build_imminent_ticker_universe(
        db,
        user_id=1,
        cap=TEST_UNIVERSE_CAP,
    )

    assert tickers == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert counts["crypto_execution_filter_dropped"] == 1
    assert counts["crypto_execution_filter_spot_tickers"] == 1


def test_gather_imminent_filters_explicit_non_coinbase_crypto(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {"indicator": "rsi_14", "op": ">", "value": TEST_RSI_BREAKOUT_TRIGGER}
        ]
    }
    executable = ScanPattern(
        name="Executable crypto pattern",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_EXECUTABLE_CRYPTO_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    unsupported = ScanPattern(
        name="Unsupported crypto pattern",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_UNSUPPORTED_CRYPTO_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add_all([executable, unsupported])
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_filter_crypto_to_coinbase_spot",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: (
            [TEST_EXECUTABLE_CRYPTO_TICKER, TEST_UNSUPPORTED_CRYPTO_TICKER],
            {},
        ),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_EXECUTABLE_CRYPTO_TICKER}),
    )
    score_calls: list[str] = []

    def _fake_score_ticker(
        ticker: str,
        skip_fundamentals: bool = True,
        skip_pattern_engine: bool = False,
    ):
        score_calls.append(ticker)
        return {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SCORE_RSI,
                "adx": TEST_SCORE_ADX,
                "atr": TEST_SCORE_ATR,
            },
        }

    monkeypatch.setattr(imminent_mod, "_score_ticker", _fake_score_ticker)
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=1,
        equity_session_open=False,
    )

    assert score_calls == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert [c["ticker"] for c in candidates] == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert meta["skip_reasons"]["crypto_execution_universe_filtered"] == 1
    assert meta["crypto_execution_filter_spot_tickers"] == 1


def test_gather_imminent_cools_repeated_score_failures(
    db,
    monkeypatch,
) -> None:
    imminent_mod._SCORE_FAILURE_CACHE.clear()
    rules = {
        "conditions": [
            {"indicator": "rsi_14", "op": ">", "value": TEST_RSI_BREAKOUT_TRIGGER}
        ]
    }
    pattern = ScanPattern(
        name="Score failure cooldown pattern",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_SCORE_FAILURE_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add(pattern)
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_score_failure_cooldown_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_score_failure_cooldown_minutes",
        TEST_SCORE_FAILURE_COOLDOWN_MINUTES,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_score_failure_min_failures",
        TEST_SCORE_FAILURE_MIN_FAILURES,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_SCORE_FAILURE_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_SCORE_FAILURE_TICKER}),
    )
    score_calls: list[str] = []

    def _failing_score_ticker(
        ticker: str,
        skip_fundamentals: bool = True,
        skip_pattern_engine: bool = False,
    ):
        score_calls.append(ticker)
        return None

    monkeypatch.setattr(imminent_mod, "_score_ticker", _failing_score_ticker)

    try:
        _candidates_1, meta_1 = gather_imminent_candidate_rows(
            db,
            user_id=1,
            equity_session_open=False,
        )
        _candidates_2, meta_2 = gather_imminent_candidate_rows(
            db,
            user_id=1,
            equity_session_open=False,
        )
    finally:
        imminent_mod._SCORE_FAILURE_CACHE.clear()

    assert score_calls == [TEST_SCORE_FAILURE_TICKER]
    assert meta_1["skip_reasons"]["score_failed"] == 1
    assert meta_2["skip_reasons"]["score_failure_cooldown"] == 1
    assert meta_2["score_cache_misses"] == 0


def test_gather_imminent_honors_score_time_budget(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {"indicator": "rsi_14", "op": ">", "value": TEST_RSI_BREAKOUT_TRIGGER}
        ]
    }
    first = ScanPattern(
        name="Budget first pattern",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_EXECUTABLE_CRYPTO_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    second = ScanPattern(
        name="Budget second pattern",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="promoted",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_CACHE_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add_all([first, second])
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_score_time_budget_seconds",
        TEST_SCORE_TIME_BUDGET_SECONDS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_EXECUTABLE_CRYPTO_TICKER, TEST_CACHE_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_EXECUTABLE_CRYPTO_TICKER, TEST_CACHE_TICKER}),
    )
    monotonic_values = iter([
        0.0,
        0.0,
        TEST_SCORE_BUDGET_EXPIRED_SECONDS,
        TEST_SCORE_BUDGET_EXPIRED_SECONDS,
    ])
    monkeypatch.setattr(
        imminent_mod._time,
        "monotonic",
        lambda: next(monotonic_values, TEST_SCORE_BUDGET_EXPIRED_SECONDS),
    )
    score_calls: list[str] = []

    def _fake_score_ticker(
        ticker: str,
        skip_fundamentals: bool = True,
        skip_pattern_engine: bool = False,
    ):
        score_calls.append(ticker)
        return {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SCORE_RSI,
                "adx": TEST_SCORE_ADX,
                "atr": TEST_SCORE_ATR,
            },
        }

    monkeypatch.setattr(imminent_mod, "_score_ticker", _fake_score_ticker)
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=1,
        equity_session_open=False,
    )

    assert score_calls == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert [c["ticker"] for c in candidates] == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert meta["score_time_budget_hit"] is True


def test_gather_imminent_prioritizes_promoted_when_score_budget_tight(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {"indicator": "rsi_14", "op": ">", "value": TEST_RSI_BREAKOUT_TRIGGER}
        ]
    }
    shadow = ScanPattern(
        name="Budget shadow pattern",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage=imminent_mod.SHADOW_PROMOTED_STAGE,
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_CACHE_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    promoted = ScanPattern(
        name="Budget promoted pattern",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage=imminent_mod.PROMOTED_STAGE,
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_EXECUTABLE_CRYPTO_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add_all([shadow, promoted])
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_score_time_budget_seconds",
        TEST_SCORE_TIME_BUDGET_SECONDS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_composite_main",
        TEST_MIN_COMPOSITE_DISABLED,
    )
    monkeypatch.setattr(
        imminent_mod,
        "us_stock_extended_session_open",
        lambda: True,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_CACHE_TICKER, TEST_EXECUTABLE_CRYPTO_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_CACHE_TICKER, TEST_EXECUTABLE_CRYPTO_TICKER}),
    )
    monotonic_values = iter([
        TEST_ROTATION_EPOCH_SECONDS,
        TEST_ROTATION_EPOCH_SECONDS,
        TEST_SCORE_BUDGET_EXPIRED_SECONDS,
        TEST_SCORE_BUDGET_EXPIRED_SECONDS,
    ])
    monkeypatch.setattr(
        imminent_mod._time,
        "monotonic",
        lambda: next(monotonic_values, TEST_SCORE_BUDGET_EXPIRED_SECONDS),
    )
    score_calls: list[str] = []

    def _fake_score_ticker(
        ticker: str,
        skip_fundamentals: bool = True,
        skip_pattern_engine: bool = False,
    ):
        score_calls.append(ticker)
        return {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SCORE_RSI,
                "adx": TEST_SCORE_ADX,
                "atr": TEST_SCORE_ATR,
            },
        }

    monkeypatch.setattr(imminent_mod, "_score_ticker", _fake_score_ticker)
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=TEST_USER_ID,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    assert score_calls == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert [c["ticker"] for c in candidates] == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert meta["score_time_budget_hit"] is True
    assert meta["pattern_priority_top_stage_counts"][imminent_mod.PROMOTED_STAGE] == 1


def test_gather_imminent_prioritizes_tradeable_pilot_over_hard_recert_debt(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {"indicator": "rsi_14", "op": ">", "value": TEST_RSI_BREAKOUT_TRIGGER}
        ]
    }
    hard_recert = ScanPattern(
        name="Budget hard recert pattern",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage=imminent_mod.PROMOTED_STAGE,
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_HARD_RECERT_TICKER}-USD"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
        recert_required=True,
        recert_reason=TEST_HARD_RECERT_REASON,
    )
    pilot = ScanPattern(
        name="Budget pilot pattern",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage=imminent_mod.PILOT_PROMOTED_STAGE,
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_EXECUTABLE_CRYPTO_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add_all([hard_recert, pilot])
    db.commit()

    hard_ticker = f"{TEST_HARD_RECERT_TICKER}-USD"
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_score_time_budget_seconds",
        TEST_SCORE_TIME_BUDGET_SECONDS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_hard_recert_shadow_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_composite_main",
        TEST_MIN_COMPOSITE_DISABLED,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([hard_ticker, TEST_EXECUTABLE_CRYPTO_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({hard_ticker, TEST_EXECUTABLE_CRYPTO_TICKER}),
    )
    monotonic_values = iter([
        TEST_ROTATION_EPOCH_SECONDS,
        TEST_ROTATION_EPOCH_SECONDS,
        TEST_SCORE_BUDGET_EXPIRED_SECONDS,
        TEST_SCORE_BUDGET_EXPIRED_SECONDS,
    ])
    monkeypatch.setattr(
        imminent_mod._time,
        "monotonic",
        lambda: next(monotonic_values, TEST_SCORE_BUDGET_EXPIRED_SECONDS),
    )
    score_calls: list[str] = []

    def _fake_score_ticker(
        ticker: str,
        skip_fundamentals: bool = True,
        skip_pattern_engine: bool = False,
    ):
        score_calls.append(ticker)
        return {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SCORE_RSI,
                "adx": TEST_SCORE_ADX,
                "atr": TEST_SCORE_ATR,
            },
        }

    monkeypatch.setattr(imminent_mod, "_score_ticker", _fake_score_ticker)
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=TEST_USER_ID,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    assert score_calls == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert [c["ticker"] for c in candidates] == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert meta["score_time_budget_hit"] is True


def test_gather_imminent_reuses_score_resistance_without_extra_fetch(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {"indicator": "rsi_14", "op": ">", "value": TEST_RSI_BREAKOUT_TRIGGER}
        ]
    }
    pattern = ScanPattern(
        name="Score resistance reuse",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage=imminent_mod.PROMOTED_STAGE,
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_EXECUTABLE_CRYPTO_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add(pattern)
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_composite_main",
        TEST_MIN_COMPOSITE_DISABLED,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_EXECUTABLE_CRYPTO_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_EXECUTABLE_CRYPTO_TICKER}),
    )
    score_modes: list[tuple[bool, bool]] = []

    def _fake_score_ticker(
        ticker: str,
        skip_fundamentals: bool = True,
        skip_pattern_engine: bool = False,
    ):
        score_modes.append((skip_fundamentals, skip_pattern_engine))
        return {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "resistance": TEST_SCORE_RESISTANCE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SCORE_RSI,
                "adx": TEST_SCORE_ADX,
                "atr": TEST_SCORE_ATR,
            },
        }

    monkeypatch.setattr(imminent_mod, "_score_ticker", _fake_score_ticker)
    monkeypatch.setattr(
        imminent_mod,
        "recent_swing_resistance",
        lambda ticker: (_ for _ in ()).throw(
            AssertionError("score resistance should avoid extra fetch")
        ),
    )

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=TEST_USER_ID,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    assert score_modes == [(True, imminent_mod.IMMINENT_SCORE_SKIP_PATTERN_ENGINE)]
    assert meta["score_skip_pattern_engine"] is True
    assert [c["ticker"] for c in candidates] == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert candidates[0]["flat"]["resistance"] == TEST_SCORE_RESISTANCE


def test_gather_imminent_scores_stock_structural_fields(
    db,
    monkeypatch,
) -> None:
    stock_ticker = "STRUCT"
    rules = {
        "conditions": [
            {"indicator": "ibs", "op": "<", "value": 0.2},
            {"indicator": "pullback_stretch_entry", "op": "==", "value": True},
        ]
    }
    pattern = ScanPattern(
        name="Stock structural parity pattern",
        rules_json=rules,
        origin="test",
        asset_class="stocks",
        lifecycle_stage=imminent_mod.PROMOTED_STAGE,
        ticker_scope="explicit_list",
        scope_tickers=f'["{stock_ticker}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add_all(
        [
            pattern,
            PrescreenCandidate(
                ticker=stock_ticker,
                ticker_norm=stock_ticker,
                asset_universe="stock",
                active=True,
                entry_reasons=[],
                sources_json={
                    "tags": [
                        "massive_momentum_gappers",
                        "massive_high_rel_volume",
                    ],
                },
            ),
        ]
    )
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_composite_main",
        TEST_MIN_COMPOSITE_DISABLED,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([stock_ticker], {}),
    )

    def _fake_score_ticker(
        ticker: str,
        skip_fundamentals: bool = True,
        skip_pattern_engine: bool = False,
    ):
        return {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "ibs": TEST_STRUCTURAL_IBS,
                "pullback_stretch_entry": False,
                "atr": TEST_SCORE_ATR,
            },
        }

    monkeypatch.setattr(imminent_mod, "_score_ticker", _fake_score_ticker)
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=TEST_USER_ID,
        equity_session_open=True,
        apply_main_dispatch_filters=True,
    )

    assert [c["ticker"] for c in candidates] == [stock_ticker]
    momentum_context = candidates[0]["score"][
        imminent_mod.SMALL_CAP_MOMENTUM_CONTEXT_KEY
    ]
    assert momentum_context[
        imminent_mod.PRESCREEN_SOURCE_TAGS_CONTEXT_KEY
    ] == [
        "massive_momentum_gappers",
        "massive_high_rel_volume",
    ]
    assert momentum_context["prescreen_source_count"] == 2
    assert momentum_context["prescreen_momentum_gapper"] is True
    assert momentum_context["prescreen_high_relative_volume"] is True
    assert candidates[0]["flat"]["ibs"] == TEST_STRUCTURAL_IBS
    assert candidates[0]["flat"]["pullback_stretch_entry"] is False
    assert imminent_mod.PRESCREEN_SOURCE_TAGS_CONTEXT_KEY not in candidates[0]["flat"]
    assert "prescreen_momentum_gapper" not in candidates[0]["flat"]
    assert meta["skip_reasons"]["readiness_unusable"] == 0
    assert meta["small_cap_momentum_context_prescreen_tagged"] == 1
    assert meta["small_cap_momentum_context_prescreen_tag_lookup_failed"] is False


def test_tickers_for_pattern_treats_stock_aliases_as_session_gated() -> None:
    pattern = ScanPattern(
        name="Stock alias route",
        rules_json={"conditions": []},
        origin="test",
        asset_class="stock",
        ticker_scope="universal",
    )
    universe = ["AAPL", TEST_EXECUTABLE_CRYPTO_TICKER]

    assert imminent_mod._tickers_for_pattern(
        pattern,
        universe,
        equity_open=True,
    ) == ["AAPL"]
    assert imminent_mod._tickers_for_pattern(
        pattern,
        universe,
        equity_open=False,
    ) == []
    assert imminent_mod._tickers_for_pattern(
        pattern,
        universe,
        equity_open=False,
        allow_offsession_stock_shadow=True,
    ) == ["AAPL"]


def test_tickers_for_pattern_treats_options_as_equity_session_gated() -> None:
    pattern = ScanPattern(
        name="Options route",
        rules_json={"conditions": []},
        origin="test",
        asset_class="options",
        ticker_scope="universal",
    )
    universe = ["AAPL", TEST_EXECUTABLE_CRYPTO_TICKER]

    assert imminent_mod._tickers_for_pattern(
        pattern,
        universe,
        equity_open=True,
    ) == ["AAPL"]
    assert imminent_mod._tickers_for_pattern(
        pattern,
        universe,
        equity_open=False,
    ) == []
    assert imminent_mod._tickers_for_pattern(
        pattern,
        universe,
        equity_open=False,
        allow_offsession_stock_shadow=True,
    ) == ["AAPL"]


def test_gather_imminent_routes_offsession_stock_to_shadow_lane(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {"indicator": "rsi_14", "op": ">", "value": TEST_DIAGNOSTIC_RSI_TRIGGER},
            {"indicator": "adx", "op": ">", "value": TEST_DIAGNOSTIC_ADX_TRIGGER},
        ]
    }
    pattern = ScanPattern(
        name="Offsession stock shadow prep",
        rules_json=rules,
        origin="test",
        asset_class="stocks",
        lifecycle_stage=imminent_mod.PROMOTED_STAGE,
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_OFFSESSION_STOCK_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add(pattern)
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_offsession_stock_shadow_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_composite_main",
        TEST_MIN_COMPOSITE_DISABLED,
    )
    monkeypatch.setattr(
        imminent_mod,
        "us_stock_extended_session_open",
        lambda: True,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_OFFSESSION_STOCK_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_score_ticker",
        lambda *args, **kwargs: {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SCORE_RSI,
                "adx": TEST_FAILED_ADX,
                "atr": TEST_SCORE_ATR,
            },
        },
    )
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=TEST_USER_ID,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    assert [c["ticker"] for c in candidates] == [TEST_OFFSESSION_STOCK_TICKER]
    assert candidates[0]["signal_lane"] == (
        imminent_mod.EQUITY_SESSION_SHADOW_SIGNAL_LANE
    )
    assert meta["offsession_stock_shadow_enabled"] is True
    assert meta["offsession_stock_shadow_active"] is True
    assert meta["equity_extended_session_open"] is True
    assert meta["offsession_stock_shadow_admitted"] == 1


def _rearm_candidate(pattern: ScanPattern, signal_lane: str = imminent_mod.STANDARD_SIGNAL_LANE):
    return {
        "pattern": pattern,
        "ticker": TEST_OFFSESSION_STOCK_TICKER,
        "eta_lo": 0.05,
        "eta_hi": 0.25,
        "score": {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["gap", "rvol"],
        },
        "trade_type": "day",
        "duration_estimate": "opening drive",
        "hold_label": "opening drive",
        "composite": 0.92,
        "readiness": 0.55,
        "flat": {"gap_pct": 7.5, "volume_ratio": 3.2, "price": TEST_SCORE_PRICE},
        "score_breakdown": {"quality": 0.92},
        "coverage_ratio": 0.8,
        "signal_lane": signal_lane,
    }


def _recent_imminent_alert_history(db, pattern: ScanPattern) -> None:
    _ensure_test_user(db)
    db.add(
        AlertHistory(
            user_id=TEST_USER_ID,
            alert_type=PATTERN_BREAKOUT_IMMINENT,
            ticker=TEST_OFFSESSION_STOCK_TICKER,
            message="recent shadow cooldown",
            scan_pattern_id=pattern.id,
            success=True,
            created_at=datetime.utcnow() - timedelta(minutes=5),
        )
    )
    db.commit()


def _ensure_test_user(db) -> None:
    if db.get(User, TEST_USER_ID) is not None:
        return
    db.add(
        User(
            id=TEST_USER_ID,
            name="pattern-imminent-test-user",
            email="pattern-imminent-test-user@example.com",
        )
    )
    db.flush()


def _recent_pattern_imminent_breakout(
    db,
    pattern: ScanPattern,
    *,
    signal_lane: str,
) -> None:
    _ensure_test_user(db)
    db.add(
        BreakoutAlert(
            ticker=TEST_OFFSESSION_STOCK_TICKER,
            asset_type="stock",
            alert_tier="pattern_imminent",
            score_at_alert=0.9,
            price_at_alert=TEST_SCORE_PRICE,
            entry_price=TEST_SCORE_PRICE,
            stop_loss=TEST_SCORE_STOP_LOSS,
            target_price=TEST_SCORE_TAKE_PROFIT,
            user_id=TEST_USER_ID,
            scan_pattern_id=pattern.id,
            indicator_snapshot={
                "flat_indicators": {
                    "gap_pct": 7.5,
                    "volume_ratio": 3.2,
                },
                "imminent_scorecard": {"signal_lane": signal_lane},
            },
            alerted_at=datetime.utcnow() - timedelta(minutes=5),
        )
    )
    db.commit()


def test_market_snapshot_context_helper_uses_latest_snapshot_row(db) -> None:
    now = datetime.utcnow()
    ticker = "SNAPCTX"
    older = MarketSnapshot(
        ticker=ticker,
        snapshot_date=now - timedelta(minutes=10),
        bar_start_at=now - timedelta(minutes=15),
        bar_interval="1d",
        close_price=5.0,
        indicator_data={"sector": "Old Sector", "premarket_high": 5.5},
        news_sentiment=-0.1,
        news_count=1,
        market_cap_b=0.01,
    )
    newer = MarketSnapshot(
        ticker=ticker,
        snapshot_date=now - timedelta(minutes=1),
        bar_start_at=now - timedelta(minutes=5),
        bar_interval="1d",
        close_price=4.0,
        indicator_data={"sector": "Healthcare", "premarket_high": 4.8},
        news_sentiment=0.31,
        news_count=7,
        market_cap_b=0.02,
    )
    db.add_all([older, newer])
    db.commit()

    context, meta = imminent_mod._latest_fresh_market_snapshot_context(
        db,
        ticker,
        now=now,
    )

    assert meta["status"] == "fresh"
    assert context["market_snapshot_id"] == newer.id
    assert context["sector"] == "Healthcare"
    assert context["news_sentiment"] == pytest.approx(0.31)
    assert context["news_count"] == pytest.approx(7)
    assert context["market_cap_b"] == pytest.approx(0.02)
    assert context["market_cap"] == pytest.approx(20_000_000.0)
    assert context["float_proxy_shares"] == pytest.approx(5_000_000.0)
    assert context["premarket_high"] == pytest.approx(4.8)
    assert context["context_source"] == "market_snapshot"


def test_market_snapshot_context_merge_overrides_gap_and_preserves_scanner_rvol() -> None:
    context = {
        "gap_pct": -11.49,
        "rvol": 17.05,
        "rel_vol": 17.05,
        "volume_ratio": 17.05,
        "news_count": 0,
    }
    snapshot_context = {
        "context_source": "market_snapshot",
        "market_snapshot_id": 123,
        "gap_pct": 22.9008,
        "rvol": 0.5778,
        "rel_vol": 0.5778,
        "volume_ratio": 0.5778,
        "news_count": 1,
        "news_sentiment": 0.12,
    }

    changed = imminent_mod._merge_market_snapshot_context_into_momentum_context(
        context,
        snapshot_context,
    )

    assert changed is True
    assert context["context_source"] == "market_snapshot"
    assert context["market_snapshot_id"] == 123
    assert context["gap_pct"] == pytest.approx(22.9008)
    assert context["scanner_gap_pct"] == pytest.approx(-11.49)
    assert context["market_snapshot_gap_pct"] == pytest.approx(22.9008)
    assert context["volume_ratio"] == pytest.approx(17.05)
    assert context["rvol"] == pytest.approx(17.05)
    assert context["rel_vol"] == pytest.approx(17.05)
    assert context["market_snapshot_volume_ratio"] == pytest.approx(0.5778)
    assert context["market_snapshot_rvol"] == pytest.approx(0.5778)
    assert context["market_snapshot_rel_vol"] == pytest.approx(0.5778)
    assert context["news_count"] == pytest.approx(1)
    assert context["scanner_news_count"] == pytest.approx(0)
    assert context["news_sentiment"] == pytest.approx(0.12)


def test_market_snapshot_context_merge_uses_snapshot_volume_when_scanner_missing() -> None:
    context = {"gap_pct": 6.0}
    snapshot_context = {
        "context_source": "market_snapshot",
        "volume_ratio": 2.4,
        "rvol": 2.4,
        "rel_vol": 2.4,
    }

    imminent_mod._merge_market_snapshot_context_into_momentum_context(
        context,
        snapshot_context,
    )

    assert context["volume_ratio"] == pytest.approx(2.4)
    assert context["rvol"] == pytest.approx(2.4)
    assert context["rel_vol"] == pytest.approx(2.4)
    assert context["market_snapshot_volume_ratio"] == pytest.approx(2.4)


def test_insert_imminent_breakout_alert_persists_momentum_context_and_alert_columns(
    db,
    monkeypatch,
) -> None:
    _ensure_test_user(db)
    pattern = ScanPattern(
        name="Momentum context persistence",
        rules_json={"conditions": []},
        origin="test",
        asset_class="stocks",
        lifecycle_stage=imminent_mod.PROMOTED_STAGE,
        timeframe="1d",
    )
    db.add(pattern)
    db.commit()
    monkeypatch.setattr(
        "app.services.trading.contracts.signal_emit.emit_signal_for_breakout_alert",
        lambda *args, **kwargs: None,
    )

    context = {
        "gap_pct": 8.5,
        "rvol": 3.2,
        "sector": "Technology",
        "news_sentiment": 0.27,
        "market_cap_b": 0.04,
        "bid": 9.98,
        "ask": 10.02,
        imminent_mod.PRESCREEN_SOURCE_TAGS_CONTEXT_KEY: [
            "massive_momentum_gappers",
            "massive_high_rel_volume",
        ],
        "prescreen_momentum_gapper": True,
        "prescreen_high_relative_volume": True,
    }
    score = {
        "price": 10.0,
        "entry_price": 10.0,
        "stop_loss": 9.0,
        "take_profit": 12.0,
        "signals": ["Gap and RVOL aligned"],
        imminent_mod.SMALL_CAP_MOMENTUM_CONTEXT_KEY: context,
    }
    flat = {
        "price": 10.0,
        "gap_pct": 8.5,
        "volume_ratio": 3.2,
    }

    imminent_mod._insert_imminent_breakout_alert(
        db,
        TEST_USER_ID,
        pattern,
        "CTXMOM",
        score,
        flat,
        composite=0.75,
        score_breakdown={"readiness": 0.5},
        readiness=0.62,
        coverage_ratio=0.8,
        eta_lo=0.1,
        eta_hi=1.0,
        signal_lane=imminent_mod.STANDARD_SIGNAL_LANE,
    )

    row = (
        db.query(BreakoutAlert)
        .filter(BreakoutAlert.ticker == "CTXMOM")
        .order_by(BreakoutAlert.id.desc())
        .first()
    )

    assert row is not None
    persisted = row.indicator_snapshot[imminent_mod.SMALL_CAP_MOMENTUM_CONTEXT_KEY]
    assert persisted["gap_pct"] == pytest.approx(8.5)
    assert persisted["rvol"] == pytest.approx(3.2)
    assert persisted["rel_vol"] == pytest.approx(3.2)
    assert persisted["volume_ratio"] == pytest.approx(3.2)
    assert persisted["sector"] == "Technology"
    assert persisted["news_sentiment"] == pytest.approx(0.27)
    assert persisted["market_cap"] == pytest.approx(40_000_000.0)
    assert persisted["float_proxy_shares"] == pytest.approx(4_000_000.0)
    assert persisted["float_bucket"] == "micro_float_proxy"
    assert persisted["spread_bps"] == pytest.approx(40.0)
    assert persisted[imminent_mod.PRESCREEN_SOURCE_TAGS_CONTEXT_KEY] == [
        "massive_momentum_gappers",
        "massive_high_rel_volume",
    ]
    assert persisted["prescreen_momentum_gapper"] is True
    assert persisted["prescreen_high_relative_volume"] is True
    assert row.signals_snapshot[imminent_mod.SMALL_CAP_MOMENTUM_CONTEXT_KEY] == persisted
    assert row.sector == "Technology"
    assert row.news_sentiment_at_alert == pytest.approx(0.27)


def test_run_pattern_imminent_scan_rearms_equity_shadow_after_rth_open(
    db,
    monkeypatch,
) -> None:
    pattern = ScanPattern(
        name="RTH rearm stock",
        rules_json={"conditions": []},
        origin="test",
        asset_class="stocks",
        lifecycle_stage=imminent_mod.PROMOTED_STAGE,
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_OFFSESSION_STOCK_TICKER}"]',
    )
    db.add(pattern)
    db.commit()
    _recent_imminent_alert_history(db, pattern)
    _recent_pattern_imminent_breakout(
        db,
        pattern,
        signal_lane=imminent_mod.EQUITY_SESSION_SHADOW_SIGNAL_LANE,
    )

    inserted: list[str] = []
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_max_per_run", 1)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_cooldown_hours", 3.0)
    monkeypatch.setattr(
        "app.services.trading.brain_neural_mesh.publisher.publish_imminent_eval",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        imminent_mod,
        "gather_imminent_candidate_rows",
        lambda *args, **kwargs: (
            [_rearm_candidate(pattern)],
            {"patterns_active": 1, "tickers_scored": 1},
        ),
    )
    monkeypatch.setattr(imminent_mod, "dispatch_alert", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        imminent_mod,
        "_insert_imminent_breakout_alert",
        lambda db, user_id, pat, ticker, *args, **kwargs: inserted.append(
            kwargs["signal_lane"]
        ),
    )

    result = run_pattern_imminent_scan(
        db,
        user_id=TEST_USER_ID,
        equity_session_open=True,
    )

    assert inserted == [imminent_mod.STANDARD_SIGNAL_LANE]
    assert result["alerts_sent"] == 1
    assert result["cooldown_skipped"] == 0
    assert result["equity_shadow_rearm_cooldown_bypassed"] == 1


def test_run_pattern_imminent_scan_keeps_non_shadow_cooldown_block(
    db,
    monkeypatch,
) -> None:
    pattern = ScanPattern(
        name="RTH no rearm stock",
        rules_json={"conditions": []},
        origin="test",
        asset_class="stocks",
        lifecycle_stage=imminent_mod.PROMOTED_STAGE,
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_OFFSESSION_STOCK_TICKER}"]',
    )
    db.add(pattern)
    db.commit()
    _recent_imminent_alert_history(db, pattern)

    inserted: list[str] = []
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_max_per_run", 1)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_cooldown_hours", 3.0)
    monkeypatch.setattr(
        imminent_mod,
        "gather_imminent_candidate_rows",
        lambda *args, **kwargs: (
            [_rearm_candidate(pattern)],
            {"patterns_active": 1, "tickers_scored": 1},
        ),
    )
    monkeypatch.setattr(imminent_mod, "dispatch_alert", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        imminent_mod,
        "_insert_imminent_breakout_alert",
        lambda *args, **kwargs: inserted.append(kwargs["signal_lane"]),
    )

    result = run_pattern_imminent_scan(
        db,
        user_id=TEST_USER_ID,
        equity_session_open=True,
    )

    assert inserted == []
    assert result["alerts_sent"] == 0
    assert result["cooldown_skipped"] == 1
    assert result["equity_shadow_rearm_cooldown_bypassed"] == 0


def test_run_pattern_imminent_scan_does_not_rearm_duplicate_standard_alert(
    db,
    monkeypatch,
) -> None:
    pattern = ScanPattern(
        name="RTH duplicate standard stock",
        rules_json={"conditions": []},
        origin="test",
        asset_class="stocks",
        lifecycle_stage=imminent_mod.PROMOTED_STAGE,
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_OFFSESSION_STOCK_TICKER}"]',
    )
    db.add(pattern)
    db.commit()
    _recent_imminent_alert_history(db, pattern)
    _recent_pattern_imminent_breakout(
        db,
        pattern,
        signal_lane=imminent_mod.EQUITY_SESSION_SHADOW_SIGNAL_LANE,
    )
    _recent_pattern_imminent_breakout(
        db,
        pattern,
        signal_lane=imminent_mod.STANDARD_SIGNAL_LANE,
    )

    inserted: list[str] = []
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_max_per_run", 1)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_cooldown_hours", 3.0)
    monkeypatch.setattr(
        imminent_mod,
        "gather_imminent_candidate_rows",
        lambda *args, **kwargs: (
            [_rearm_candidate(pattern)],
            {"patterns_active": 1, "tickers_scored": 1},
        ),
    )
    monkeypatch.setattr(imminent_mod, "dispatch_alert", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        imminent_mod,
        "_insert_imminent_breakout_alert",
        lambda *args, **kwargs: inserted.append(kwargs["signal_lane"]),
    )

    result = run_pattern_imminent_scan(
        db,
        user_id=TEST_USER_ID,
        equity_session_open=True,
    )

    assert inserted == []
    assert result["alerts_sent"] == 0
    assert result["cooldown_skipped"] == 1
    assert result["equity_shadow_rearm_cooldown_bypassed"] == 0


def test_gather_imminent_can_keep_offsession_stock_shadow_disabled(
    db,
    monkeypatch,
) -> None:
    pattern = ScanPattern(
        name="Offsession stock shadow disabled",
        rules_json={
            "conditions": [
                {
                    "indicator": "rsi_14",
                    "op": ">",
                    "value": TEST_DIAGNOSTIC_RSI_TRIGGER,
                }
            ]
        },
        origin="test",
        asset_class="stocks",
        lifecycle_stage=imminent_mod.PROMOTED_STAGE,
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_OFFSESSION_STOCK_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add(pattern)
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_offsession_stock_shadow_enabled",
        False,
    )
    monkeypatch.setattr(
        imminent_mod,
        "us_stock_extended_session_open",
        lambda: True,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_OFFSESSION_STOCK_TICKER], {}),
    )
    score_calls: list[str] = []
    monkeypatch.setattr(
        imminent_mod,
        "_score_ticker",
        lambda ticker, **_kwargs: score_calls.append(ticker) or None,
    )

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=TEST_USER_ID,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    assert candidates == []
    assert score_calls == []
    assert meta["offsession_stock_shadow_enabled"] is False
    assert meta["offsession_stock_shadow_active"] is False
    assert meta["skip_reasons"]["pattern_no_tickers"] == 1


def test_gather_imminent_blocks_offsession_stock_shadow_when_extended_closed(
    db,
    monkeypatch,
) -> None:
    pattern = ScanPattern(
        name="Offsession stock shadow fully closed",
        rules_json={
            "conditions": [
                {
                    "indicator": "rsi_14",
                    "op": ">",
                    "value": TEST_DIAGNOSTIC_RSI_TRIGGER,
                }
            ]
        },
        origin="test",
        asset_class="stocks",
        lifecycle_stage=imminent_mod.PROMOTED_STAGE,
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_OFFSESSION_STOCK_TICKER}"]',
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add(pattern)
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_offsession_stock_shadow_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod,
        "us_stock_extended_session_open",
        lambda: False,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_OFFSESSION_STOCK_TICKER], {}),
    )
    score_calls: list[str] = []
    monkeypatch.setattr(
        imminent_mod,
        "_score_ticker",
        lambda ticker, **_kwargs: score_calls.append(ticker) or None,
    )

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=TEST_USER_ID,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    assert candidates == []
    assert score_calls == []
    assert meta["offsession_stock_shadow_enabled"] is True
    assert meta["offsession_stock_shadow_active"] is False
    assert meta["equity_extended_session_open"] is False
    assert meta["skip_reasons"]["pattern_no_tickers"] == 1


def test_gather_imminent_caps_tickers_per_pattern(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {"indicator": "rsi_14", "op": ">", "value": TEST_RSI_BREAKOUT_TRIGGER}
        ]
    }
    pattern = ScanPattern(
        name="Per-pattern ticker cap",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        lifecycle_stage="promoted",
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add(pattern)
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_max_tickers_per_pattern",
        TEST_PER_PATTERN_TICKER_CAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: ([TEST_EXECUTABLE_CRYPTO_TICKER, TEST_CACHE_TICKER], {}),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_EXECUTABLE_CRYPTO_TICKER, TEST_CACHE_TICKER}),
    )
    score_calls: list[str] = []

    def _fake_score_ticker(
        ticker: str,
        skip_fundamentals: bool = True,
        skip_pattern_engine: bool = False,
    ):
        score_calls.append(ticker)
        return {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SCORE_RSI,
                "adx": TEST_SCORE_ADX,
                "atr": TEST_SCORE_ATR,
            },
        }

    monkeypatch.setattr(imminent_mod, "_score_ticker", _fake_score_ticker)
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=1,
        equity_session_open=False,
    )

    assert score_calls == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert [c["ticker"] for c in candidates] == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert meta["per_pattern_ticker_cap"] == TEST_PER_PATTERN_TICKER_CAP


def test_gather_imminent_deflects_open_position_before_ticker_cap(
    db,
    monkeypatch,
) -> None:
    rules = {
        "conditions": [
            {"indicator": "rsi_14", "op": ">", "value": TEST_RSI_BREAKOUT_TRIGGER}
        ]
    }
    pattern = ScanPattern(
        name="Open-position deflection",
        rules_json=rules,
        origin="test",
        asset_class="crypto",
        ticker_scope="explicit_list",
        scope_tickers=f'["{TEST_OPEN_POSITION_TICKER}", "{TEST_EXECUTABLE_CRYPTO_TICKER}"]',
        lifecycle_stage="promoted",
        avg_return_pct=TEST_PATTERN_AVG_RETURN_PCT,
        win_rate=TEST_PATTERN_WIN_RATE,
        evidence_count=TEST_PATTERN_EVIDENCE_COUNT,
    )
    db.add(pattern)
    db.flush()
    db.add(
        Trade(
            user_id=TEST_USER_ID,
            ticker=TEST_OPEN_POSITION_TICKER,
            direction="long",
            entry_price=TEST_SCORE_PRICE,
            quantity=TEST_POSITION_QUANTITY,
            status=imminent_mod.AUTOTRADER_POSITION_DEFLECTION_OPEN_STATUS,
            scan_pattern_id=pattern.id,
            auto_trader_version=imminent_mod.AUTOTRADER_POSITION_DEFLECTION_VERSION,
        )
    )
    db.commit()

    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_max_tickers_per_pattern",
        TEST_PER_PATTERN_TICKER_CAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_open_position_deflection_enabled",
        True,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_readiness",
        TEST_MIN_READINESS,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_readiness_cap",
        TEST_FULL_READINESS_CAP,
    )
    monkeypatch.setattr(
        imminent_mod.settings,
        "pattern_imminent_min_composite_main",
        TEST_MIN_COMPOSITE_DISABLED,
    )
    monkeypatch.setattr(
        imminent_mod,
        "build_imminent_ticker_universe",
        lambda *args, **kwargs: (
            [TEST_OPEN_POSITION_TICKER, TEST_EXECUTABLE_CRYPTO_TICKER],
            {},
        ),
    )
    monkeypatch.setattr(
        imminent_mod,
        "_coinbase_spot_ticker_set",
        lambda: frozenset({TEST_OPEN_POSITION_TICKER, TEST_EXECUTABLE_CRYPTO_TICKER}),
    )
    score_calls: list[str] = []

    def _fake_score_ticker(
        ticker: str,
        skip_fundamentals: bool = True,
        skip_pattern_engine: bool = False,
    ):
        score_calls.append(ticker)
        return {
            "price": TEST_SCORE_PRICE,
            "entry_price": TEST_SCORE_PRICE,
            "stop_loss": TEST_SCORE_STOP_LOSS,
            "take_profit": TEST_SCORE_TAKE_PROFIT,
            "signals": ["test"],
            "indicators": {
                "rsi": TEST_SCORE_RSI,
                "adx": TEST_SCORE_ADX,
                "atr": TEST_SCORE_ATR,
            },
        }

    monkeypatch.setattr(imminent_mod, "_score_ticker", _fake_score_ticker)
    monkeypatch.setattr(imminent_mod, "recent_swing_resistance", lambda ticker: None)

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id=TEST_USER_ID,
        equity_session_open=False,
        apply_main_dispatch_filters=True,
    )

    assert score_calls == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert [c["ticker"] for c in candidates] == [TEST_EXECUTABLE_CRYPTO_TICKER]
    assert meta["skip_reasons"]["open_position_deflected"] == 1
    assert meta["open_position_deflection_keys"] == 1
    assert meta["top_suppressed"][0]["reason"] == "open_position_deflected"


def test_run_pattern_imminent_scan_counts_persisted_alert_when_delivery_fails(
    db,
    monkeypatch,
) -> None:
    inserted: list[tuple[str, int]] = []

    pattern = SimpleNamespace(
        id=52,
        name="Promoted VCP",
        description="Test imminent pattern",
    )
    candidate = {
        "pattern": pattern,
        "ticker": "AAOI",
        "eta_lo": 0.5,
        "eta_hi": 1.0,
        "score": {
            "price": 100.0,
            "entry_price": 101.0,
            "stop_loss": 97.5,
            "take_profit": 110.0,
            "signals": ["Tight range", "Volume building"],
        },
        "trade_type": "swing",
        "duration_estimate": "2-5 days",
        "hold_label": "2-5 days",
        "composite": 0.71,
        "readiness": 0.82,
        "flat": {"price": 100.0},
        "score_breakdown": {"quality": 0.7},
        "coverage_ratio": 0.75,
    }

    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts.gather_imminent_candidate_rows",
        lambda *args, **kwargs: ([candidate], {"patterns_active": 1, "tickers_scored": 1}),
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts._cooldown_active",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts.dispatch_alert",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts._insert_imminent_breakout_alert",
        lambda db, user_id, pat, ticker, *args, **kwargs: inserted.append((ticker, pat.id)),
    )

    result = run_pattern_imminent_scan(db, user_id=1)

    assert result["candidates"] == 1
    assert result["alerts_sent"] == 1
    assert result["delivery_failed"] == 1
    assert inserted == [("AAOI", 52)]


def test_run_pattern_imminent_scan_reserves_shadow_observation_slots(
    db,
    monkeypatch,
) -> None:
    inserted: list[tuple[str, int, str]] = []

    def _candidate(pattern_id: int, ticker: str, composite: float, stage: str):
        pattern = SimpleNamespace(
            id=pattern_id,
            name=f"Pattern {pattern_id}",
            description="Test imminent pattern",
            lifecycle_stage=stage,
        )
        return {
            "pattern": pattern,
            "ticker": ticker,
            "eta_lo": 0.5,
            "eta_hi": 1.0,
            "score": {
                "price": 100.0,
                "entry_price": 101.0,
                "stop_loss": 97.5,
                "take_profit": 110.0,
                "signals": ["Tight range", "Volume building"],
            },
            "trade_type": "swing",
            "duration_estimate": "2-5 days",
            "hold_label": "2-5 days",
            "composite": composite,
            "readiness": 0.82,
            "flat": {"price": 100.0},
            "score_breakdown": {"quality": composite},
            "coverage_ratio": 0.75,
        }

    candidates = [
        _candidate(11, "MAIN1", 0.91, "promoted"),
        _candidate(12, "MAIN2", 0.90, "promoted"),
        _candidate(99, "SHADOW", 0.62, "shadow_promoted"),
    ]

    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_max_per_run", 1)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_observation_enabled", True)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_reserve_per_run", 1)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_extra_per_run", 1)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_max_per_ticker_per_run", 2)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_max_per_pattern_per_run", 2)
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts.gather_imminent_candidate_rows",
        lambda *args, **kwargs: (
            candidates,
            {"patterns_active": 3, "tickers_scored": 3},
        ),
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts._cooldown_active",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts.dispatch_alert",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts._insert_imminent_breakout_alert",
        lambda db, user_id, pat, ticker, *args, **kwargs: inserted.append(
            (ticker, pat.id, pat.lifecycle_stage)
        ),
    )

    result = run_pattern_imminent_scan(db, user_id=1)

    assert result["alerts_sent"] == 2
    assert result["main_alerts_sent"] == 1
    assert result["shadow_alerts_sent"] == 1
    assert inserted[0] == ("SHADOW", 99, "shadow_promoted")
    assert ("MAIN1", 11, "promoted") in inserted


def test_run_pattern_imminent_scan_reserves_signal_lane_observation_slots(
    db,
    monkeypatch,
) -> None:
    inserted: list[tuple[str, int, str, str]] = []

    def _candidate(
        pattern_id: int,
        ticker: str,
        composite: float,
        stage: str,
        signal_lane: str = imminent_mod.STANDARD_SIGNAL_LANE,
    ):
        pattern = SimpleNamespace(
            id=pattern_id,
            name=f"Pattern {pattern_id}",
            description="Test imminent pattern",
            lifecycle_stage=stage,
        )
        return {
            "pattern": pattern,
            "ticker": ticker,
            "eta_lo": 0.5,
            "eta_hi": 1.0,
            "score": {
                "price": 100.0,
                "entry_price": 101.0,
                "stop_loss": 97.5,
                "take_profit": 110.0,
                "signals": ["Tight range", "Volume building"],
            },
            "trade_type": "swing",
            "duration_estimate": "2-5 days",
            "hold_label": "2-5 days",
            "composite": composite,
            "readiness": 0.44,
            "flat": {"price": 100.0},
            "score_breakdown": {"quality": composite},
            "coverage_ratio": 0.75,
            "signal_lane": signal_lane,
        }

    candidates = [
        _candidate(21, "MAIN", 0.91, "promoted"),
        _candidate(
            22,
            "PILOTNEAR",
            0.62,
            "pilot_promoted",
            imminent_mod.SHADOW_NEAR_MISS_SIGNAL_LANE,
        ),
    ]

    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_max_per_run", 1)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_observation_enabled", True)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_reserve_per_run", 1)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_extra_per_run", 1)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_max_per_ticker_per_run", 2)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_max_per_pattern_per_run", 2)
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts.gather_imminent_candidate_rows",
        lambda *args, **kwargs: (
            candidates,
            {"patterns_active": 2, "tickers_scored": 2},
        ),
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts._cooldown_active",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts.dispatch_alert",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts._insert_imminent_breakout_alert",
        lambda db, user_id, pat, ticker, *args, **kwargs: inserted.append(
            (ticker, pat.id, pat.lifecycle_stage, kwargs["signal_lane"])
        ),
    )

    result = run_pattern_imminent_scan(db, user_id=1)

    assert result["alerts_sent"] == 2
    assert result["main_alerts_sent"] == 1
    assert result["shadow_alerts_sent"] == 1
    assert inserted[0] == (
        "PILOTNEAR",
        22,
        "pilot_promoted",
        imminent_mod.SHADOW_NEAR_MISS_SIGNAL_LANE,
    )
    assert ("MAIN", 21, "promoted", imminent_mod.STANDARD_SIGNAL_LANE) in inserted


def test_run_pattern_imminent_scan_reserves_hard_recert_observation_slots(
    db,
    monkeypatch,
) -> None:
    inserted: list[tuple[str, int, str, str, list[str]]] = []

    def _candidate(
        pattern_id: int,
        ticker: str,
        composite: float,
        signal_lane: str = imminent_mod.STANDARD_SIGNAL_LANE,
        hard_recert_reasons: list[str] | None = None,
    ):
        pattern = SimpleNamespace(
            id=pattern_id,
            name=f"Pattern {pattern_id}",
            description="Test imminent pattern",
            lifecycle_stage="promoted",
        )
        row = {
            "pattern": pattern,
            "ticker": ticker,
            "eta_lo": 0.5,
            "eta_hi": 1.0,
            "score": {
                "price": 100.0,
                "entry_price": 101.0,
                "stop_loss": 97.5,
                "take_profit": 110.0,
                "signals": ["Tight range", "Volume building"],
            },
            "trade_type": "swing",
            "duration_estimate": "2-5 days",
            "hold_label": "2-5 days",
            "composite": composite,
            "readiness": 0.82,
            "flat": {"price": 100.0},
            "score_breakdown": {"quality": composite},
            "coverage_ratio": 0.75,
            "signal_lane": signal_lane,
        }
        if hard_recert_reasons:
            row["hard_recert_reasons"] = hard_recert_reasons
        return row

    candidates = [
        _candidate(
            31,
            "HARDRECERT",
            0.92,
            imminent_mod.HARD_RECERT_SHADOW_SIGNAL_LANE,
            [TEST_HARD_RECERT_REASON],
        ),
        _candidate(32, "MAIN", 0.91),
    ]

    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_max_per_run", 1)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_observation_enabled", True)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_reserve_per_run", 1)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_extra_per_run", 1)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_max_per_ticker_per_run", 2)
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_shadow_max_per_pattern_per_run", 2)
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts.gather_imminent_candidate_rows",
        lambda *args, **kwargs: (
            candidates,
            {"patterns_active": 2, "tickers_scored": 2},
        ),
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts._cooldown_active",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts.dispatch_alert",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts._insert_imminent_breakout_alert",
        lambda db, user_id, pat, ticker, *args, **kwargs: inserted.append(
            (
                ticker,
                pat.id,
                pat.lifecycle_stage,
                kwargs["signal_lane"],
                kwargs["hard_recert_reasons"],
            )
        ),
    )

    result = run_pattern_imminent_scan(db, user_id=1)

    assert result["alerts_sent"] == 2
    assert result["main_alerts_sent"] == 1
    assert result["shadow_alerts_sent"] == 1
    assert inserted[0] == (
        "HARDRECERT",
        31,
        "promoted",
        imminent_mod.HARD_RECERT_SHADOW_SIGNAL_LANE,
        [TEST_HARD_RECERT_REASON],
    )
    assert ("MAIN", 32, "promoted", imminent_mod.STANDARD_SIGNAL_LANE, []) in inserted
