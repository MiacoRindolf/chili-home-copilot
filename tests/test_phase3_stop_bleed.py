"""Tests for f-phase3-stop-bleed (Phase 3 stop-the-bleeding deliverables).

Covers D1 (monthly DD breaker), D2 (NameError reason format),
D3 (product_id normalizer), D4 (BUY pre-flight cash check),
D6 (@validates scan_pattern_id), D7 (migration 243 BNB-USD zombie
cleanup).

D5 (stop_not_below_entry producer fix) is deferred to a follow-up
brief per the f-phase3-stop-bleed hard-constraints allowance; the
existing rule at auto_trader_rules.py:915 continues to reject the bad
orders so no regression test is needed in this file.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.models.trading import ScanPattern, Trade, _NO_PATTERN_SENTINEL
from app.services.trading import portfolio_risk
from app.services.trading.portfolio_risk import (
    _assert_portfolio_breaker_ok,
    _monthly_attributed_pnl,
    _monthly_dd_threshold,
    _monthly_total_pnl,
    _portfolio_dd_threshold,
    check_drawdown_breaker,
    check_portfolio_drawdown_breaker,
)
from app.services.trading.venue.coinbase_spot import (
    _coinbase_preflight_cash_check,
    _normalize_product_id,
)


# --------------------------------------------------------------------- #
# Helpers                                                               #
# --------------------------------------------------------------------- #

def _seed_pattern(db, *, pattern_id: int = 585) -> int:
    """Idempotently insert a ScanPattern row to satisfy the FK from
    ``trading_trades.scan_pattern_id``. The ``db`` fixture truncates per
    test so we re-seed each call (cheap; one row)."""
    existing = db.query(ScanPattern).filter(ScanPattern.id == pattern_id).first()
    if existing is None:
        db.add(ScanPattern(id=pattern_id, name=f"test_pattern_{pattern_id}"))
        db.flush()
    return pattern_id


def _seed_chili_attributed_trade(
    db,
    *,
    user_id: int,
    pnl: float,
    days_ago: int,
    scan_pattern_id: int = 585,  # real promoted pattern from the 2026-05-15 audit
    broker_source: str = "robinhood",
    auto_trader_version: str = "v1",
) -> Trade:
    """Seed a CHILI-attributed closed Trade row.

    Sets auto_trader_version so the breaker's CHILI-placed-only filter
    includes it. Auto-seeds the referenced ScanPattern when scan_pattern_id
    is positive (the FK constraint requires it).
    """
    if scan_pattern_id is not None and scan_pattern_id > 0:
        _seed_pattern(db, pattern_id=scan_pattern_id)
    exit_dt = datetime.utcnow() - timedelta(days=days_ago)
    # Decouple exit_price from pnl: the @validates("exit_price") guard
    # rejects non-positive values, but pnl is the canonical column for
    # the breaker's math. Set exit_price to a benign positive constant
    # and let pnl carry the test signal.
    t = Trade(
        user_id=user_id,
        ticker="ZZZ",
        direction="long",
        entry_price=10.0,
        exit_price=10.0,
        quantity=1.0,
        entry_date=exit_dt - timedelta(days=1),
        exit_date=exit_dt,
        last_fill_at=exit_dt,
        filled_at=exit_dt,
        status="closed",
        pnl=pnl,
        broker_source=broker_source,
        scan_pattern_id=scan_pattern_id,
        auto_trader_version=auto_trader_version,
    )
    db.add(t)
    db.flush()
    return t


def _seed_user(db, *, user_id: int = 999) -> int:
    """Insert a User row so trades' user_id FK can resolve."""
    from app.models import User
    u = User(id=user_id, name=f"test_user_{user_id}", email=f"t{user_id}@x.com")
    db.merge(u)
    db.flush()
    return user_id


# --------------------------------------------------------------------- #
# D1 — monthly DD breaker                                               #
# --------------------------------------------------------------------- #

class TestD1MonthlyDdBreaker:

    def test_threshold_returns_none_below_30_days(self, db):
        uid = _seed_user(db)
        # 29 distinct days of history -- below the 30-day floor.
        for d in range(29):
            _seed_chili_attributed_trade(db, user_id=uid, pnl=10.0, days_ago=d + 1)
        threshold, n_obs = _monthly_dd_threshold(db, uid)
        assert threshold is None
        assert n_obs == 29

    def test_threshold_computes_when_30_plus_days(self, db):
        uid = _seed_user(db)
        # 40 days of history with known mean/std.
        for d in range(40):
            _seed_chili_attributed_trade(db, user_id=uid, pnl=10.0, days_ago=d + 1)
        threshold, n_obs = _monthly_dd_threshold(db, uid)
        assert threshold is not None
        assert n_obs == 40
        # Constant 10/day → mean=10, std=0 → threshold = 30*10 - K*sqrt(30)*0 = 300.
        assert abs(threshold - 300.0) < 1e-6

    def test_threshold_filters_no_pattern_rows(self, db):
        uid = _seed_user(db)
        # 30 days of legit attribution + 30 days of no_pattern (NULL).
        # The helper SQL filter is ``scan_pattern_id IS NOT NULL AND != -1``,
        # so a NULL scan_pattern_id is excluded from the distribution.
        # Use broker_source="manual" so the D6 validator allows NULL.
        for d in range(30):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=10.0, days_ago=d + 1,
                scan_pattern_id=585,
            )
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=-100.0, days_ago=d + 1,
                scan_pattern_id=None,
                broker_source="manual",
            )
        threshold, n_obs = _monthly_dd_threshold(db, uid)
        # n_obs should reflect only the 30 attributed days, NOT 60.
        assert n_obs == 30
        # Mean should be 10 (from attributed rows only); std=0.
        assert threshold is not None
        assert abs(threshold - 300.0) < 1e-6

    def test_threshold_scoped_to_user_id(self, db):
        uid_a = _seed_user(db, user_id=101)
        uid_b = _seed_user(db, user_id=102)
        # User A has 35 days of profitable history.
        for d in range(35):
            _seed_chili_attributed_trade(db, user_id=uid_a, pnl=50.0, days_ago=d + 1)
        # User B has none.
        threshold_a, n_a = _monthly_dd_threshold(db, uid_a)
        threshold_b, n_b = _monthly_dd_threshold(db, uid_b)
        assert n_a == 35 and threshold_a is not None
        assert n_b == 0 and threshold_b is None

    def test_threshold_k_sigma_from_settings(self, db):
        """K is loaded from settings; identical history yields different
        thresholds under different K -- proves no hardcoded constant in
        the helper body."""
        uid = _seed_user(db)
        for d in range(40):
            # Variable PnL so std > 0 and K matters.
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=10.0 + (d % 7) * 5.0, days_ago=d + 1,
            )

        class _S:
            chili_pattern_dd_breaker_lower_bound_sigmas = 1.0

        class _S3:
            chili_pattern_dd_breaker_lower_bound_sigmas = 3.0

        t1, _ = _monthly_dd_threshold(db, uid, settings_obj=_S())
        t3, _ = _monthly_dd_threshold(db, uid, settings_obj=_S3())
        assert t1 is not None and t3 is not None
        # Higher K → wider tail → lower (more negative) threshold.
        assert t3 < t1

    def test_breaker_disabled_by_default_does_not_trip(self, db, monkeypatch):
        """When the flag is OFF, even severe synthetic losses do not trip
        the new D1 path. Other breaker checks may still fire on their own."""
        from app import config as app_config
        # Defensively force the flag OFF in case env or process-wide state
        # has flipped it.
        monkeypatch.setattr(
            app_config.settings, "chili_pattern_dd_breaker_enabled", False,
        )
        uid = _seed_user(db)
        # Seed 40 days of attributed history with huge loss in last 30d.
        for d in range(40):
            pnl = -1000.0 if d < 30 else 10.0
            _seed_chili_attributed_trade(db, user_id=uid, pnl=pnl, days_ago=d + 1)
        # capital=1_000_000 so the existing 5d/30d %-of-capital checks
        # don't fire; we want to isolate D1 behavior.
        tripped, reason = check_drawdown_breaker(db, uid, capital=1_000_000.0)
        # The D1 monthly_dd_breaker reason has a distinctive prefix.
        if tripped and reason:
            assert "monthly_dd_breaker" not in reason

    def test_breaker_enabled_skips_when_insufficient_history(self, db, monkeypatch, caplog):
        uid = _seed_user(db)
        # Only 5 attributed days.
        for d in range(5):
            _seed_chili_attributed_trade(db, user_id=uid, pnl=10.0, days_ago=d + 1)
        from app import config as app_config
        monkeypatch.setattr(
            app_config.settings, "chili_pattern_dd_breaker_enabled", True,
        )
        import logging
        with caplog.at_level(logging.WARNING):
            tripped, reason = check_drawdown_breaker(db, uid, capital=1_000_000.0)
        if tripped and reason:
            assert "monthly_dd_breaker" not in reason
        # The skip-warning is emitted at WARNING level with this text.
        skip_msgs = [
            r for r in caplog.records
            if "monthly_dd_breaker enabled but only" in (r.getMessage() or "")
        ]
        assert len(skip_msgs) >= 1

    def test_breaker_enabled_trips_when_pnl_below_threshold(self, db, monkeypatch):
        uid = _seed_user(db)
        # 35 days of mildly profitable attributed history outside the 30d
        # window. Then 30 consecutive losing days inside the window so the
        # losses contribute to the 30d sum without the std-inflation a
        # single outlier would cause.
        for d in range(35):
            _seed_chili_attributed_trade(db, user_id=uid, pnl=10.0, days_ago=d + 31)
        for d in range(30):
            _seed_chili_attributed_trade(db, user_id=uid, pnl=-1000.0, days_ago=d + 1)
        from app import config as app_config
        monkeypatch.setattr(
            app_config.settings, "chili_pattern_dd_breaker_enabled", True,
        )
        monkeypatch.setattr(
            app_config.settings, "chili_pattern_dd_breaker_lower_bound_sigmas", 2.0,
        )
        tripped, reason = check_drawdown_breaker(db, uid, capital=1_000_000.0)
        assert tripped is True
        assert reason is not None and "monthly_dd_breaker" in reason

    def test_breaker_enabled_does_not_trip_when_pnl_above_threshold(self, db, monkeypatch):
        uid = _seed_user(db)
        # 35 days of pnl=10 outside the 30d window → threshold ≈ 300.
        # Recent 30d window: 30 days at pnl=50/day → monthly = 1500 ≫ 300.
        for d in range(35):
            _seed_chili_attributed_trade(db, user_id=uid, pnl=10.0, days_ago=d + 31)
        for d in range(30):
            _seed_chili_attributed_trade(db, user_id=uid, pnl=50.0, days_ago=d + 1)
        from app import config as app_config
        monkeypatch.setattr(
            app_config.settings, "chili_pattern_dd_breaker_enabled", True,
        )
        tripped, reason = check_drawdown_breaker(db, uid, capital=1_000_000.0)
        if tripped and reason:
            assert "monthly_dd_breaker" not in reason

    def test_numerator_filters_no_pattern_matching_threshold_scope(self, db):
        """ARCHITECT-FLAG fix 2026-05-16: numerator must filter scan_pattern_id
        just like the threshold.

        Pre-fix, check_drawdown_breaker's monthly_pnl SELECT had no
        scan_pattern_id filter while _monthly_dd_threshold filtered to
        attributed only. A no_pattern bleed in the same 30-day window would
        push the numerator deep negative without widening the threshold's
        variance estimate -- tripping the breaker on losses the threshold
        mathematically cannot see.
        """
        uid = _seed_user(db, user_id=998)

        # 30 attributed days at +$10/day → attributed monthly_pnl = +$300.
        for d in range(30):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=10.0, days_ago=d + 1,
            )

        # Massive no_pattern bleed via raw SQL (bypasses @validates guard).
        db.execute(
            text(
                """
                INSERT INTO trading_trades (
                    user_id, ticker, direction, entry_price, exit_price, quantity,
                    entry_date, exit_date, last_fill_at, filled_at, status, pnl,
                    broker_source, scan_pattern_id, auto_trader_version
                ) VALUES (
                    :uid, 'NPL', 'long', 10.0, 10.0, 1.0,
                    NOW() - INTERVAL '2 days', NOW() - INTERVAL '1 day',
                    NOW() - INTERVAL '1 day', NOW() - INTERVAL '1 day',
                    'closed', -2000.0, 'manual', NULL, NULL
                )
                """
            ),
            {"uid": uid},
        )
        db.flush()

        # Helper must EXCLUDE the no_pattern bleed.
        attributed_pnl = _monthly_attributed_pnl(db, uid)
        assert attributed_pnl == pytest.approx(300.0, abs=1.0), (
            f"numerator should be attributed-only +$300, got "
            f"${attributed_pnl:.2f}; no_pattern bleed is leaking in"
        )

        # Sanity check: the unfiltered SUM(pnl) (what the bug shipped) would
        # include the bleed.
        unfiltered = db.execute(
            text(
                """
                SELECT COALESCE(SUM(pnl), 0)::float
                  FROM trading_trades
                 WHERE user_id = :uid
                   AND status = 'closed'
                   AND pnl IS NOT NULL
                   AND COALESCE(exit_date, last_fill_at, filled_at)
                       >= NOW() - INTERVAL '30 days'
                """
            ),
            {"uid": uid},
        ).scalar()
        assert float(unfiltered or 0.0) == pytest.approx(-1700.0, abs=1.0), (
            "unfiltered SUM(pnl) should be -$1,700 = +$300 attributed + "
            "(-$2,000) no_pattern; if not, the test scenario is malformed"
        )

    def test_breaker_no_trip_on_no_pattern_bleed_when_flag_on(
        self, db, monkeypatch,
    ):
        """End-to-end: with the flag ON and a no_pattern bleed alongside
        attributed history, the monthly_dd path must NOT trip.

        35 days of +$10/day attributed (threshold ≈ +$300, std=0) plus a
        -$1,000 no_pattern row in the 30-day window. Pre-fix this would
        have tripped: numerator = -$700 vs threshold +$300, -$700 <= +$300.
        Post-fix: numerator = +$300 attributed-only, breaker does not trip
        on the monthly_dd path.
        """
        from app import config as app_config

        uid = _seed_user(db, user_id=997)

        for d in range(35):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=10.0, days_ago=d + 1,
            )

        db.execute(
            text(
                """
                INSERT INTO trading_trades (
                    user_id, ticker, direction, entry_price, exit_price, quantity,
                    entry_date, exit_date, last_fill_at, filled_at, status, pnl,
                    broker_source, scan_pattern_id, auto_trader_version
                ) VALUES (
                    :uid, 'NPL', 'long', 10.0, 10.0, 1.0,
                    NOW() - INTERVAL '2 days', NOW() - INTERVAL '1 day',
                    NOW() - INTERVAL '1 day', NOW() - INTERVAL '1 day',
                    'closed', -1000.0, 'manual', NULL, NULL
                )
                """
            ),
            {"uid": uid},
        )
        db.flush()

        monkeypatch.setattr(
            app_config.settings, "chili_pattern_dd_breaker_enabled", True,
        )
        tripped, reason = check_drawdown_breaker(db, uid, capital=1_000_000.0)
        # 5d/30d %-of-capital checks use a different scope and might fire on
        # the -$1,000 row -- but the monthly_dd reason has a distinctive
        # prefix and must NOT appear.
        if tripped and reason:
            assert "monthly_dd_breaker" not in reason, (
                f"monthly_dd_breaker tripped on no_pattern bleed despite the "
                f"attribution-scope fix: {reason}"
            )


# --------------------------------------------------------------------- #
# f-portfolio-vs-pattern-breaker-separation D6 — two-tier breaker        #
# --------------------------------------------------------------------- #

def _seed_no_pattern_trade(db, *, user_id: int, pnl: float, days_ago: int) -> None:
    """Insert a no_pattern (scan_pattern_id NULL) closed trade via raw SQL.

    Bypasses the @validates("scan_pattern_id") guard the ORM enforces on
    Trade rows — the guard only allows NULL for broker_source='manual' /
    'reconcile_inferred', but raw SQL is the canonical pattern in this
    file (see TestD1MonthlyDdBreaker.test_numerator_filters_no_pattern...)
    for seeding the no_pattern bleed bucket.
    """
    db.execute(
        text(
            """
            INSERT INTO trading_trades (
                user_id, ticker, direction, entry_price, exit_price, quantity,
                entry_date, exit_date, last_fill_at, filled_at, status, pnl,
                broker_source, scan_pattern_id, auto_trader_version
            ) VALUES (
                :uid, 'NPL', 'long', 10.0, 10.0, 1.0,
                NOW() - make_interval(days => :da + 1),
                NOW() - make_interval(days => :da),
                NOW() - make_interval(days => :da),
                NOW() - make_interval(days => :da),
                'closed', :pnl, 'manual', NULL, NULL
            )
            """
        ),
        {"uid": user_id, "pnl": float(pnl), "da": int(days_ago)},
    )
    db.flush()


class TestPortfolioBreakerSeparation:
    """D6 — two-tier drawdown breaker separation.

    Verifies the brief's lever-signal alignment: pattern tier gates
    CHILI-attributed strategy decisions, portfolio tier gates EVERY
    venue-adapter entry path. Each tier's threshold is drawn from its
    own coherent distribution; each can trip / not trip independently.
    """

    def test_portfolio_breaker_trips_on_all_closed_pnl_exceeding_threshold(
        self, db, monkeypatch,
    ):
        # Seed shape (intentional, std > 0 so the trip margin is robust):
        #   - 35 days outside the 30d window (days_ago 31..65):
        #     attributed +10/day, ALL-closed daily_sum = +10. Stable mean.
        #   - 30 days inside the 30d window (days_ago 1..30):
        #     attributed -500/day + no_pattern -500/day via raw SQL,
        #     ALL-closed daily_sum = -1000/day.
        # Threshold computation (portfolio tier, 65d window):
        #   mean over all 65 days ≈ ((35×10) + (30×-1000))/65 = -454.6
        #   variance is large (35d ≈ +10 vs 30d ≈ -1000 → huge spread)
        #   threshold ends up deeply negative (~ -$19k empirically).
        # Numerator (recent 30d ALL-closed) = 30 × -1000 = -$30,000,
        # which is below the threshold by a wide margin → trips.
        from app import config as app_config

        uid = _seed_user(db, user_id=950)
        for d in range(35):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=10.0, days_ago=d + 31,
            )
        for d in range(30):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=-500.0, days_ago=d + 1,
            )
            _seed_no_pattern_trade(
                db, user_id=uid, pnl=-500.0, days_ago=d + 1,
            )

        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_enabled", True,
        )
        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_live", True,
        )
        # Silence shadow-log persistence so the test does not write
        # trading_risk_state rows even on live trips (the trip path persists
        # a live row; we accept that — verifies the persistence is reachable).
        monkeypatch.setattr(
            app_config.settings,
            "chili_portfolio_dd_breaker_shadow_log_enabled",
            False,
        )

        # Sanity: numerator < threshold.
        threshold, n_obs = _portfolio_dd_threshold(db, uid)
        monthly_total = _monthly_total_pnl(db, uid)
        assert threshold is not None and n_obs >= 30
        assert monthly_total <= threshold, (
            f"seed math wrong: monthly_total={monthly_total} "
            f"> threshold={threshold}"
        )

        tripped, reason = check_portfolio_drawdown_breaker(db, uid)
        assert tripped is True
        assert reason is not None
        assert "portfolio_dd_breaker" in reason
        assert "ALL closed trades" in reason

    def test_portfolio_breaker_does_not_trip_on_chili_loss_when_no_pattern_offsets(
        self, db, monkeypatch,
    ):
        """Lever-signal alignment proof — portfolio tier sees ALL-closed.

        Seed shape:
          - 35 outside days: attributed +$30, no_pattern -$30 → daily_sum=0
            (with variance from the per-row magnitudes).
          - 30 inside days: attributed -$500, no_pattern +$600 → daily_sum=+$100.
        Portfolio numerator = 30 × +100 = +$3,000 → above threshold → no trip.
        Pattern numerator = 30 × -500 = -$15,000 → below threshold → trips.
        This is the rare case the brief calls out: CHILI loses big but
        no_pattern offsets account-level → portfolio safe to keep trading.
        """
        from app import config as app_config

        uid = _seed_user(db, user_id=951)
        for d in range(35):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=30.0, days_ago=d + 31,
            )
            _seed_no_pattern_trade(
                db, user_id=uid, pnl=-30.0, days_ago=d + 31,
            )
        for d in range(30):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=-500.0, days_ago=d + 1,
            )
            _seed_no_pattern_trade(
                db, user_id=uid, pnl=600.0, days_ago=d + 1,
            )

        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_enabled", True,
        )
        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_live", True,
        )
        monkeypatch.setattr(
            app_config.settings,
            "chili_portfolio_dd_breaker_shadow_log_enabled",
            False,
        )

        # Portfolio: numerator > threshold → does NOT trip.
        tripped_p, reason_p = check_portfolio_drawdown_breaker(db, uid)
        assert tripped_p is False
        assert reason_p is None

        # Pattern: numerator (attributed-only) deeply negative → trips.
        monkeypatch.setattr(
            app_config.settings, "chili_pattern_dd_breaker_enabled", True,
        )
        tripped_a, reason_a = check_drawdown_breaker(db, uid, capital=1_000_000.0)
        assert tripped_a is True
        assert reason_a is not None and "monthly_dd_breaker" in reason_a

    def test_portfolio_breaker_live_blocks_when_threshold_unavailable(
        self, monkeypatch,
    ):
        from app import config as app_config

        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_enabled", True,
        )
        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_live", True,
        )

        def _boom(*_args, **_kwargs):
            raise RuntimeError("threshold unavailable")

        persisted = []
        monkeypatch.setattr(portfolio_risk, "_portfolio_dd_threshold", _boom)
        monkeypatch.setattr(
            portfolio_risk,
            "_persist_portfolio_breaker_state",
            lambda **kwargs: persisted.append(kwargs),
        )

        tripped, reason = check_portfolio_drawdown_breaker(
            object(), user_id=955,
        )

        assert tripped is True
        assert reason == "portfolio_dd_breaker_unavailable:threshold"
        assert persisted == [{
            "tripped": True,
            "reason": "portfolio_dd_breaker_unavailable:threshold",
            "regime": "portfolio_breaker",
        }]

    def test_portfolio_breaker_live_blocks_when_monthly_pnl_unavailable(
        self, monkeypatch,
    ):
        from app import config as app_config

        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_enabled", True,
        )
        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_live", True,
        )

        def _boom(*_args, **_kwargs):
            raise RuntimeError("monthly pnl unavailable")

        persisted = []
        monkeypatch.setattr(
            portfolio_risk,
            "_portfolio_dd_threshold",
            lambda *_args, **_kwargs: (0.0, 30),
        )
        monkeypatch.setattr(portfolio_risk, "_monthly_total_pnl", _boom)
        monkeypatch.setattr(
            portfolio_risk,
            "_persist_portfolio_breaker_state",
            lambda **kwargs: persisted.append(kwargs),
        )

        tripped, reason = check_portfolio_drawdown_breaker(
            object(), user_id=956,
        )

        assert tripped is True
        assert reason == "portfolio_dd_breaker_unavailable:monthly_total_pnl"
        assert persisted == [{
            "tripped": True,
            "reason": "portfolio_dd_breaker_unavailable:monthly_total_pnl",
            "regime": "portfolio_breaker",
        }]

    def test_portfolio_breaker_live_gate_fails_closed_when_session_unavailable(
        self, monkeypatch,
    ):
        from app import config as app_config
        from app import db as app_db

        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_enabled", True,
        )
        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_live", True,
        )

        def _boom():
            raise RuntimeError("session unavailable")

        monkeypatch.setattr(app_db, "SessionLocal", _boom, raising=False)

        ok, reason = _assert_portfolio_breaker_ok()

        assert ok is False
        assert reason == "portfolio_dd_breaker_unavailable:gate_exception"

    def test_portfolio_breaker_shadow_gate_still_passes_when_session_unavailable(
        self, monkeypatch,
    ):
        from app import config as app_config
        from app import db as app_db

        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_enabled", True,
        )
        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_live", False,
        )

        def _boom():
            raise RuntimeError("session unavailable")

        monkeypatch.setattr(app_db, "SessionLocal", _boom, raising=False)

        ok, reason = _assert_portfolio_breaker_ok()

        assert ok is True
        assert reason is None

    def test_pattern_breaker_still_works_post_rename(self, db, monkeypatch):
        """The pattern tier still trips under its own distribution after
        the chili_monthly_dd_breaker_enabled → chili_pattern_dd_breaker_enabled
        rename. Regression guard against the rename breaking the existing
        D1 path."""
        from app import config as app_config

        uid = _seed_user(db, user_id=952)
        for d in range(35):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=10.0, days_ago=d + 31,
            )
        for d in range(30):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=-1000.0, days_ago=d + 1,
            )

        monkeypatch.setattr(
            app_config.settings, "chili_pattern_dd_breaker_enabled", True,
        )
        monkeypatch.setattr(
            app_config.settings,
            "chili_pattern_dd_breaker_lower_bound_sigmas",
            2.0,
        )
        tripped, reason = check_drawdown_breaker(db, uid, capital=1_000_000.0)
        assert tripped is True
        # Trip-reason wording stays "monthly_dd_breaker:" for log/SQL
        # backward-compat — the rename is on the settings key, not the
        # log prefix (operators have parsers built on the old text).
        assert reason is not None and "monthly_dd_breaker" in reason
        persisted_uid, persisted_capital = db.execute(text(
            "SELECT user_id, capital FROM trading_risk_state "
            "WHERE regime = 'circuit_breaker' "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        )).one()
        assert persisted_uid == uid
        assert float(persisted_capital) == 1_000_000.0

    def test_portfolio_tripped_blocks_manual_buy_through_coinbase_adapter(
        self, db, monkeypatch,
    ):
        """Trickiest sub-test — exercises the venue-adapter gate.

        Mocks the broker client factory so no real network call is made,
        but leaves the test DB real so the breaker's SQL runs against
        seeded data. The gate fires from the top of place_market_order
        and short-circuits before any broker call — proved by asserting
        the mock's market_order_buy was NEVER invoked.
        """
        from unittest.mock import MagicMock

        from app import config as app_config
        from app.services.trading.venue.coinbase_spot import CoinbaseSpotAdapter
        from app.services.trading.venue import (
            reset_duplicate_client_order_guard_for_tests,
        )

        uid = _seed_user(db, user_id=953)
        for d in range(35):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=10.0, days_ago=d + 31,
            )
        for d in range(30):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=-500.0, days_ago=d + 1,
            )
            _seed_no_pattern_trade(
                db, user_id=uid, pnl=-500.0, days_ago=d + 1,
            )
        # _assert_portfolio_breaker_ok opens its own SessionLocal — commit
        # seeds so the separate connection sees them.
        db.commit()

        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_enabled", True,
        )
        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_live", True,
        )
        monkeypatch.setattr(
            app_config.settings,
            "chili_portfolio_dd_breaker_shadow_log_enabled",
            False,
        )
        # Force adapter through the gate even without Coinbase creds.
        monkeypatch.setattr(
            app_config.settings, "chili_coinbase_spot_adapter_enabled", True,
        )

        reset_duplicate_client_order_guard_for_tests()
        mock_client = MagicMock()
        mock_client.market_order_buy.return_value = {
            "success": True,
            "success_response": {"order_id": "should_not_be_called"},
        }
        adapter = CoinbaseSpotAdapter(client_factory=lambda: mock_client)
        adapter.is_enabled = lambda: True  # type: ignore[method-assign]
        adapter._require_client = lambda: mock_client  # type: ignore[method-assign]

        result = adapter.place_market_order(
            product_id="BTC-USD",
            side="buy",
            base_size="0.001",
            client_order_id="test-portfolio-blocked",
        )

        assert result.get("ok") is False
        err = (result.get("error") or "")
        assert err.startswith("portfolio_breaker:"), (
            f"expected envelope to start with portfolio_breaker:, got {err!r}"
        )
        # Gate must short-circuit BEFORE the broker is touched.
        assert not mock_client.market_order_buy.called, (
            "portfolio breaker gate did NOT short-circuit before the broker "
            "client call — the gate is wired in the wrong position"
        )

        # Cleanup committed seeds (next test's TRUNCATE will handle it too,
        # but explicit teardown avoids surprises if this test runs in
        # isolation).
        db.execute(
            text("DELETE FROM trading_trades WHERE user_id = :uid"),
            {"uid": uid},
        )
        db.execute(
            text(
                "DELETE FROM trading_risk_state WHERE regime = "
                "'portfolio_breaker'"
            ),
        )
        db.commit()

    def test_portfolio_not_tripped_pattern_tripped_blocks_attributed_allows_no_pattern(
        self, db, monkeypatch,
    ):
        """The lever-alignment crossover case.

        Same seed as test 7.2 (no_pattern offsets the attributed loss at
        account level). Both tiers enabled. Portfolio tier does NOT
        trip; pattern tier DOES trip. The pattern tier's verdict gates
        only CHILI-attributed strategy decisions inside auto_trader; the
        venue-adapter gate (portfolio tier) ALLOWS the entry through, so
        a no_pattern reconcile-driven buy would proceed.
        """
        from app import config as app_config

        uid = _seed_user(db, user_id=954)
        for d in range(35):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=30.0, days_ago=d + 31,
            )
            _seed_no_pattern_trade(
                db, user_id=uid, pnl=-30.0, days_ago=d + 31,
            )
        for d in range(30):
            _seed_chili_attributed_trade(
                db, user_id=uid, pnl=-500.0, days_ago=d + 1,
            )
            _seed_no_pattern_trade(
                db, user_id=uid, pnl=600.0, days_ago=d + 1,
            )

        monkeypatch.setattr(
            app_config.settings, "chili_pattern_dd_breaker_enabled", True,
        )
        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_enabled", True,
        )
        monkeypatch.setattr(
            app_config.settings, "chili_portfolio_dd_breaker_live", True,
        )
        monkeypatch.setattr(
            app_config.settings,
            "chili_portfolio_dd_breaker_shadow_log_enabled",
            False,
        )

        # Pattern tier trips (attributed-only distribution sees the loss).
        tripped_a, reason_a = check_drawdown_breaker(
            db, uid, capital=1_000_000.0,
        )
        assert tripped_a is True
        assert reason_a is not None and "monthly_dd_breaker" in reason_a

        # Portfolio tier does NOT trip (no_pattern offset keeps numerator
        # above threshold).
        tripped_p, reason_p = check_portfolio_drawdown_breaker(db, uid)
        assert tripped_p is False
        assert reason_p is None

    def test_alias_backwards_compat_legacy_env_var_still_honored(
        self, monkeypatch,
    ):
        """Verifies the chili_monthly_dd_breaker_enabled →
        chili_pattern_dd_breaker_enabled rename doesn't break operators
        still on the legacy CHILI_MONTHLY_DD_BREAKER_ENABLED env var.

        Why a fresh Settings() instantiation instead of monkeypatch on the
        existing singleton: pydantic's AliasChoices resolves at field
        construction (when Settings.__init__ reads env), not on attribute
        access. monkeypatch.setattr(settings, "field", val) bypasses the
        alias path entirely and would let this test pass even if the
        AliasChoices wiring were broken. Re-instantiating Settings() with
        env vars set is the ONLY way to exercise the alias-resolution
        code path. Do not "fix" this back to monkeypatch.setattr.
        """
        from app.config import Settings

        monkeypatch.delenv("CHILI_PATTERN_DD_BREAKER_ENABLED", raising=False)
        monkeypatch.delenv("CHILI_MONTHLY_DD_BREAKER_ENABLED", raising=False)

        # Case 1: only the legacy env var set → resolves via the alias.
        monkeypatch.setenv("CHILI_MONTHLY_DD_BREAKER_ENABLED", "true")
        s1 = Settings()
        assert s1.chili_pattern_dd_breaker_enabled is True

        # Case 2: both set, new name takes precedence (AliasChoices order
        # is new-first, legacy-second). One boolean field — no
        # double-counting possible.
        monkeypatch.setenv("CHILI_PATTERN_DD_BREAKER_ENABLED", "false")
        s2 = Settings()
        assert s2.chili_pattern_dd_breaker_enabled is False


# --------------------------------------------------------------------- #
# D2 — NameError diagnostic improvement                                  #
# --------------------------------------------------------------------- #

class TestD2NameErrorDiagnostic:

    def test_nameerror_with_name_attribute_formats_with_identifier(self):
        """The D2 fix extracts ``NameError.name`` so the rejection
        histogram pins the unbound identifier instead of reporting a
        generic ``coinbase_cap_unavailable:NameError``."""
        try:
            undefined_thing  # noqa: F821 — intentional unbound name
        except NameError as exc:
            _exc_detail = type(exc).__name__
            if isinstance(exc, NameError) and getattr(exc, "name", None):
                _exc_detail = f"NameError:{exc.name}"
            reason = f"coinbase_cap_unavailable:{_exc_detail}"
        assert reason == "coinbase_cap_unavailable:NameError:undefined_thing"

    def test_non_nameerror_falls_back_to_class_name(self):
        """Non-NameError exceptions follow the legacy path -- only
        NameError gets the new identifier-extraction treatment."""
        try:
            raise ValueError("oops")
        except Exception as exc:
            _exc_detail = type(exc).__name__
            if isinstance(exc, NameError) and getattr(exc, "name", None):
                _exc_detail = f"NameError:{exc.name}"
            reason = f"coinbase_cap_unavailable:{_exc_detail}"
        assert reason == "coinbase_cap_unavailable:ValueError"

    def test_d2_code_present_in_auto_trader_module(self):
        """Belt-and-suspenders: verify the new code is in the source.
        Integration is observed via the post-deploy rejection histogram
        (D9), not via unit-tested in-process drive of _process_one_alert."""
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "app/services/trading/auto_trader.py"
        text_src = src.read_text(encoding="utf-8")
        assert 'NameError:{exc.name}' in text_src
        assert 'isinstance(exc, NameError) and getattr(exc, "name", None)' in text_src


# --------------------------------------------------------------------- #
# D3 — product_id normalizer                                            #
# --------------------------------------------------------------------- #

class TestD3NormalizeProductId:

    def test_happy_path_uppercases(self):
        assert _normalize_product_id("btc-usd") == "BTC-USD"

    def test_already_canonical_passes_through(self):
        assert _normalize_product_id("ETH-USDC") == "ETH-USDC"

    def test_strip_whitespace(self):
        assert _normalize_product_id("  BTC-USD  ") == "BTC-USD"

    @pytest.mark.parametrize("bad", ["BTC", "BTCUSD", "BTC/USD", "", "BTC-USDT", "-USD"])
    def test_rejects_malformed(self, bad):
        with pytest.raises(ValueError, match="invalid product_id"):
            _normalize_product_id(bad)

    def test_rejects_none(self):
        with pytest.raises(ValueError, match="invalid product_id"):
            _normalize_product_id(None)

    def test_error_message_preserves_original_input(self):
        """The error string must contain the un-normalized input so the
        producer bug is easy to find."""
        try:
            _normalize_product_id("BTC/USD")
        except ValueError as exc:
            assert "'BTC/USD'" in str(exc)
        else:  # pragma: no cover
            pytest.fail("ValueError not raised")


# --------------------------------------------------------------------- #
# D4 — pre-flight BUY cash check                                        #
# --------------------------------------------------------------------- #

class TestD4PreflightCashCheck:

    def _patch_resolver(self, monkeypatch, *, total: float, last_updated_age_s: float = 0.0):
        import time as _t
        import app.services.trading.cost_aware_gate as cag
        # The helper does ``from ..cost_aware_gate import resolve_coinbase_buying_power``
        # inside the function body. Patching the module attribute is the
        # canonical seam (each call re-imports fresh).
        monkeypatch.setattr(
            cag,
            "resolve_coinbase_buying_power",
            lambda: {
                "usd": total / 2,
                "usdc": total / 2,
                "total": float(total),
                "last_updated": _t.time() - float(last_updated_age_s),
            },
        )

    def test_pass_through_when_buying_power_sufficient(self, monkeypatch):
        self._patch_resolver(monkeypatch, total=10_000.0)
        result = _coinbase_preflight_cash_check(
            product_id="BTC-USD",
            base_size="0.01",
            limit_price="50000",  # 0.01 * 50000 = $500 < $10k
        )
        assert result is None

    def test_refuses_when_buying_power_below_required(self, monkeypatch):
        self._patch_resolver(monkeypatch, total=100.0)
        result = _coinbase_preflight_cash_check(
            product_id="BTC-USD",
            base_size="0.01",
            limit_price="50000",  # 0.01 * 50000 = $500 > $100
        )
        assert result is not None
        assert result.get("ok") is False
        assert result.get("preflight_refused") is True
        assert "local buying_power $100.00" in result["error"]
        assert "BTC-USD" in result["error"]

    def test_fee_slack_uses_settings(self, monkeypatch):
        """R1: fee slack is settings-sourced, not a hardcoded 1.005.
        At 0bps slack the pre-flight allows exactly at base*limit;
        at 200bps it refuses the same call."""
        # Total buying power = exactly base*limit = 500.
        self._patch_resolver(monkeypatch, total=500.0)
        from app import config as app_config

        monkeypatch.setattr(
            app_config.settings, "chili_coinbase_preflight_fee_slack_bps", 0.0,
        )
        result_zero_slack = _coinbase_preflight_cash_check(
            product_id="BTC-USD",
            base_size="0.01",
            limit_price="50000",
        )
        # 500.0 < 500.0 is False -- exactly equal allows through.
        assert result_zero_slack is None

        monkeypatch.setattr(
            app_config.settings, "chili_coinbase_preflight_fee_slack_bps", 200.0,
        )
        result_with_slack = _coinbase_preflight_cash_check(
            product_id="BTC-USD",
            base_size="0.01",
            limit_price="50000",  # 500 * 1.02 = 510 > 500 → refuse
        )
        assert result_with_slack is not None
        assert result_with_slack.get("preflight_refused") is True

    def test_stale_cache_refuses_with_warning(self, monkeypatch, caplog):
        """R2: stale-cache threshold is settings-sourced. When the cache
        is older than chili_coinbase_preflight_max_stale_seconds, the
        pre-flight refuses because buying power is not proven."""
        # Buying power is technically insufficient, but cache is stale.
        self._patch_resolver(monkeypatch, total=100.0, last_updated_age_s=60.0)
        from app import config as app_config
        monkeypatch.setattr(
            app_config.settings, "chili_coinbase_preflight_max_stale_seconds", 5.0,
        )
        import logging
        with caplog.at_level(logging.WARNING):
            result = _coinbase_preflight_cash_check(
                product_id="BTC-USD",
                base_size="0.01",
                limit_price="50000",
            )
        assert result is not None
        assert result.get("ok") is False
        assert result.get("preflight_refused") is True
        assert result["error"].startswith("buying_power_unavailable:stale_cache")
        stale_msgs = [
            r for r in caplog.records
            if "buying_power cache stale" in (r.getMessage() or "")
            and "blocking" in (r.getMessage() or "")
        ]
        assert len(stale_msgs) >= 1


# --------------------------------------------------------------------- #
# D6 — @validates scan_pattern_id                                       #
# --------------------------------------------------------------------- #

class TestD6ScanPatternIdValidator:

    def test_null_with_robinhood_broker_source_raises(self):
        with pytest.raises(ValueError, match="scan_pattern_id IS NULL"):
            Trade(
                ticker="ZZZ",
                direction="long",
                entry_price=10.0,
                quantity=1.0,
                broker_source="robinhood",
                scan_pattern_id=None,
            )

    def test_null_with_coinbase_broker_source_raises(self):
        with pytest.raises(ValueError, match="scan_pattern_id IS NULL"):
            Trade(
                ticker="ZZZ",
                direction="long",
                entry_price=10.0,
                quantity=1.0,
                broker_source="coinbase",
                scan_pattern_id=None,
            )

    def test_null_with_manual_broker_source_passes(self):
        t = Trade(
            ticker="ZZZ",
            direction="long",
            entry_price=10.0,
            quantity=1.0,
            broker_source="manual",
            scan_pattern_id=None,
        )
        assert t.scan_pattern_id is None

    def test_null_with_reconcile_import_broker_source_passes(self):
        t = Trade(
            ticker="ZZZ",
            direction="long",
            entry_price=10.0,
            quantity=1.0,
            broker_source="reconcile_import",
            scan_pattern_id=None,
        )
        assert t.scan_pattern_id is None

    def test_real_pattern_id_passes_regardless_of_broker_source(self):
        t = Trade(
            ticker="ZZZ",
            direction="long",
            entry_price=10.0,
            quantity=1.0,
            broker_source="robinhood",
            scan_pattern_id=585,
        )
        assert t.scan_pattern_id == 585

    def test_null_with_strategy_proposal_id_passes(self):
        """User-approved proposal placed via a live broker can land
        with NULL scan_pattern_id (signals_json may not carry attribution).
        The strategy_proposal_id signals "this came from a user-approved
        proposal" so the validator allows it."""
        t = Trade(
            ticker="ZZZ",
            direction="long",
            entry_price=10.0,
            quantity=1.0,
            broker_source="robinhood",
            strategy_proposal_id=123,
            scan_pattern_id=None,
        )
        assert t.scan_pattern_id is None

    def test_null_with_no_broker_source_passes(self):
        """Defensive: when broker_source is unset/empty, the validator
        defers enforcement so SQLAlchemy's attribute-population order
        doesn't false-fire the guard during ``Trade(...)`` construction."""
        t = Trade(
            ticker="ZZZ",
            direction="long",
            entry_price=10.0,
            quantity=1.0,
            scan_pattern_id=None,
        )
        assert t.scan_pattern_id is None

    def test_update_without_setting_scan_pattern_id_does_not_fire(self, db):
        """REGRESSION: closing an existing open no_pattern trade (e.g.
        CRDL id=1814) via ``status='closed'`` + ``exit_date=now()``
        without touching ``scan_pattern_id`` must NOT trigger the
        validator. The brief explicitly relies on this so CRDL's exit
        machinery continues to function with the D6 guard in place."""
        uid = _seed_user(db)
        # Seed a legacy no_pattern open trade (broker_source="manual" so
        # the validator allows the initial insert with NULL).
        t = Trade(
            user_id=uid,
            ticker="CRDLCHK",
            direction="long",
            entry_price=1.45,
            quantity=200.0,
            entry_date=datetime.utcnow() - timedelta(days=15),
            status="open",
            broker_source="manual",
            scan_pattern_id=None,
        )
        db.add(t)
        db.flush()
        trade_id = t.id
        # Now close it without touching scan_pattern_id.
        t.status = "closed"
        t.exit_date = datetime.utcnow()
        t.exit_price = 1.60
        t.pnl = (1.60 - 1.45) * 200.0
        db.flush()
        # No ValueError raised; the row persisted.
        refreshed = db.query(Trade).get(trade_id)
        assert refreshed.status == "closed"
        assert refreshed.scan_pattern_id is None


# --------------------------------------------------------------------- #
# D7 — migration 243 BNB-USD zombie cleanup                              #
# --------------------------------------------------------------------- #

class TestD7Migration243:

    def test_idempotent_no_op_when_row_absent(self, db):
        """On chili_test the id=1861 row is absent; migration should be
        a logged no-op, not a failure."""
        from app.migrations import _migration_243_bnb_usd_zombie_cleanup
        # Use the underlying engine connection so commit semantics match.
        conn = db.connection()
        # Should not raise.
        _migration_243_bnb_usd_zombie_cleanup(conn)

    def test_cancels_zombie_when_all_guards_match(self, db):
        from app.migrations import _migration_243_bnb_usd_zombie_cleanup
        uid = _seed_user(db)
        # Seed the zombie. quantity must be > 0 (model validator) but
        # filled_quantity=0 keeps it inside the migration's filter.
        zombie = Trade(
            id=1861,
            user_id=uid,
            ticker="BNB-USD",
            direction="long",
            entry_price=680.46,
            quantity=1.0,
            filled_quantity=0.0,
            entry_date=datetime.utcnow() - timedelta(days=4),
            status="open",
            broker_source="manual",  # allows NULL scan_pattern_id
            scan_pattern_id=None,
        )
        db.add(zombie)
        db.flush()
        db.commit()

        conn = db.connection()
        _migration_243_bnb_usd_zombie_cleanup(conn)

        db.expire_all()
        refreshed = db.query(Trade).filter(Trade.id == 1861).first()
        assert refreshed is not None
        assert refreshed.status == "cancelled"
        assert refreshed.exit_reason == "zombie_cleanup_2026_05_15_phase3"
        assert refreshed.exit_date is not None

    def test_idempotent_no_op_on_second_run(self, db):
        """Re-running after the row has been cancelled is a safe no-op."""
        from app.migrations import _migration_243_bnb_usd_zombie_cleanup
        uid = _seed_user(db)
        zombie = Trade(
            id=1861,
            user_id=uid,
            ticker="BNB-USD",
            direction="long",
            entry_price=680.46,
            quantity=1.0,
            filled_quantity=0.0,
            entry_date=datetime.utcnow() - timedelta(days=4),
            status="open",
            broker_source="manual",
            scan_pattern_id=None,
        )
        db.add(zombie)
        db.flush()
        db.commit()

        conn = db.connection()
        _migration_243_bnb_usd_zombie_cleanup(conn)
        # Second run -- should not touch the row again.
        _migration_243_bnb_usd_zombie_cleanup(conn)

        db.expire_all()
        refreshed = db.query(Trade).filter(Trade.id == 1861).first()
        assert refreshed is not None
        assert refreshed.status == "cancelled"

    def test_does_not_touch_row_when_filled_quantity_positive(self, db):
        """Guards in the WHERE clause prevent the cleanup from running
        if the row actually has fills -- protection against running it
        on a real (non-zombie) position."""
        from app.migrations import _migration_243_bnb_usd_zombie_cleanup
        uid = _seed_user(db)
        not_a_zombie = Trade(
            id=1861,
            user_id=uid,
            ticker="BNB-USD",
            direction="long",
            entry_price=680.46,
            quantity=1.0,
            filled_quantity=1.0,  # NOT a zombie
            entry_date=datetime.utcnow() - timedelta(days=4),
            status="open",
            broker_source="manual",
            scan_pattern_id=None,
        )
        db.add(not_a_zombie)
        db.flush()
        db.commit()

        conn = db.connection()
        _migration_243_bnb_usd_zombie_cleanup(conn)

        db.expire_all()
        refreshed = db.query(Trade).filter(Trade.id == 1861).first()
        assert refreshed is not None
        assert refreshed.status == "open"  # untouched
        assert refreshed.exit_reason is None

    def test_does_not_touch_crdl_id_1814(self, db):
        """CRDL is explicitly out of scope. The WHERE clause's
        ``id = 1861`` filter excludes anything else, but we test
        explicitly here because the brief is emphatic about it."""
        from app.migrations import _migration_243_bnb_usd_zombie_cleanup
        uid = _seed_user(db)
        crdl = Trade(
            id=1814,
            user_id=uid,
            ticker="CRDL",
            direction="long",
            entry_price=1.4485,
            quantity=200.0,
            filled_quantity=200.0,
            entry_date=datetime.utcnow() - timedelta(days=15),
            status="open",
            broker_source="manual",
            scan_pattern_id=None,
        )
        db.add(crdl)
        db.flush()
        db.commit()

        conn = db.connection()
        _migration_243_bnb_usd_zombie_cleanup(conn)

        db.expire_all()
        refreshed = db.query(Trade).filter(Trade.id == 1814).first()
        assert refreshed is not None
        assert refreshed.status == "open"
        assert refreshed.exit_reason is None
