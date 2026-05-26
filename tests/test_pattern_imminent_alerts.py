"""Tests for ScanPattern imminent breakout alert helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.models.trading import AlertHistory, AutoTraderRun, ScanPattern
from app.services.trading import pattern_imminent_alerts as imminent_mod
from app.services.trading.alerts import PATTERN_BREAKOUT_IMMINENT
from app.services.trading.pattern_imminent_alerts import (
    _cooldown_active,
    _shadow_poor_edge_pattern_ids,
    estimate_breakout_eta_hours,
    evaluate_imminent_readiness,
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
TEST_SCORE_RSI = 55.0
TEST_SCORE_ADX = 20.0
TEST_SCORE_ATR = 2.0
TEST_RSI_BREAKOUT_TRIGGER = 60
TEST_EXECUTABLE_CRYPTO_TICKER = "GOOD-USD"
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

    cooldown_ids, counts = _shadow_poor_edge_pattern_ids(
        db,
        [poor, healthy],
        user_id=1,
    )

    assert int(poor.id) in cooldown_ids
    assert int(healthy.id) not in cooldown_ids
    assert counts[int(poor.id)] == TEST_POOR_EDGE_MIN_REJECTS


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
    monkeypatch.setattr(imminent_mod.settings, "pattern_imminent_min_composite_main", 0.0)
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
        lambda ticker, skip_fundamentals=True: {
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

    def _fake_score_ticker(ticker: str, skip_fundamentals: bool = True):
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

    def _fake_score_ticker(ticker: str, skip_fundamentals: bool = True):
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

    def _failing_score_ticker(ticker: str, skip_fundamentals: bool = True):
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

    def _fake_score_ticker(ticker: str, skip_fundamentals: bool = True):
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

    def _fake_score_ticker(ticker: str, skip_fundamentals: bool = True):
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
