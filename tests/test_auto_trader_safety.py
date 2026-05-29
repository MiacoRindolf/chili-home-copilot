"""Phase 1 safety hardening tests for the AutoTrader v1 live path.

Covers:
  * P0.2 — advisory-lock claim prevents a same-tick duplicate submission
    when an alert is observed twice. (Race between two ticks is hard to
    exercise deterministically in one process; we validate the primitive.)
  * P0.5 — kill switch flipped mid-flight (after initial gate, before
    placement) is honoured — no broker call is made and the audit reason
    is recorded.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import models
from app.config import (
    AUTOTRADER_SHADOW_OBSERVATION_DIAGNOSTIC_SIZING_DEFAULT_ENABLED,
    AUTOTRADER_SHADOW_OBSERVATION_EVIDENCE_NOTIONAL_DEFAULT_USD,
    AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_BACKTEST_PRIORITY,
    AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_LIFECYCLE_STAGES,
    AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_MIN_EXPECTED_NET_PCT,
    AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_REBOOST_COOLDOWN_MINUTES,
    AUTOTRADER_NON_STOCK_CANDIDATE_MAX_AGE_DEFAULT_MINUTES,
    AUTOTRADER_STOCK_CANDIDATE_MAX_AGE_DEFAULT_MINUTES,
    AUTOTRADER_STALE_CANDIDATE_SWEEP_DEFAULT_SECONDS,
    AUTOTRADER_STOCK_SESSION_DEFER_DEFAULT_MAX_AGE_HOURS,
    AUTOTRADER_MAX_TICK_MAX_SECONDS,
    AUTOTRADER_MIN_TICK_MAX_SECONDS,
)
from app.models.trading import (
    AutoTraderRun,
    BreakoutAlert,
    PatternRecertLog,
    ScanPattern,
    Trade,
)
from app.services.trading import auto_trader as at_mod

TEST_ENTRY_PRICE = 50.0
TEST_STOP_PRICE = 48.0
TEST_TARGET_PRICE = 55.0
TEST_RISK_NOTIONAL = 100.0
TEST_ACCOUNT_EQUITY = 10_000.0
TEST_PROBATION_MULTIPLIER = 0.25
TEST_PROBATION_MAX_PER_PATTERN = 1
TEST_PROBATION_MAX_PORTFOLIO = 3
TEST_CPCV_PATHS = 35
TEST_STRONG_CPCV_SHARPE = 1.4
TEST_REALIZED_TRADE_COUNT = 20
TEST_REALIZED_AVG_RETURN_PCT = 1.2
TEST_POSITIVE_EXPECTED_NET_PCT = 1.25
STALE_ALERT_MARGIN = timedelta(minutes=1)


def _minimal_settings(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        chili_autotrader_enabled=True,
        chili_autotrader_live_enabled=True,
        chili_autotrader_user_id=user_id,
        brain_default_user_id=user_id,
        chili_autotrader_llm_revalidation_enabled=False,
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=0.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_max_entry_slippage_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_max_concurrent=5,
        chili_autotrader_per_trade_notional_usd=0.0,
        chili_autotrader_per_trade_risk_pct=1.0,
        chili_autotrader_stock_session_defer_enabled=True,
        chili_autotrader_stock_session_defer_max_age_hours=(
            AUTOTRADER_STOCK_SESSION_DEFER_DEFAULT_MAX_AGE_HOURS
        ),
        chili_autotrader_tick_max_seconds=AUTOTRADER_MIN_TICK_MAX_SECONDS,
        chili_autotrader_stale_candidate_sweep_interval_seconds=(
            AUTOTRADER_STALE_CANDIDATE_SWEEP_DEFAULT_SECONDS
        ),
        chili_autotrader_non_stock_candidate_max_age_minutes=(
            AUTOTRADER_NON_STOCK_CANDIDATE_MAX_AGE_DEFAULT_MINUTES
        ),
        chili_autotrader_stock_candidate_max_age_minutes=(
            AUTOTRADER_STOCK_CANDIDATE_MAX_AGE_DEFAULT_MINUTES
        ),
        chili_autotrader_synergy_enabled=False,
        chili_autotrader_synergy_scale_notional_usd=0.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_paper_shadow_reject_lightweight_sizing_enabled=True,
        chili_feature_parity_enabled=False,
        chili_autotrader_live_require_feature_parity=False,
        chili_autotrader_live_require_venue_health_enabled=False,
        chili_autotrader_recert_signal_fastlane_enabled=True,
        chili_autotrader_shadow_stock_fastlane_enabled=True,
        chili_autotrader_shadow_stock_fastlane_backtest_priority=(
            AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_BACKTEST_PRIORITY
        ),
        chili_autotrader_shadow_stock_fastlane_min_expected_net_pct=(
            AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_MIN_EXPECTED_NET_PCT
        ),
        chili_autotrader_shadow_stock_fastlane_lifecycle_stages=(
            AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_LIFECYCLE_STAGES
        ),
        chili_autotrader_shadow_stock_fastlane_reboost_cooldown_minutes=(
            AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_REBOOST_COOLDOWN_MINUTES
        ),
        chili_autotrader_shadow_observation_diagnostic_sizing_enabled=(
            AUTOTRADER_SHADOW_OBSERVATION_DIAGNOSTIC_SIZING_DEFAULT_ENABLED
        ),
        chili_autotrader_shadow_observation_evidence_notional_usd=(
            AUTOTRADER_SHADOW_OBSERVATION_EVIDENCE_NOTIONAL_DEFAULT_USD
        ),
        brain_recert_queue_mode="shadow",
        chili_autotrader_probation_live_enabled=True,
        chili_autotrader_probation_notional_multiplier=TEST_PROBATION_MULTIPLIER,
        chili_autotrader_probation_max_trades_per_pattern_per_day=(
            TEST_PROBATION_MAX_PER_PATTERN
        ),
        chili_autotrader_probation_max_trades_per_day=TEST_PROBATION_MAX_PORTFOLIO,
        chili_autotrader_probation_min_cpcv_sharpe=TEST_STRONG_CPCV_SHARPE,
        chili_autotrader_probation_min_realized_trades=TEST_REALIZED_TRADE_COUNT,
    )


def _live_runtime() -> dict:
    return {
        "tick_allowed": True,
        "paused": False,
        "live_orders_effective": True,
        "live_orders_env": True,
        "desk_live_override": False,
        "monitor_entries_allowed": True,
        "payload": {},
    }


def _patch_tick_shell(monkeypatch, user_id: int) -> None:
    monkeypatch.setattr(at_mod, "settings", _minimal_settings(user_id))
    monkeypatch.setattr(at_mod, "_last_stale_candidate_sweep_at", 0.0)
    monkeypatch.setattr(
        at_mod,
        "effective_autotrader_runtime",
        lambda _db: _live_runtime(),
    )

    from app.services.trading import governance

    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: False)


def _audit_only_process(processed: list[str]):
    def _process(db_, uid_, alert, out, _runtime):
        processed.append(alert.ticker)
        at_mod._audit(
            db_,
            user_id=uid_,
            alert=alert,
            decision="skipped",
            reason="test_processed",
        )
        out["skipped"] += 1
        at_mod._autotrader_tick_note(
            out,
            kind="skipped",
            reason="test_processed",
            alert=alert,
        )

    return _process


def test_autotrader_tick_soft_budget_clamps_to_config_bounds(monkeypatch):
    settings = _minimal_settings(user_id=1)

    settings.chili_autotrader_tick_max_seconds = AUTOTRADER_MIN_TICK_MAX_SECONDS - 1
    monkeypatch.setattr(at_mod, "settings", settings)
    assert at_mod._autotrader_tick_soft_budget_seconds() == AUTOTRADER_MIN_TICK_MAX_SECONDS

    settings.chili_autotrader_tick_max_seconds = AUTOTRADER_MAX_TICK_MAX_SECONDS + 1
    assert at_mod._autotrader_tick_soft_budget_seconds() == AUTOTRADER_MAX_TICK_MAX_SECONDS


def test_recent_live_exit_cooldown_blocks_stock_reentry(db, monkeypatch):
    user = models.User(name="recent_live_exit_cooldown")
    db.add(user)
    db.flush()
    monkeypatch.setattr(at_mod, "settings", _minimal_settings(user.id))
    now = datetime.utcnow()
    pattern = ScanPattern(name="Churn guard", rules_json={}, active=True)
    db.add(pattern)
    db.flush()
    trade = Trade(
        user_id=user.id,
        ticker="CHURN",
        direction="long",
        status="closed",
        broker_source="robinhood",
        quantity=1.0,
        entry_price=10.0,
        exit_price=9.8,
        pnl=-0.2,
        entry_date=now - timedelta(minutes=10),
        exit_date=now - timedelta(minutes=5),
        exit_reason="stop",
        scan_pattern_id=pattern.id,
    )
    alert = BreakoutAlert(
        user_id=user.id,
        ticker="CHURN",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.9,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=12.0,
        scan_pattern_id=pattern.id,
        alerted_at=now,
    )
    db.add_all([trade, alert])
    db.flush()

    snap = at_mod._recent_live_exit_cooldown_snapshot(
        db,
        user_id=user.id,
        alert=alert,
        now=now,
    )

    assert snap is not None
    assert snap["recent_exit_trade_id"] == trade.id
    assert snap["recent_exit_scan_pattern_id"] == pattern.id
    assert snap["cooldown_policy"] == "stop_reentry"


def test_closed_stock_session_defers_stock_without_starving_crypto(db, monkeypatch):
    user = models.User(name="stock_defer_closed")
    db.add(user)
    db.flush()
    now = datetime.utcnow()
    stock_alert = BreakoutAlert(
        ticker="AAPL",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=190.0,
        entry_price=190.0,
        stop_loss=185.0,
        target_price=200.0,
        user_id=user.id,
        alerted_at=now,
    )
    crypto_alert = BreakoutAlert(
        ticker="BTC-USD",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=100_000.0,
        entry_price=100_000.0,
        stop_loss=99_000.0,
        target_price=103_000.0,
        user_id=user.id,
        alerted_at=now,
    )
    db.add_all([stock_alert, crypto_alert])
    db.commit()

    _patch_tick_shell(monkeypatch, user.id)
    defer_hours = AUTOTRADER_STOCK_SESSION_DEFER_DEFAULT_MAX_AGE_HOURS
    monkeypatch.setattr(
        at_mod,
        "_stock_session_defer_state",
        lambda: {
            "enabled": True,
            "active": True,
            "reason": at_mod.STOCK_SESSION_DEFER_REASON_CLOSED,
            "max_age_hours": defer_hours,
            "cutoff": now - timedelta(hours=defer_hours),
        },
    )
    processed: list[str] = []
    monkeypatch.setattr(at_mod, "_process_one_alert", _audit_only_process(processed))

    out = at_mod.run_auto_trader_tick(db)

    assert out["ok"] is True
    assert processed == ["BTC-USD"]
    assert out["stock_session_defer_active"] is True
    assert out["stock_session_defer_counts_checked"] is True
    assert out["stock_session_deferred_pool"] == 1
    assert (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == stock_alert.id)
        .count()
        == 0
    )
    assert (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == crypto_alert.id)
        .count()
        == 1
    )


def test_stale_deferred_stock_alerts_do_not_block_fresh_session_open(db, monkeypatch):
    user = models.User(name="stock_defer_stale")
    db.add(user)
    db.flush()
    now = datetime.utcnow()
    defer_hours = AUTOTRADER_STOCK_SESSION_DEFER_DEFAULT_MAX_AGE_HOURS
    stale_alert = BreakoutAlert(
        ticker="OLD",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=50.0,
        entry_price=50.0,
        stop_loss=48.0,
        target_price=55.0,
        user_id=user.id,
        alerted_at=now - timedelta(hours=defer_hours) - STALE_ALERT_MARGIN,
    )
    moderately_stale_alert = BreakoutAlert(
        ticker="MODERATE",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=50.0,
        entry_price=50.0,
        stop_loss=48.0,
        target_price=55.0,
        user_id=user.id,
        alerted_at=now - timedelta(hours=2),
    )
    fresh_alert = BreakoutAlert(
        ticker="FRESH",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=50.0,
        entry_price=50.0,
        stop_loss=48.0,
        target_price=55.0,
        user_id=user.id,
        alerted_at=now,
    )
    db.add_all([stale_alert, moderately_stale_alert, fresh_alert])
    db.commit()

    _patch_tick_shell(monkeypatch, user.id)
    monkeypatch.setattr(
        at_mod,
        "_stock_session_defer_state",
        lambda: {
            "enabled": True,
            "active": False,
            "reason": "stock_session_open",
            "max_age_hours": defer_hours,
            "cutoff": now - timedelta(hours=defer_hours),
        },
    )
    processed: list[str] = []
    monkeypatch.setattr(at_mod, "_process_one_alert", _audit_only_process(processed))

    out = at_mod.run_auto_trader_tick(db)

    assert out["ok"] is True
    assert processed == ["FRESH"]
    assert out["stock_session_defer_active"] is False
    assert out["stock_session_defer_counts_checked"] is False
    assert out["stock_session_stale_unprocessed"] == 0
    assert (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == moderately_stale_alert.id)
        .count()
        == 0
    )
    assert (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == stale_alert.id)
        .count()
        == 0
    )
    assert (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == fresh_alert.id)
        .count()
        == 1
    )


def test_fresh_candidate_fastlane_prioritizes_new_alerts(db, monkeypatch):
    user = models.User(name="fresh_fastlane")
    db.add(user)
    db.flush()
    now = datetime.utcnow()
    old_alert = BreakoutAlert(
        ticker="OLDFAST",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        user_id=user.id,
        alerted_at=now - timedelta(minutes=5),
    )
    fresh_alert = BreakoutAlert(
        ticker="NEWFAST",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        user_id=user.id,
        alerted_at=now,
    )
    db.add_all([old_alert, fresh_alert])
    db.commit()

    settings = _minimal_settings(user.id)
    settings.chili_autotrader_candidate_batch_size = 1
    settings.chili_autotrader_fresh_candidate_fastlane_enabled = True
    settings.chili_autotrader_fresh_candidate_fastlane_max_age_seconds = 30
    settings.chili_autotrader_candidate_price_prefetch_enabled = False
    monkeypatch.setattr(at_mod, "settings", settings)
    monkeypatch.setattr(
        at_mod,
        "effective_autotrader_runtime",
        lambda _db: _live_runtime(),
    )
    from app.services.trading import governance

    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: False)
    processed: list[str] = []

    def _record_only(_db, _uid, alert, out, _runtime):
        processed.append(alert.ticker)
        out["skipped"] += 1

    monkeypatch.setattr(at_mod, "_process_one_alert", _record_only)

    out = at_mod.run_auto_trader_tick(db)

    assert out["ok"] is True
    assert out["fresh_candidate_fastlane_enabled"] is True
    assert processed == ["NEWFAST"]


def test_fresh_fastlane_throttles_stale_backlog_sweeps(db, monkeypatch):
    user = models.User(name="fresh_fastlane_stale_sweep")
    db.add(user)
    db.flush()
    now = datetime.utcnow()
    stale_alerts = [
        BreakoutAlert(
            ticker=f"STALE{i}",
            asset_type="crypto",
            alert_tier="pattern_imminent",
            score_at_alert=0.8,
            price_at_alert=10.0,
            entry_price=10.0,
            stop_loss=9.0,
            target_price=12.0,
            user_id=user.id,
            alerted_at=now - timedelta(minutes=5, milliseconds=-i),
        )
        for i in range(2)
    ]
    expired_alert = BreakoutAlert(
        ticker="EXPIRED",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        user_id=user.id,
        alerted_at=now - timedelta(hours=2),
    )
    db.add_all([*stale_alerts, expired_alert])
    db.commit()

    settings = _minimal_settings(user.id)
    settings.chili_autotrader_candidate_batch_size = 1
    settings.chili_autotrader_fresh_candidate_fastlane_enabled = True
    settings.chili_autotrader_fresh_candidate_fastlane_max_age_seconds = 30
    settings.chili_autotrader_stale_candidate_sweep_interval_seconds = 60
    settings.chili_autotrader_non_stock_candidate_max_age_minutes = 30
    settings.chili_autotrader_candidate_price_prefetch_enabled = False
    monkeypatch.setattr(at_mod, "settings", settings)
    monkeypatch.setattr(at_mod, "_last_stale_candidate_sweep_at", 0.0)
    monkeypatch.setattr(
        at_mod,
        "effective_autotrader_runtime",
        lambda _db: _live_runtime(),
    )
    from app.services.trading import governance

    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: False)
    processed: list[str] = []
    monkeypatch.setattr(at_mod, "_process_one_alert", _audit_only_process(processed))

    first = at_mod.run_auto_trader_tick(db)
    second = at_mod.run_auto_trader_tick(db)
    monkeypatch.setattr(at_mod, "_last_stale_candidate_sweep_at", 0.0)
    third = at_mod.run_auto_trader_tick(db)

    assert first["ok"] is True
    assert first["stale_candidate_sweep_checked"] is True
    assert first["candidate_pool_exact"] is True
    assert second["ok"] is True
    assert second["stale_candidate_sweep_checked"] is False
    assert second["candidate_pool_exact"] is False
    assert second["processed"] == 0
    assert third["ok"] is True
    assert third["stale_candidate_sweep_checked"] is True
    assert processed == ["STALE1", "STALE0"]
    assert (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == expired_alert.id)
        .count()
        == 0
    )


def test_fresh_candidate_fastlane_bursts_across_one_fresh_window(db, monkeypatch):
    user = models.User(name="fresh_burst")
    db.add(user)
    db.flush()
    now = datetime.utcnow()
    old_alert = BreakoutAlert(
        ticker="OLDBURST",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        user_id=user.id,
        alerted_at=now - timedelta(minutes=5),
    )
    fresh_alerts = [
        BreakoutAlert(
            ticker=f"FRESH{i}",
            asset_type="crypto",
            alert_tier="pattern_imminent",
            score_at_alert=0.8,
            price_at_alert=10.0,
            entry_price=10.0,
            stop_loss=9.0,
            target_price=12.0,
            user_id=user.id,
            alerted_at=now + timedelta(milliseconds=i),
        )
        for i in range(5)
    ]
    db.add_all([old_alert, *fresh_alerts])
    db.commit()

    settings = _minimal_settings(user.id)
    settings.chili_autotrader_candidate_batch_size = 2
    settings.chili_autotrader_tick_interval_seconds = 10
    settings.chili_autotrader_fresh_candidate_fastlane_enabled = True
    settings.chili_autotrader_fresh_candidate_fastlane_max_age_seconds = 30
    settings.chili_autotrader_fresh_candidate_burst_enabled = True
    settings.chili_autotrader_candidate_price_prefetch_enabled = False
    monkeypatch.setattr(at_mod, "settings", settings)
    monkeypatch.setattr(
        at_mod,
        "effective_autotrader_runtime",
        lambda _db: _live_runtime(),
    )
    from app.services.trading import governance

    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: False)
    processed: list[str] = []
    monkeypatch.setattr(at_mod, "_process_one_alert", _audit_only_process(processed))

    out = at_mod.run_auto_trader_tick(db)

    assert out["ok"] is True
    assert out["candidate_batch_base_size"] == 2
    assert out["candidate_batch_effective_size"] == 5
    assert out["fresh_candidate_burst_window_count"] == 3
    assert set(processed) == {f"FRESH{i}" for i in range(5)}
    assert "OLDBURST" not in processed


def test_candidate_price_prefetch_attaches_batch_quote(db, monkeypatch):
    user = models.User(name="price_prefetch")
    db.add(user)
    db.flush()
    alert = BreakoutAlert(
        ticker="PREF-USD",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        user_id=user.id,
    )
    db.add(alert)
    db.commit()

    settings = _minimal_settings(user.id)
    settings.chili_autotrader_candidate_batch_size = 1
    settings.chili_autotrader_fresh_candidate_fastlane_enabled = False
    settings.chili_autotrader_candidate_price_prefetch_enabled = True
    monkeypatch.setattr(at_mod, "settings", settings)
    monkeypatch.setattr(
        at_mod,
        "effective_autotrader_runtime",
        lambda _db: _live_runtime(),
    )
    from app.services.trading import governance, market_data

    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(
        market_data,
        "fetch_quotes_batch",
        lambda tickers, **_kwargs: {"PREF-USD": {"price": "10.25"}},
    )
    seen: list[float | None] = []

    def _fake_process(db_, uid_, candidate, out, _runtime):
        seen.append(getattr(candidate, "_chili_prefetched_current_price", None))
        at_mod._audit(
            db_,
            user_id=uid_,
            alert=candidate,
            decision="skipped",
            reason="test_processed",
        )
        out["skipped"] += 1

    monkeypatch.setattr(at_mod, "_process_one_alert", _fake_process)

    out = at_mod.run_auto_trader_tick(db)

    assert out["ok"] is True
    assert out["candidate_price_prefetch_requested"] == 1
    assert out["candidate_price_prefetch_hits"] == 1
    assert out["tick_runtime_gate_elapsed_seconds"] >= 0
    assert out["tick_lock_cleanup_elapsed_seconds"] >= 0
    assert out["tick_candidate_select_elapsed_seconds"] >= 0
    assert out["tick_processing_elapsed_seconds"] >= 0
    assert out["tick_candidate_pool_zero_diag_elapsed_seconds"] == 0.0
    assert out["tick_slowest_alert_elapsed_seconds"] >= 0
    assert out["tick_slowest_alert_id"] == alert.id
    assert out["tick_slowest_alert_ticker"] == "PREF-USD"
    assert seen == [10.25]


def test_try_claim_alert_same_session_releases_cleanly(db):
    """The advisory lock is the primitive behind the TOCTOU fix. Acquire,
    release, then re-acquire in the same session to prove it's not sticky."""
    pytest.importorskip("sqlalchemy")
    if db.bind is None or db.bind.dialect.name != "postgresql":
        pytest.skip("advisory locks are Postgres-only")

    alert_id = 987654321
    assert at_mod._try_claim_alert(db, alert_id) is True
    at_mod._release_alert_claim(db, alert_id)

    # After release, another acquire in the same session must still succeed.
    assert at_mod._try_claim_alert(db, alert_id) is True
    at_mod._release_alert_claim(db, alert_id)


def test_try_claim_alert_second_session_blocks_while_held(db):
    """While one session holds the lock, a second session's try-acquire
    must return False. This is what prevents two concurrent AutoTrader
    ticks from both passing the gate for the same alert."""
    if db.bind is None or db.bind.dialect.name != "postgresql":
        pytest.skip("advisory locks are Postgres-only")

    from sqlalchemy.orm import sessionmaker

    alert_id = 123456789
    assert at_mod._try_claim_alert(db, alert_id) is True
    try:
        # Open a second independent session against the same engine.
        Session2 = sessionmaker(bind=db.bind)
        other = Session2()
        try:
            got_from_other = at_mod._try_claim_alert(other, alert_id)
            assert got_from_other is False, (
                "second session should not be able to claim while the first holds the lock"
            )
        finally:
            other.close()
    finally:
        at_mod._release_alert_claim(db, alert_id)


def test_kill_switch_flipped_mid_flight_blocks_placement(db, monkeypatch):
    """P0.5 — the kill switch is re-checked right before broker submission.
    Setup: kill switch starts off (tick-entry check passes), we patch
    is_kill_switch_active to start returning True only at the moment
    _execute_new_entry is about to call place_market_order."""
    u = models.User(name="kill_midflight_u")
    db.add(u)
    db.flush()

    alert = BreakoutAlert(
        ticker="KSWT",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=50.0,
        entry_price=50.0,
        stop_loss=48.0,
        target_price=55.0,
        user_id=u.id,
        scan_pattern_id=None,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    monkeypatch.setattr(at_mod, "settings", _minimal_settings(u.id))
    monkeypatch.setattr(at_mod, "effective_autotrader_runtime", lambda _db: _live_runtime())

    from app.services.trading import governance as gov
    # Tick-entry check (in auto_trader.run_auto_trader_tick) uses the module
    # import; _execute_new_entry imports it locally, which means
    # monkeypatching at_mod's reference is not enough — patch the source
    # module directly. Starts False, flips True on the 2nd call.
    calls = {"n": 0}

    def _flipping():
        calls["n"] += 1
        return calls["n"] >= 2  # pass the tick gate, fail the placement re-check

    monkeypatch.setattr(gov, "is_kill_switch_active", _flipping)

    # Stub out everything expensive between gates and placement so the
    # second is_kill_switch_active call IS the placement re-check.
    monkeypatch.setattr(at_mod, "_current_price", lambda _t: 50.0)
    monkeypatch.setattr(at_mod, "count_autotrader_v1_open", lambda *a, **k: 0)
    monkeypatch.setattr(at_mod, "autotrader_realized_pnl_today_et", lambda *a, **k: 0.0)
    monkeypatch.setattr(at_mod, "autotrader_paper_realized_pnl_today_et", lambda *a, **k: 0.0)
    monkeypatch.setattr(at_mod, "find_open_autotrader_trade", lambda *a, **k: None)
    monkeypatch.setattr(at_mod, "find_open_autotrader_paper", lambda *a, **k: None)
    monkeypatch.setattr(at_mod, "maybe_scale_in", lambda *a, **k: None)
    from app.services.trading import auto_trader_rules as rules_mod
    monkeypatch.setattr(rules_mod, "resolve_effective_capital", lambda *a, **k: (100_000.0, "broker_equity"))
    monkeypatch.setattr(
        rules_mod,
        "resolve_brain_risk_context",
        lambda *a, **k: {"dial_value": 1.0, "source": "test"},
    )
    monkeypatch.setattr(
        at_mod, "passes_rule_gate",
        lambda *a, **k: (True, "ok", {"projected_profit_pct": 10.0}),
    )

    from app.services.trading import autopilot_scope
    monkeypatch.setattr(
        autopilot_scope, "check_autopilot_entry_gate",
        lambda *a, **k: {"allowed": True, "reason": "test"},
    )

    from app.services.trading.venue import venue_health
    monkeypatch.setattr(venue_health, "is_venue_degraded", lambda *a, **k: False)

    # If placement ever got called, the test fails — it means the
    # mid-flight kill switch re-check was bypassed.
    fake_adapter = MagicMock()
    fake_adapter.is_enabled.return_value = True
    fake_adapter.place_market_order.side_effect = AssertionError(
        "broker call should have been blocked by mid-flight kill switch"
    )
    with patch(
        "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
        return_value=fake_adapter,
    ):
        out = at_mod.run_auto_trader_tick(db)

    assert out.get("ok") is True
    # The flipping check returned True before placement; expected outcome
    # is that _execute_new_entry wrote a 'blocked' audit row.
    fake_adapter.place_market_order.assert_not_called()

    runs = db.query(AutoTraderRun).filter(AutoTraderRun.breakout_alert_id == alert.id).all()
    assert runs, "expected an audit row for this alert"
    reasons = {r.reason for r in runs}
    assert any(
        r and "kill_switch_activated_mid_flight" in r for r in reasons
    ), f"expected mid-flight kill-switch reason, got: {reasons}"


def test_feature_parity_uses_decision_snapshot_not_recomputed(monkeypatch):
    """The AutoTrader parity gate must compare the real entry snapshot, not a
    freshly computed vector against itself."""
    from app.services.trading import feature_parity, market_data

    class _FakeDf:
        empty = False

    captured: dict = {}

    def _fake_check(db_, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(ok=True, reason=None, severity="ok")

    monkeypatch.setattr(
        at_mod,
        "settings",
        SimpleNamespace(
            chili_feature_parity_enabled=True,
            chili_autotrader_live_require_feature_parity=True,
            chili_feature_parity_fail_closed_on_error=True,
        ),
    )
    monkeypatch.setattr(market_data, "fetch_ohlcv_df", lambda *a, **k: _FakeDf())
    monkeypatch.setattr(feature_parity, "check_entry_feature_parity", _fake_check)

    alert = SimpleNamespace(
        indicator_snapshot={"rsi_14": 42.0, "ema_stack": True},
        signals_snapshot={},
        price_at_alert=101.0,
        entry_price=102.0,
    )

    reason = at_mod._maybe_check_feature_parity(
        None,
        alert=alert,
        rule_snapshot={"current_price": 103.0},
        ticker="SNAP",
        scan_pattern_id=None,
        venue="robinhood",
        source="test",
    )

    assert reason is None
    assert captured["live_snap"] == {
        "rsi_14": 42.0,
        "ema_stack": True,
        "price": 103.0,
    }
    assert captured["features"] == {"rsi_14", "ema_stack", "price"}


def test_qualified_block_shadow_decisions_cover_learning_dead_ends():
    assert (
        at_mod._qualified_reject_shadow_decision(
            "regime_gate:negative_ev_consensus:n_neg=2/4"
        )
        == "blocked_regime_gate"
    )
    assert (
        at_mod._qualified_reject_shadow_decision("llm_not_viable")
        == "blocked_llm_not_viable"
    )
    assert (
        at_mod._qualified_reject_shadow_decision("llm_unavailable")
        == "blocked_llm_unavailable"
    )
    assert (
        at_mod._llm_revalidation_block_reason({"error": "llm_unavailable"})
        == "llm_unavailable"
    )
    assert (
        at_mod._llm_revalidation_block_reason({"error": "parse_failed", "raw_preview": ""})
        == "llm_unavailable"
    )
    assert (
        at_mod._llm_revalidation_block_reason({"error": "parse_failed", "raw_preview": "oops"})
        == "llm_not_viable"
    )
    assert (
        at_mod._qualified_reject_shadow_decision("synergy_disabled_second_signal")
        == "skipped_synergy_disabled_second_signal"
    )
    assert (
        at_mod._qualified_reject_shadow_decision("synergy_retry_not_applicable")
        == "skipped_synergy_retry_not_applicable"
    )


def test_synergy_retry_candidates_revisit_recent_distinct_pattern(db, monkeypatch):
    user = models.User(name="synergy_retry_candidate")
    db.add(user)
    db.flush()
    base_pattern = ScanPattern(name="Base", rules_json={}, active=True)
    confirming_pattern = ScanPattern(name="Confirm", rules_json={}, active=True)
    db.add_all([base_pattern, confirming_pattern])
    db.flush()
    alert = BreakoutAlert(
        ticker="SYN-USD",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        score_at_alert=0.9,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        scan_pattern_id=confirming_pattern.id,
        user_id=user.id,
    )
    trade = Trade(
        user_id=user.id,
        ticker="SYN-USD",
        direction="long",
        entry_price=10.0,
        quantity=1.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=12.0,
        scan_pattern_id=base_pattern.id,
        auto_trader_version="v1",
        scale_in_count=1,
    )
    db.add_all([alert, trade])
    db.flush()
    source_run = AutoTraderRun(
        user_id=user.id,
        breakout_alert_id=alert.id,
        scan_pattern_id=confirming_pattern.id,
        ticker="SYN-USD",
        decision="skipped",
        reason="synergy_not_applicable",
    )
    db.add(source_run)
    db.commit()

    monkeypatch.setattr(
        at_mod,
        "settings",
        SimpleNamespace(
            chili_autotrader_synergy_retry_enabled=True,
            chili_autotrader_synergy_retry_lookback_minutes=60,
            chili_autotrader_synergy_retry_max_per_tick=1,
        ),
    )

    retry_pool, alerts = at_mod._synergy_retry_candidates(
        db,
        uid=user.id,
        limit=1,
    )

    assert retry_pool == 1
    assert [row.id for row in alerts] == [alert.id]
    assert getattr(alerts[0], "_chili_synergy_retry") is True
    assert getattr(alerts[0], "_chili_synergy_retry_source_run_id") == source_run.id


def test_run_tick_processes_synergy_retry_even_when_alert_has_prior_run(
    db,
    monkeypatch,
):
    user = models.User(name="synergy_retry_tick")
    db.add(user)
    db.flush()
    base_pattern = ScanPattern(name="Base tick", rules_json={}, active=True)
    confirming_pattern = ScanPattern(name="Confirm tick", rules_json={}, active=True)
    db.add_all([base_pattern, confirming_pattern])
    db.flush()
    alert = BreakoutAlert(
        ticker="SYNT-USD",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        score_at_alert=0.9,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        scan_pattern_id=confirming_pattern.id,
        user_id=user.id,
    )
    trade = Trade(
        user_id=user.id,
        ticker="SYNT-USD",
        direction="long",
        entry_price=10.0,
        quantity=1.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=12.0,
        scan_pattern_id=base_pattern.id,
        auto_trader_version="v1",
        scale_in_count=1,
    )
    db.add_all([alert, trade])
    db.flush()
    db.add(
        AutoTraderRun(
            user_id=user.id,
            breakout_alert_id=alert.id,
            scan_pattern_id=confirming_pattern.id,
            ticker="SYNT-USD",
            decision="skipped",
            reason="synergy_not_applicable",
        )
    )
    db.commit()

    monkeypatch.setattr(
        at_mod,
        "settings",
        SimpleNamespace(
            chili_autotrader_enabled=True,
            chili_autotrader_candidate_batch_size=1,
            chili_autotrader_synergy_retry_enabled=True,
            chili_autotrader_synergy_retry_lookback_minutes=60,
            chili_autotrader_synergy_retry_max_per_tick=1,
        ),
    )
    monkeypatch.setattr(
        at_mod,
        "effective_autotrader_runtime",
        lambda _db: {"tick_allowed": True},
    )
    monkeypatch.setattr(at_mod, "_resolve_user_id", lambda: user.id)
    seen: list[dict] = []

    def _fake_process(_db, _uid, retry_alert, out, _rt):
        seen.append({
            "alert_id": retry_alert.id,
            "retry": getattr(retry_alert, "_chili_synergy_retry", False),
        })
        out["skipped"] += 1

    monkeypatch.setattr(at_mod, "_process_one_alert", _fake_process)

    out = at_mod.run_auto_trader_tick(db)

    assert out["processed"] == 1
    assert out["synergy_retry_batch"] == 1
    assert seen == [{"alert_id": alert.id, "retry": True}]


def test_live_entry_blocks_recert_required_pattern_after_sizing_before_broker(db, monkeypatch):
    u = models.User(name="recert_block_u")
    db.add(u)
    db.flush()
    pat = ScanPattern(
        name="Needs recert",
        rules_json={},
        lifecycle_stage="live",
        active=True,
        recert_required=True,
    )
    db.add(pat)
    db.flush()
    alert = BreakoutAlert(
        ticker="RCERT",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.9,
        price_at_alert=50.0,
        entry_price=50.0,
        stop_loss=48.0,
        target_price=55.0,
        user_id=u.id,
        scan_pattern_id=pat.id,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    settings = _minimal_settings(u.id)
    settings.chili_autotrader_block_live_on_recert_required = True
    settings.chili_autotrader_paper_shadow_qualified_blocks_enabled = False
    monkeypatch.setattr(at_mod, "settings", settings)
    monkeypatch.setattr(
        at_mod,
        "_resolve_entry_risk_notional",
        lambda *a, **k: (100.0, {
            "notional_capital_usd": 10_000.0,
            "notional_explicit_fallback_usd": 0.0,
            "notional_risk_pct": 1.0,
            "notional_source": "test",
            "notional_capital_source": "test",
        }),
    )
    monkeypatch.setattr(
        at_mod,
        "_execute_broker_buy",
        lambda *a, **k: pytest.fail("recert block should happen before broker buy"),
    )
    shadow_calls: list[dict] = []
    monkeypatch.setattr(
        at_mod,
        "_maybe_open_paper_shadow",
        lambda *a, **k: shadow_calls.append(k),
    )

    out = {"skipped": 0, "blocked": 0}
    setattr(alert, "_chili_recert_required", True)
    at_mod._execute_new_entry(db, u.id, alert, TEST_ENTRY_PRICE, {}, None, True, out)

    runs = db.query(AutoTraderRun).filter(AutoTraderRun.breakout_alert_id == alert.id).all()
    assert runs
    assert {r.reason for r in runs} == {"pattern_recert_required"}
    assert runs[0].rule_snapshot["recert_signal_fastlane"]["queued"] is True
    assert shadow_calls
    assert shadow_calls[0]["decision"] == "blocked_recert_required"

    recert_logs = (
        db.query(PatternRecertLog)
        .filter(PatternRecertLog.scan_pattern_id == pat.id)
        .all()
    )
    assert len(recert_logs) == 1
    assert recert_logs[0].source == "scheduler"
    assert recert_logs[0].reason == "autotrader_signal:pattern_recert_required"


def test_shadow_stock_fastlane_boosts_pattern_for_positive_edge(monkeypatch):
    settings = _minimal_settings(1)
    monkeypatch.setattr(at_mod, "settings", settings)
    emitted: list[dict] = []
    invalidated: list[bool] = []
    monkeypatch.setattr(
        "app.services.trading.brain_work.emitters.emit_backtest_requested_for_pattern",
        lambda db, scan_pattern_id, source: emitted.append({
            "scan_pattern_id": scan_pattern_id,
            "source": source,
        }) or 1,
    )
    monkeypatch.setattr(
        "app.services.trading.backtest_queue.invalidate_queue_status_cache",
        lambda: invalidated.append(True),
    )
    db = MagicMock()
    pat = ScanPattern(
        id=123,
        name="Shadow stock wants evidence",
        rules_json={},
        lifecycle_stage="shadow_promoted",
        active=True,
        backtest_priority=0,
    )
    alert = BreakoutAlert(
        id=456,
        ticker="FASTL",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.9,
        price_at_alert=TEST_ENTRY_PRICE,
        entry_price=TEST_ENTRY_PRICE,
        stop_loss=TEST_STOP_PRICE,
        target_price=TEST_TARGET_PRICE,
        user_id=1,
        scan_pattern_id=pat.id,
    )
    snap = {"entry_edge": {"expected_net_pct": TEST_POSITIVE_EXPECTED_NET_PCT}}

    fastlane = at_mod._queue_shadow_stock_fastlane_for_observation(
        db,
        alert=alert,
        pattern=pat,
        reason=at_mod.SHADOW_OBSERVATION_REASON_STAGE,
        snap=snap,
    )

    assert fastlane is not None
    assert fastlane["queued"] is True
    assert fastlane["priority"] == AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_BACKTEST_PRIORITY
    assert fastlane["expected_net_pct"] == TEST_POSITIVE_EXPECTED_NET_PCT
    assert pat.backtest_priority == AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_BACKTEST_PRIORITY
    assert emitted == [{
        "scan_pattern_id": pat.id,
        "source": "autotrader_shadow_stock_fastlane",
    }]
    assert invalidated == [True]
    db.flush.assert_called_once()


def test_shadow_stock_fastlane_respects_recent_backtest_cooldown(monkeypatch):
    settings = _minimal_settings(1)
    monkeypatch.setattr(at_mod, "settings", settings)
    emitted: list[dict] = []
    monkeypatch.setattr(
        "app.services.trading.brain_work.emitters.emit_backtest_requested_for_pattern",
        lambda db, scan_pattern_id, source: emitted.append({
            "scan_pattern_id": scan_pattern_id,
            "source": source,
        }) or 1,
    )
    db = MagicMock()
    pat = ScanPattern(
        id=124,
        name="Recently tested stock shadow",
        rules_json={},
        lifecycle_stage="shadow_promoted",
        active=True,
        backtest_priority=0,
        last_backtest_at=datetime.utcnow(),
    )
    alert = BreakoutAlert(
        id=457,
        ticker="COOLD",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.9,
        price_at_alert=TEST_ENTRY_PRICE,
        entry_price=TEST_ENTRY_PRICE,
        stop_loss=TEST_STOP_PRICE,
        target_price=TEST_TARGET_PRICE,
        user_id=1,
        scan_pattern_id=pat.id,
    )
    snap = {"entry_edge": {"expected_net_pct": TEST_POSITIVE_EXPECTED_NET_PCT}}

    fastlane = at_mod._queue_shadow_stock_fastlane_for_observation(
        db,
        alert=alert,
        pattern=pat,
        reason=at_mod.SHADOW_OBSERVATION_REASON_STAGE,
        snap=snap,
    )

    assert fastlane is not None
    assert fastlane["queued"] is False
    assert fastlane["reason"] == "recent_backtest_cooldown"
    assert fastlane["reboost_cooldown_minutes"] == (
        AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_REBOOST_COOLDOWN_MINUTES
    )
    assert pat.backtest_priority == 0
    assert emitted == []
    db.flush.assert_not_called()


def test_shadow_observation_uses_lightweight_sizing_path(monkeypatch):
    settings = _minimal_settings(1)
    settings.chili_autotrader_shadow_observation_diagnostic_sizing_enabled = False
    settings.chili_autotrader_shadow_observation_evidence_notional_usd = (
        TEST_RISK_NOTIONAL
    )
    monkeypatch.setattr(at_mod, "settings", settings)
    monkeypatch.setattr(
        at_mod,
        "_resolve_entry_risk_notional",
        lambda *a, **k: pytest.fail(
            "shadow observation should skip broker-backed notional resolution"
        ),
    )
    monkeypatch.setattr(
        at_mod,
        "_pattern_row",
        lambda *a, **k: ScanPattern(
            id=123,
            lifecycle_stage="shadow_promoted",
            active=True,
        ),
    )
    monkeypatch.setattr(
        at_mod,
        "_queue_shadow_stock_fastlane_for_observation",
        lambda *a, **k: {"queued": False, "reason": "test"},
    )
    monkeypatch.setattr(
        "app.services.trading.hrp_sizing.decide_position_size",
        lambda *a, **k: pytest.fail("shadow observation should skip HRP sizing"),
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_survival.decisions.compute_decision",
        lambda *a, **k: pytest.fail("shadow observation should skip survival sizing"),
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_shadow_vetting.pilot_promoted_risk_multiplier",
        lambda *a, **k: pytest.fail("shadow observation should skip pilot sizing"),
    )
    monkeypatch.setattr(
        "app.services.trading.position_sizer_emitter.emit_shadow_proposal",
        lambda *a, **k: pytest.fail("shadow observation should skip position sizing"),
    )
    audit_calls: list[dict] = []
    paper_calls: list[dict] = []
    monkeypatch.setattr(
        at_mod,
        "_audit",
        lambda *a, **k: audit_calls.append(k),
    )
    monkeypatch.setattr(
        at_mod,
        "_maybe_open_paper_shadow",
        lambda *a, **k: paper_calls.append(k),
    )
    monkeypatch.setattr(
        at_mod,
        "_execute_broker_buy",
        lambda *a, **k: pytest.fail("shadow observation must not reach broker"),
    )

    alert = BreakoutAlert(
        id=456,
        ticker="FASTL",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.9,
        price_at_alert=TEST_ENTRY_PRICE,
        entry_price=TEST_ENTRY_PRICE,
        stop_loss=TEST_STOP_PRICE,
        target_price=TEST_TARGET_PRICE,
        user_id=1,
        scan_pattern_id=123,
    )
    setattr(alert, "_chili_shadow_observation_only", True)
    setattr(
        alert,
        "_chili_shadow_observation_reason",
        at_mod.SHADOW_OBSERVATION_REASON_STAGE,
    )
    out = {"skipped": 0, "placed": 0}

    at_mod._execute_new_entry(
        MagicMock(),
        1,
        alert,
        TEST_ENTRY_PRICE,
        {},
        None,
        True,
        out,
    )

    assert out["skipped"] == 1
    assert audit_calls and audit_calls[0]["decision"] == "blocked"
    snap = audit_calls[0]["rule_snapshot"]
    assert snap["shadow_observation_sizing_mode"] == (
        at_mod.SHADOW_OBSERVATION_SIZING_MODE_BASE_RISK
    )
    assert snap["shadow_observation_lightweight_sizing_supported"] is True
    assert snap["shadow_observation_advisory_sizing_skipped"] is True
    assert snap["shadow_observation_advisory_sizing_skip_reason"] == (
        at_mod.SHADOW_OBSERVATION_ADVISORY_SIZING_SKIP_REASON
    )
    assert snap["notional_broker_lookup_skipped"] is True
    assert snap["notional_source"] == at_mod.SHADOW_OBSERVATION_NOTIONAL_SOURCE_EVIDENCE
    assert "position_sizer_proposal_id" not in snap
    assert "hrp_size_usd" not in snap
    assert "ps_sizing_decision" not in snap
    assert "pilot_promoted_risk_multiplier" not in snap
    assert paper_calls
    assert paper_calls[0]["qty"] == TEST_RISK_NOTIONAL / TEST_ENTRY_PRICE


def test_live_recert_block_waives_pilot_bootstrap_cert_debt(monkeypatch):
    settings = _minimal_settings(123)
    settings.chili_autotrader_block_live_on_recert_required = True
    settings.chili_pilot_promoted_allow_bootstrap_recert_live = True
    monkeypatch.setattr(at_mod, "settings", settings)

    soft_pilot = ScanPattern(
        lifecycle_stage="pilot_promoted",
        recert_required=True,
        recert_reason="missing_oos_recert,missing_quality_composite_score,thin_realized_ev",
    )
    hard_pilot = ScanPattern(
        lifecycle_stage="pilot_promoted",
        recert_required=True,
        recert_reason="missing_oos_recert,negative_realized_ev",
    )
    full_risk = ScanPattern(
        lifecycle_stage="promoted",
        recert_required=True,
        recert_reason="missing_oos_recert",
    )
    probation_full_risk = ScanPattern(
        lifecycle_stage="promoted",
        recert_required=True,
        recert_reason="missing_oos_recert",
        promotion_gate_passed=True,
        cpcv_n_paths=TEST_CPCV_PATHS,
        cpcv_median_sharpe=TEST_STRONG_CPCV_SHARPE,
        raw_realized_trade_count=TEST_REALIZED_TRADE_COUNT,
        raw_realized_avg_return_pct=TEST_REALIZED_AVG_RETURN_PCT,
    )

    assert at_mod._live_recert_block_applies(soft_pilot) is False
    assert at_mod._live_recert_block_applies(hard_pilot) is True
    assert at_mod._live_recert_block_applies(full_risk) is True
    assert at_mod._live_recert_block_applies(probation_full_risk) is False
    assert at_mod._live_recert_allowance(probation_full_risk) == at_mod.PROBATION_RECERT_ALLOWANCE


def test_probation_recert_live_entry_reduces_size_and_enforces_daily_quota(db, monkeypatch):
    u = models.User(name="probation_recert_u")
    db.add(u)
    db.flush()
    pat = ScanPattern(
        name="Strong promoted soft OOS debt",
        rules_json={},
        lifecycle_stage="promoted",
        active=True,
        recert_required=True,
        recert_reason="missing_oos_recert",
        promotion_gate_passed=True,
        cpcv_n_paths=TEST_CPCV_PATHS,
        cpcv_median_sharpe=TEST_STRONG_CPCV_SHARPE,
        raw_realized_trade_count=TEST_REALIZED_TRADE_COUNT,
        raw_realized_avg_return_pct=TEST_REALIZED_AVG_RETURN_PCT,
    )
    db.add(pat)
    db.flush()

    settings = _minimal_settings(u.id)
    settings.chili_autotrader_block_live_on_recert_required = True
    monkeypatch.setattr(at_mod, "settings", settings)
    monkeypatch.setattr(
        at_mod,
        "_resolve_entry_risk_notional",
        lambda *a, **k: (TEST_RISK_NOTIONAL, {
            "notional_capital_usd": TEST_ACCOUNT_EQUITY,
            "notional_explicit_fallback_usd": 0.0,
            "notional_risk_pct": settings.chili_autotrader_per_trade_risk_pct,
            "notional_source": "test",
            "notional_capital_source": "test",
        }),
    )
    monkeypatch.setattr(
        "app.services.trading.tick_normalizer.normalize_quantity",
        lambda qty, _ticker: qty,
    )
    monkeypatch.setattr(
        "app.services.trading.pdt_guard.can_open_intraday_round_trip",
        lambda *a, **k: SimpleNamespace(allowed=True, reason="ok", snapshot={}),
    )

    broker_calls: list[dict] = []

    def _fake_broker_buy(*_args, **kwargs):
        broker_calls.append(kwargs)
        return {
            "ok": True,
            "order_id": f"probation-order-{len(broker_calls)}",
            "raw": {"average_price": TEST_ENTRY_PRICE},
        }

    monkeypatch.setattr(at_mod, "_execute_broker_buy", _fake_broker_buy)

    first_alert = BreakoutAlert(
        ticker="PBRT",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.9,
        price_at_alert=TEST_ENTRY_PRICE,
        entry_price=TEST_ENTRY_PRICE,
        stop_loss=TEST_STOP_PRICE,
        target_price=TEST_TARGET_PRICE,
        user_id=u.id,
        scan_pattern_id=pat.id,
    )
    db.add(first_alert)
    db.commit()
    db.refresh(first_alert)

    out = {"skipped": 0, "placed": 0, "blocked": 0}
    setattr(first_alert, "_chili_probation_recert_allowed", True)
    at_mod._execute_new_entry(
        db,
        u.id,
        first_alert,
        TEST_ENTRY_PRICE,
        {},
        None,
        True,
        out,
    )

    assert out["placed"] == 1
    assert broker_calls
    expected_qty = (
        TEST_RISK_NOTIONAL * TEST_PROBATION_MULTIPLIER / TEST_ENTRY_PRICE
    )
    assert broker_calls[0]["qty"] == expected_qty

    second_alert = BreakoutAlert(
        ticker="PBRT2",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.9,
        price_at_alert=TEST_ENTRY_PRICE,
        entry_price=TEST_ENTRY_PRICE,
        stop_loss=TEST_STOP_PRICE,
        target_price=TEST_TARGET_PRICE,
        user_id=u.id,
        scan_pattern_id=pat.id,
    )
    db.add(second_alert)
    db.commit()
    db.refresh(second_alert)

    setattr(second_alert, "_chili_probation_recert_allowed", True)
    at_mod._execute_new_entry(
        db,
        u.id,
        second_alert,
        TEST_ENTRY_PRICE,
        {},
        None,
        True,
        out,
    )

    assert len(broker_calls) == TEST_PROBATION_MAX_PER_PATTERN
    reasons = {
        row.reason
        for row in db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == second_alert.id)
        .all()
    }
    assert at_mod.PROBATION_QUOTA_REASON_PATTERN in reasons


def test_feature_parity_blocks_price_only_snapshot_when_required(monkeypatch):
    from app.services.trading import feature_parity, market_data

    class _FakeDf:
        empty = False

    monkeypatch.setattr(
        at_mod,
        "settings",
        SimpleNamespace(
            chili_feature_parity_enabled=True,
            chili_autotrader_live_require_feature_parity=True,
            chili_feature_parity_fail_closed_on_error=True,
        ),
    )
    monkeypatch.setattr(market_data, "fetch_ohlcv_df", lambda *a, **k: _FakeDf())
    monkeypatch.setattr(
        feature_parity,
        "check_entry_feature_parity",
        lambda *a, **k: pytest.fail("price-only snapshot should block before parity"),
    )

    alert = SimpleNamespace(
        indicator_snapshot={},
        signals_snapshot={},
        price_at_alert=None,
        entry_price=None,
    )

    reason = at_mod._maybe_check_feature_parity(
        None,
        alert=alert,
        rule_snapshot={"current_price": 103.0},
        ticker="SNAP",
        scan_pattern_id=None,
        venue="robinhood",
        source="test",
    )

    assert reason == "feature_parity_unavailable:no_signal_features"


def test_required_venue_health_blocks_insufficient_data(monkeypatch):
    from app.services.trading.venue import venue_health

    monkeypatch.setattr(
        at_mod,
        "settings",
        SimpleNamespace(chili_autotrader_live_require_venue_health_enabled=True),
    )
    monkeypatch.setattr(
        venue_health,
        "summarize_venue",
        lambda *a, **k: {"status": "insufficient_data", "reason": "lifecycle_samples=0<5"},
    )

    reason = at_mod._live_venue_health_block_reason(None, venue="coinbase")

    assert reason == "venue_health_insufficient_data:coinbase:lifecycle_samples=0<5"


def test_entry_risk_notional_refuses_unproven_fallback_capital(monkeypatch):
    from app.services.trading import auto_trader_rules as rules_mod

    monkeypatch.setattr(
        at_mod,
        "settings",
        SimpleNamespace(
            chili_autotrader_per_trade_notional_usd=0.0,
            chili_autotrader_per_trade_risk_pct=1.0,
        ),
    )
    monkeypatch.setattr(
        rules_mod,
        "resolve_effective_capital",
        lambda *a, **k: (100_000.0, "fallback:broker_disconnected"),
    )
    monkeypatch.setattr(
        rules_mod,
        "resolve_brain_risk_context",
        lambda *a, **k: {"dial_value": 1.0},
    )

    notional, snap = at_mod._resolve_entry_risk_notional(MagicMock(), uid=1)

    assert notional == 0.0
    assert snap["notional_source"] == "capital_unavailable"
    assert snap["notional_capital_source"] == "fallback:broker_disconnected"
    assert snap["notional_capital_unproven"] is True


def test_entry_risk_notional_keeps_explicit_operator_notional(monkeypatch):
    from app.services.trading import auto_trader_rules as rules_mod

    monkeypatch.setattr(
        at_mod,
        "settings",
        SimpleNamespace(
            chili_autotrader_per_trade_notional_usd=250.0,
            chili_autotrader_per_trade_risk_pct=1.0,
        ),
    )
    monkeypatch.setattr(
        rules_mod,
        "resolve_effective_capital",
        lambda *a, **k: (100_000.0, "fallback:broker_disconnected"),
    )
    monkeypatch.setattr(
        rules_mod,
        "resolve_brain_risk_context",
        lambda *a, **k: {"dial_value": 0.5},
    )

    notional, snap = at_mod._resolve_entry_risk_notional(MagicMock(), uid=1)

    assert notional == pytest.approx(125.0)
    assert snap["notional_source"] == "explicit_env_notional_dial"
    assert snap["notional_capital_unproven"] is True


def test_scale_in_blocks_live_capital_fallback(monkeypatch):
    alert = SimpleNamespace(id=77, ticker="SCAP")
    monkeypatch.setattr(
        at_mod,
        "settings",
        SimpleNamespace(chili_autotrader_block_live_on_capital_fallback=True),
    )
    from app.services.trading import auto_trader_rules as rules_mod

    monkeypatch.setattr(
        rules_mod,
        "resolve_effective_capital",
        lambda *a, **k: (100_000.0, "fallback:get_portfolio_timeout"),
    )
    monkeypatch.setattr(
        at_mod,
        "_execute_broker_buy",
        lambda *a, **k: pytest.fail("broker should not be called on fallback capital"),
    )
    blocked: list[dict] = []
    monkeypatch.setattr(
        at_mod,
        "_block_live_order",
        lambda *a, **k: blocked.append(k),
    )

    plan = SimpleNamespace(
        trade=SimpleNamespace(),
        added_quantity=1.0,
        new_avg_entry=50.0,
        new_stop=48.0,
        new_target=55.0,
    )
    out = {"skipped": 0, "scaled_in": 0}

    at_mod._execute_scale_in(None, 1, alert, plan, 50.0, {}, None, True, out)

    assert blocked
    assert blocked[0]["reason"] == "capital_unavailable:fallback:get_portfolio_timeout"


def test_scale_in_records_confirming_pattern_history(monkeypatch):
    db = MagicMock()
    alert = SimpleNamespace(id=77, ticker="SCAP", scan_pattern_id=12)
    trade = SimpleNamespace(
        id=5,
        entry_price=TEST_ENTRY_PRICE,
        quantity=1.0,
        stop_loss=TEST_STOP_PRICE,
        take_profit=TEST_TARGET_PRICE,
        scale_in_count=0,
        indicator_snapshot={
            at_mod.SCALE_IN_ALERT_IDS_SNAPSHOT_KEY: [70],
            at_mod.SCALE_IN_PATTERN_IDS_SNAPSHOT_KEY: [10],
        },
    )
    plan = SimpleNamespace(
        trade=trade,
        added_quantity=1.0,
        new_avg_entry=51.0,
        new_stop=TEST_STOP_PRICE,
        new_target=TEST_TARGET_PRICE,
        confirming_pattern_id=12,
    )
    monkeypatch.setattr(at_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(at_mod, "_autotrader_tick_note", lambda *a, **k: None)

    out = {"scaled_in": 0}

    at_mod._execute_scale_in(db, 1, alert, plan, TEST_ENTRY_PRICE, {}, None, False, out)

    assert trade.scale_in_count == 1
    assert trade.indicator_snapshot[at_mod.SCALE_IN_ALERT_IDS_SNAPSHOT_KEY] == [70, 77]
    assert trade.indicator_snapshot[at_mod.SCALE_IN_PATTERN_IDS_SNAPSHOT_KEY] == [10, 12]


def test_scale_in_normalizes_quantity_before_live_broker(monkeypatch):
    db = MagicMock()
    alert = SimpleNamespace(id=78, ticker="ACMR", scan_pattern_id=12)
    trade = SimpleNamespace(
        id=6,
        entry_price=TEST_ENTRY_PRICE,
        quantity=1.0,
        stop_loss=TEST_STOP_PRICE,
        take_profit=TEST_TARGET_PRICE,
        scale_in_count=0,
        indicator_snapshot={},
    )
    plan = SimpleNamespace(
        trade=trade,
        added_quantity=0.5595970900951316,
        new_avg_entry=51.0,
        new_stop=TEST_STOP_PRICE,
        new_target=TEST_TARGET_PRICE,
        confirming_pattern_id=12,
    )
    captured: dict[str, float] = {}

    monkeypatch.setattr(
        at_mod,
        "settings",
        SimpleNamespace(chili_autotrader_block_live_on_capital_fallback=False),
    )
    monkeypatch.setattr(at_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(at_mod, "_autotrader_tick_note", lambda *a, **k: None)
    def _fake_broker_buy(*args, **kwargs):
        captured["qty"] = kwargs["qty"]
        return {"ok": True, "order_id": "rh-1"}

    monkeypatch.setattr(at_mod, "_execute_broker_buy", _fake_broker_buy)

    out = {"scaled_in": 0, "skipped": 0}
    snap: dict[str, object] = {}

    at_mod._execute_scale_in(db, 1, alert, plan, TEST_ENTRY_PRICE, snap, None, True, out)

    assert captured["qty"] == pytest.approx(0.559597)
    assert trade.quantity == pytest.approx(1.559597)
    assert snap["scale_in_qty_raw"] == pytest.approx(0.55959709)
    assert snap["scale_in_qty_normalized"] == pytest.approx(0.559597)


def test_scale_in_zero_normalized_quantity_skips_broker(monkeypatch):
    db = MagicMock()
    alert = SimpleNamespace(id=79, ticker="ACMR", scan_pattern_id=12)
    plan = SimpleNamespace(
        trade=SimpleNamespace(id=7, quantity=1.0, indicator_snapshot={}),
        added_quantity=0.00000001,
        new_avg_entry=51.0,
        new_stop=TEST_STOP_PRICE,
        new_target=TEST_TARGET_PRICE,
    )
    audit_rows: list[dict[str, object]] = []

    monkeypatch.setattr(
        at_mod,
        "settings",
        SimpleNamespace(chili_autotrader_block_live_on_capital_fallback=False),
    )
    monkeypatch.setattr(
        at_mod,
        "_execute_broker_buy",
        lambda *a, **k: pytest.fail("zero normalized scale-in should not reach broker"),
    )
    monkeypatch.setattr(
        at_mod,
        "_audit",
        lambda *a, **k: audit_rows.append(k),
    )
    monkeypatch.setattr(at_mod, "_autotrader_tick_note", lambda *a, **k: None)

    out = {"scaled_in": 0, "skipped": 0}
    snap: dict[str, object] = {}

    at_mod._execute_scale_in(db, 1, alert, plan, TEST_ENTRY_PRICE, snap, None, True, out)

    assert out["skipped"] == 1
    assert audit_rows[0]["reason"] == "scale_in_notional_below_trade_unit"
    assert snap["scale_in_qty_normalized"] == 0.0


def test_coinbase_cap_uses_passed_price(monkeypatch):
    from app import config as config_mod
    from app.services.trading import broker_selector, cost_aware_gate, governance
    from app.services.trading.venue import factory as venue_factory

    captured: dict[str, float] = {}

    def _fake_cap_check(*, proposed_notional_usd, **kwargs):
        captured["proposed_notional_usd"] = proposed_notional_usd
        return SimpleNamespace(
            allowed=True,
            reason="within_cap",
            current_positions=0,
            current_notional_usd=0.0,
        )

    fake_adapter = MagicMock()
    fake_adapter.is_enabled.return_value = True
    fake_adapter.place_market_order.return_value = {"ok": True, "order_id": "cb-123"}

    monkeypatch.setattr(
        at_mod,
        "settings",
        SimpleNamespace(chili_autotrader_block_live_on_capital_fallback=True),
    )
    monkeypatch.setattr(
        config_mod,
        "settings",
        SimpleNamespace(chili_coinbase_autotrader_live=True),
    )
    monkeypatch.setattr(governance, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(
        cost_aware_gate,
        "cost_aware_min_edge_gate",
        lambda *a, **k: SimpleNamespace(
            allowed=True,
            reason="ok",
            edge_bps=500,
            threshold_bps=0,
            fee_bps=0,
            tca_cost_bps=0,
            tca_snapshot=None,
        ),
    )
    monkeypatch.setattr(
        broker_selector,
        "select_venue",
        lambda *a, **k: SimpleNamespace(venue="coinbase", reason="test"),
    )
    monkeypatch.setattr(at_mod, "_live_venue_health_block_reason", lambda *a, **k: None)
    monkeypatch.setattr(cost_aware_gate, "per_venue_cap_check", _fake_cap_check)
    monkeypatch.setattr(venue_factory, "get_adapter", lambda venue: fake_adapter)

    alert = SimpleNamespace(
        id=88,
        ticker="PEPE-USD",
        entry_price=None,
        price_at_alert=None,
    )
    out = {"skipped": 0}

    res = at_mod._execute_broker_buy(
        None,
        uid=1,
        alert=alert,
        qty=10.0,
        client_order_id="cid-88",
        snap={"projected_profit_pct": 5.0},
        llm_snap=None,
        out=out,
        px=0.5,
    )

    assert res is not None and res["ok"] is True
    assert res["_chili_broker_source"] == "coinbase"
    assert captured["proposed_notional_usd"] == 5.0
    fake_adapter.place_market_order.assert_called_once_with(
        product_id="PEPE-USD",
        side="buy",
        base_size="10.0",
        client_order_id="cid-88",
    )


def test_broker_reject_suppression_applies_pattern_filter_before_limit(db, monkeypatch):
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_broker_reject_suppression_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_broker_reject_suppression_minutes",
        60,
        raising=False,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_broker_reject_suppression_threshold",
        2,
        raising=False,
    )
    pat = ScanPattern(
        name="broker reject suppression",
        rules_json={},
        origin="test",
        asset_class="crypto",
        timeframe="1d",
        active=True,
        lifecycle_stage="promoted",
    )
    other = ScanPattern(
        name="other broker reject suppression",
        rules_json={},
        origin="test",
        asset_class="crypto",
        timeframe="1d",
        active=True,
        lifecycle_stage="promoted",
    )
    db.add_all([pat, other])
    db.flush()
    alert = BreakoutAlert(
        scan_pattern_id=pat.id,
        ticker="TRUMP-USD",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        outcome="pending",
        score_at_alert=90.0,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        alerted_at=datetime.utcnow(),
    )
    db.add(alert)
    db.flush()
    now = datetime.utcnow()
    for idx in range(3):
        db.add(
            AutoTraderRun(
                breakout_alert_id=alert.id,
                scan_pattern_id=other.id if idx == 0 else pat.id,
                ticker="TRUMP-USD",
                decision="error",
                reason="broker:Robinhood returned no order_id",
                rule_snapshot={"broker_reject_fingerprint": "same-fp"},
                created_at=now - timedelta(minutes=idx),
            )
        )
    db.commit()

    suppression = at_mod._broker_reject_suppression(db, alert, "same-fp")

    assert suppression is not None
    assert suppression["recent_reject_count"] == 2
    assert suppression["last_reject_reason"] == "broker:Robinhood returned no order_id"
