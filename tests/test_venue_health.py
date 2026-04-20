"""P1.2 — venue health + circuit breaker tests.

Verifies the rolling health summary + degraded gate built on top of the
``TradingExecutionEvent`` stream. Headline guarantees:

* Feature flag off → ``status == "disabled"`` and every gate returns
  False/None so an unwired environment behaves exactly as before.
* With the flag on, the breaker trips when:
    - ``error_rate`` crosses the configured threshold,
    - ack-to-fill P95 latency crosses its threshold,
    - submit-to-ack P95 latency crosses its threshold.
  ``error_rate`` is evaluated FIRST because a venue rejecting every order
  is the clearest "stop" signal.
* Sample floor (``min_samples``) short-circuits to
  ``insufficient_data`` so the breaker never fires on a single unlucky
  latency observation.
* ``record_rate_limit_event`` writes a ``rate_limit`` row and never
  raises — a bookkeeping failure cannot bubble up into the caller's
  order-placement path.
* Rate-limit hits count toward ``error_rate`` because operationally both
  are "stop sending new orders here" signals.
* The canonical venue map folds ``"coinbase_spot"`` / ``"crypto"`` /
  ``"coinbase"`` onto the single ``coinbase`` breaker key.
* Events outside the rolling window are excluded.
* When ``venue`` is NULL the summary still finds events via the
  ``broker_source`` fallback.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.trading import TradingExecutionEvent
from app.services.trading.venue import venue_health


# ── Settings fixtures ────────────────────────────────────────────────────


@pytest.fixture()
def _enable_health(monkeypatch):
    """Turn on the feature flag with predictable thresholds.

    Thresholds pinned tight enough to trip deterministically inside the
    test data:
        * window: 300s (matches default)
        * min_samples: 3 (small so we don't need to forge many rows)
        * ack_to_fill_p95_ms: 5000
        * submit_to_ack_p95_ms: 3000
        * error_rate: 0.10 (10%)
    """
    from app.config import settings

    monkeypatch.setattr(settings, "chili_venue_health_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_venue_health_window_sec", 300, raising=False)
    monkeypatch.setattr(settings, "chili_venue_health_min_samples", 3, raising=False)
    monkeypatch.setattr(settings, "chili_venue_health_ack_to_fill_p95_ms", 5000, raising=False)
    monkeypatch.setattr(settings, "chili_venue_health_submit_to_ack_p95_ms", 3000, raising=False)
    monkeypatch.setattr(settings, "chili_venue_health_error_rate_pct", 0.10, raising=False)
    monkeypatch.setattr(settings, "chili_venue_health_auto_switch_to_paper", False, raising=False)
    yield


def _mk_event(
    db,
    *,
    venue: str = "coinbase",
    event_type: str = "fill",
    status: str = "filled",
    submit_to_ack_ms: float | None = 100.0,
    ack_to_first_fill_ms: float | None = 200.0,
    recorded_at: datetime | None = None,
    ticker: str = "BTC-USD",
    broker_source: str | None = None,
) -> TradingExecutionEvent:
    """Forge a TradingExecutionEvent row with sensible defaults."""
    row = TradingExecutionEvent(
        ticker=ticker,
        venue=venue,
        broker_source=broker_source if broker_source is not None else venue,
        event_type=event_type,
        status=status,
        submit_to_ack_ms=submit_to_ack_ms,
        ack_to_first_fill_ms=ack_to_first_fill_ms,
        recorded_at=recorded_at or datetime.utcnow(),
        payload_json={},
    )
    db.add(row)
    db.flush()
    return row


# ── canonicalize_venue ───────────────────────────────────────────────────


class TestCanonicalizeVenue:
    """The breaker keys on canonical venue strings — so two adapter
    spellings for the same exchange share one circuit."""

    def test_coinbase_spot_folds_to_coinbase(self):
        assert venue_health.canonicalize_venue("coinbase_spot") == "coinbase"

    def test_crypto_folds_to_coinbase(self):
        """Legacy ``broker_source = "crypto"`` still routes to coinbase."""
        assert venue_health.canonicalize_venue("crypto") == "coinbase"

    def test_robinhood_spot_folds_to_robinhood(self):
        assert venue_health.canonicalize_venue("robinhood_spot") == "robinhood"

    def test_equities_folds_to_robinhood(self):
        assert venue_health.canonicalize_venue("equities") == "robinhood"

    def test_case_and_whitespace_normalized(self):
        assert venue_health.canonicalize_venue("  Coinbase_Spot  ") == "coinbase"
        assert venue_health.canonicalize_venue("ROBINHOOD") == "robinhood"

    def test_manual_passes_through(self):
        assert venue_health.canonicalize_venue("manual") == "manual"

    def test_none_returns_unknown(self):
        assert venue_health.canonicalize_venue(None) == "unknown"
        assert venue_health.canonicalize_venue("") == "unknown"

    def test_unknown_raw_passes_through_lowered(self):
        """A raw spelling not in the map stays as-is (lowered) so new
        venues don't need a code change before they show up in metrics."""
        assert venue_health.canonicalize_venue("kraken") == "kraken"


# ── Feature flag behavior ────────────────────────────────────────────────


class TestFeatureFlag:
    def test_summary_disabled_when_flag_off(self, db, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "chili_venue_health_enabled", False, raising=False)
        summary = venue_health.summarize_venue(db, venue="coinbase")
        assert summary["status"] == "disabled"
        assert summary["venue"] == "coinbase"
        # Frozen shape — every key present even when disabled.
        assert set(summary.keys()) >= {
            "venue", "window_sec", "samples", "status", "reason",
            "submit_to_ack_p50_ms", "submit_to_ack_p95_ms",
            "ack_to_fill_p50_ms", "ack_to_fill_p95_ms",
            "error_rate", "rate_limit_rate",
            "n_events", "n_errors", "n_rate_limits", "n_acks", "n_fills",
            "thresholds",
        }

    def test_is_venue_degraded_false_when_flag_off(self, db, monkeypatch):
        """Flag off is a hard bypass — no summary query, no degraded result."""
        from app.config import settings

        monkeypatch.setattr(settings, "chili_venue_health_enabled", False, raising=False)
        # Even if we insert clearly-bad data, flag-off suppresses.
        _mk_event(db, event_type="reject", status="rejected")
        _mk_event(db, event_type="reject", status="rejected")
        db.flush()

        assert venue_health.is_venue_degraded(db, venue="coinbase") is False
        assert venue_health.venue_degraded_reason(db, venue="coinbase") is None

    def test_should_auto_switch_false_when_toggle_off(self, db, _enable_health, monkeypatch):
        """Auto-switch is its own toggle; degraded alone doesn't flip modes."""
        from app.config import settings

        monkeypatch.setattr(settings, "chili_venue_health_auto_switch_to_paper", False, raising=False)
        # Force degraded — 3 rejects, 0 fills → 100% error rate.
        for _ in range(3):
            _mk_event(db, event_type="reject", status="rejected",
                      submit_to_ack_ms=None, ack_to_first_fill_ms=None)
        db.flush()

        assert venue_health.is_venue_degraded(db, venue="coinbase") is True
        assert venue_health.should_auto_switch_to_paper(db, venue="coinbase") is False


# ── Insufficient data → no breaker trip ─────────────────────────────────


class TestInsufficientData:
    def test_empty_stream_returns_insufficient_data(self, db, _enable_health):
        """No events in the window → ``insufficient_data`` not ``degraded``.

        This is the cold-start case: we should NOT block entries just
        because nobody has traded yet today.
        """
        summary = venue_health.summarize_venue(db, venue="coinbase")
        assert summary["status"] == "insufficient_data"
        assert summary["samples"] == 0
        assert venue_health.is_venue_degraded(db, venue="coinbase") is False

    def test_below_min_samples_returns_insufficient_data(self, db, _enable_health):
        """Two fills when min_samples=3 → still insufficient_data.

        Floor prevents a single bad latency from flipping the breaker.
        """
        _mk_event(db, submit_to_ack_ms=80, ack_to_first_fill_ms=150)
        _mk_event(db, submit_to_ack_ms=120, ack_to_first_fill_ms=9999)  # would breach if counted
        db.flush()

        summary = venue_health.summarize_venue(db, venue="coinbase")
        assert summary["status"] == "insufficient_data"
        assert summary["samples"] == 2
        # Reason carries the sample count for the operator log.
        assert "samples=2" in (summary["reason"] or "")
        assert venue_health.is_venue_degraded(db, venue="coinbase") is False


# ── Healthy vs degraded classification ──────────────────────────────────


class TestHealthyClassification:
    def test_normal_latencies_are_healthy(self, db, _enable_health):
        """5 fills well under threshold → healthy with reason=None."""
        for _ in range(5):
            _mk_event(db, submit_to_ack_ms=100, ack_to_first_fill_ms=200)
        db.flush()

        summary = venue_health.summarize_venue(db, venue="coinbase")
        assert summary["status"] == "healthy"
        assert summary["reason"] is None
        assert summary["samples"] == 5
        assert summary["submit_to_ack_p95_ms"] == 100.0
        assert summary["ack_to_fill_p95_ms"] == 200.0
        assert summary["error_rate"] == 0.0
        assert venue_health.is_venue_degraded(db, venue="coinbase") is False

    def test_two_venues_independent(self, db, _enable_health):
        """Coinbase degraded doesn't make Robinhood degraded."""
        # 3 healthy coinbase fills.
        for _ in range(3):
            _mk_event(db, venue="coinbase", submit_to_ack_ms=100, ack_to_first_fill_ms=200)
        # 3 degraded robinhood rejects.
        for _ in range(3):
            _mk_event(db, venue="robinhood", event_type="reject", status="rejected",
                      submit_to_ack_ms=None, ack_to_first_fill_ms=None)
        db.flush()

        assert venue_health.is_venue_degraded(db, venue="coinbase") is False
        assert venue_health.is_venue_degraded(db, venue="robinhood") is True


class TestDegradedOnErrorRate:
    def test_high_reject_ratio_trips_error_rate(self, db, _enable_health):
        """10% error threshold: 2 rejects + 4 fills = 33% → degraded."""
        for _ in range(4):
            _mk_event(db, submit_to_ack_ms=100, ack_to_first_fill_ms=200)
        for _ in range(2):
            _mk_event(db, event_type="reject", status="rejected",
                      submit_to_ack_ms=None, ack_to_first_fill_ms=None)
        db.flush()

        summary = venue_health.summarize_venue(db, venue="coinbase")
        assert summary["status"] == "degraded"
        assert "error_rate=" in summary["reason"]
        assert summary["n_errors"] == 2
        assert venue_health.is_venue_degraded(db, venue="coinbase") is True

    def test_rejected_by_status_also_counts(self, db, _enable_health):
        """A row with event_type='ack' but status='failed' still counts as error."""
        for _ in range(4):
            _mk_event(db, submit_to_ack_ms=100, ack_to_first_fill_ms=200)
        # event_type is not "reject", but status is in the REJECT_STATUSES set.
        for _ in range(2):
            _mk_event(db, event_type="ack", status="failed",
                      submit_to_ack_ms=None, ack_to_first_fill_ms=None)
        db.flush()

        summary = venue_health.summarize_venue(db, venue="coinbase")
        assert summary["status"] == "degraded"
        assert summary["n_errors"] == 2

    def test_rate_limit_events_count_as_errors(self, db, _enable_health):
        """A rate-limit hit IS an operational "stop" signal even though
        the venue didn't reject."""
        for _ in range(4):
            _mk_event(db, submit_to_ack_ms=100, ack_to_first_fill_ms=200)
        for _ in range(2):
            _mk_event(db, event_type="rate_limit", status="cb_place_market",
                      submit_to_ack_ms=None, ack_to_first_fill_ms=None)
        db.flush()

        summary = venue_health.summarize_venue(db, venue="coinbase")
        assert summary["status"] == "degraded"
        assert summary["n_rate_limits"] == 2
        assert summary["rate_limit_rate"] > 0.0


class TestDegradedOnLatency:
    def test_ack_to_fill_p95_breach_trips(self, db, _enable_health):
        """5 fast + 1 very slow ack-to-fill → P95 pushed over threshold."""
        # Below threshold (5000ms).
        for _ in range(5):
            _mk_event(db, submit_to_ack_ms=100, ack_to_first_fill_ms=200)
        # One outlier that pushes P95 past the 5000ms threshold.
        _mk_event(db, submit_to_ack_ms=100, ack_to_first_fill_ms=9000)
        db.flush()

        summary = venue_health.summarize_venue(db, venue="coinbase")
        assert summary["status"] == "degraded"
        assert "ack_to_fill_p95_ms=" in summary["reason"]
        assert summary["ack_to_fill_p95_ms"] == 9000.0

    def test_submit_to_ack_p95_breach_trips(self, db, _enable_health):
        """Large submit-to-ack P95 trips even when ack-to-fill is fine."""
        # Fill values fine (under 5000ms) but submit_to_ack pushed over 3000ms.
        for _ in range(5):
            _mk_event(db, submit_to_ack_ms=100, ack_to_first_fill_ms=200)
        _mk_event(db, submit_to_ack_ms=5000, ack_to_first_fill_ms=200)
        db.flush()

        summary = venue_health.summarize_venue(db, venue="coinbase")
        assert summary["status"] == "degraded"
        assert "submit_to_ack_p95_ms=" in summary["reason"]
        assert summary["submit_to_ack_p95_ms"] == 5000.0


class TestEvaluationOrder:
    def test_error_rate_wins_over_latency(self, db, _enable_health):
        """When both conditions breach, error_rate is reported.

        Error rate is the stronger "stop" signal — a venue rejecting
        orders is more urgent than a slow one.
        """
        # 4 slow fills + 2 rejects. Both would trip the breaker independently.
        for _ in range(4):
            _mk_event(db, submit_to_ack_ms=100, ack_to_first_fill_ms=9000)
        for _ in range(2):
            _mk_event(db, event_type="reject", status="rejected",
                      submit_to_ack_ms=None, ack_to_first_fill_ms=None)
        db.flush()

        summary = venue_health.summarize_venue(db, venue="coinbase")
        assert summary["status"] == "degraded"
        # Error rate evaluated first → reason mentions error_rate, not latency.
        assert summary["reason"].startswith("error_rate=")


# ── Window + venue filtering ────────────────────────────────────────────


class TestWindowFiltering:
    def test_events_outside_window_excluded(self, db, _enable_health):
        """Window is 300s by default; events 600s old must NOT count.

        Headline guarantee: the breaker is a ROLLING window, not a
        forever-accumulating counter — a burst of errors at market
        open shouldn't lock the venue for the rest of the day.
        """
        old = datetime.utcnow() - timedelta(seconds=600)
        # 10 rejects 10 minutes ago (outside the 5-min window).
        for _ in range(10):
            _mk_event(db, event_type="reject", status="rejected",
                      submit_to_ack_ms=None, ack_to_first_fill_ms=None,
                      recorded_at=old)
        # 3 good fills now.
        for _ in range(3):
            _mk_event(db, submit_to_ack_ms=100, ack_to_first_fill_ms=200)
        db.flush()

        summary = venue_health.summarize_venue(db, venue="coinbase")
        # Old rejects excluded → status must be healthy.
        assert summary["status"] == "healthy"
        assert summary["n_errors"] == 0
        assert summary["samples"] == 3

    def test_broker_source_fallback_when_venue_null(self, db, _enable_health):
        """Older rows with NULL venue but populated broker_source must
        still be findable via the canonical venue key."""
        # Legacy row: venue=None, broker_source='coinbase'.
        for _ in range(3):
            row = TradingExecutionEvent(
                ticker="BTC-USD",
                venue=None,
                broker_source="coinbase",
                event_type="fill",
                status="filled",
                submit_to_ack_ms=100,
                ack_to_first_fill_ms=200,
                recorded_at=datetime.utcnow(),
                payload_json={},
            )
            db.add(row)
        db.flush()

        summary = venue_health.summarize_venue(db, venue="coinbase")
        assert summary["samples"] == 3
        assert summary["status"] == "healthy"


# ── Gate helpers ─────────────────────────────────────────────────────────


class TestGateHelpers:
    def test_venue_degraded_reason_matches_summary(self, db, _enable_health):
        """``venue_degraded_reason`` returns the same string as ``summary['reason']``."""
        for _ in range(3):
            _mk_event(db, event_type="reject", status="rejected",
                      submit_to_ack_ms=None, ack_to_first_fill_ms=None)
        db.flush()

        summary = venue_health.summarize_venue(db, venue="coinbase")
        assert summary["status"] == "degraded"

        reason = venue_health.venue_degraded_reason(db, venue="coinbase")
        assert reason == summary["reason"]

    def test_venue_degraded_reason_none_when_healthy(self, db, _enable_health):
        for _ in range(3):
            _mk_event(db, submit_to_ack_ms=100, ack_to_first_fill_ms=200)
        db.flush()

        assert venue_health.venue_degraded_reason(db, venue="coinbase") is None

    def test_should_auto_switch_requires_both_flag_and_degraded(self, db, _enable_health, monkeypatch):
        """auto_switch_to_paper=True AND degraded → True. Otherwise → False."""
        from app.config import settings

        monkeypatch.setattr(settings, "chili_venue_health_auto_switch_to_paper", True, raising=False)
        # Healthy → False even with toggle on.
        for _ in range(3):
            _mk_event(db, submit_to_ack_ms=100, ack_to_first_fill_ms=200)
        db.flush()
        assert venue_health.should_auto_switch_to_paper(db, venue="coinbase") is False

        # Flip to degraded — 3 rejects on robinhood (independent of coinbase above).
        for _ in range(3):
            _mk_event(db, venue="robinhood", event_type="reject", status="rejected",
                      submit_to_ack_ms=None, ack_to_first_fill_ms=None)
        db.flush()
        assert venue_health.should_auto_switch_to_paper(db, venue="robinhood") is True


# ── Rate-limit event recorder ───────────────────────────────────────────


class TestRecordRateLimitEvent:
    def test_writes_rate_limit_row(self, db, _enable_health):
        """``record_rate_limit_event`` persists a row with event_type='rate_limit'."""
        # Opens its own SessionLocal → the test's own `db` is a separate
        # session, but commits are visible across them.
        venue_health.record_rate_limit_event(
            venue="coinbase_spot",
            ticker="BTC-USD",
            source="cb_place_market",
        )

        # `record_rate_limit_event` opens its own SessionLocal and commits;
        # our test `db` is a separate session so we read directly via SQL.
        from sqlalchemy import text
        rows = db.execute(text("""
            SELECT venue, broker_source, event_type, status, ticker
            FROM trading_execution_events
            WHERE event_type = 'rate_limit'
        """)).fetchall()

        assert len(rows) >= 1
        match = [r for r in rows if r[4] == "BTC-USD"]
        assert len(match) == 1
        venue, broker_source, event_type, status, ticker = match[0]
        assert venue == "coinbase"  # canonicalized
        assert broker_source == "coinbase"
        assert event_type == "rate_limit"
        assert status == "cb_place_market"
        assert ticker == "BTC-USD"

    def test_missing_ticker_is_ok(self, db, _enable_health):
        """Cancels have no ticker; ``ticker=None`` must not raise."""
        venue_health.record_rate_limit_event(
            venue="robinhood",
            ticker=None,
            source="rh_cancel",
        )
        from sqlalchemy import text
        rows = db.execute(text("""
            SELECT venue, status, ticker FROM trading_execution_events
            WHERE event_type = 'rate_limit' AND status = 'rh_cancel'
        """)).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "robinhood"
        assert rows[0][2] is None

    def test_never_raises_on_db_failure(self, monkeypatch):
        """A bookkeeping failure must not bubble up into the caller.

        Headline guarantee: the rate-limit recorder is wrapped in
        try/except so a DB hiccup during an order-placement retry storm
        can never take down the order path itself.
        """
        # Point SessionLocal at something that raises on open.
        import app.db as app_db

        def _boom():  # pragma: no cover - we assert it gets called, but raises
            raise RuntimeError("simulated db outage")

        monkeypatch.setattr(app_db, "SessionLocal", _boom, raising=False)

        # Must return None without raising.
        result = venue_health.record_rate_limit_event(
            venue="coinbase", ticker="BTC-USD", source="cb_place_market",
        )
        assert result is None

    def test_feeds_back_into_summary(self, db, _enable_health):
        """End-to-end: record 3 rate-limit events, then the summary's
        ``n_rate_limits`` reflects them and the breaker trips.

        This is the full round-trip: adapter → recorder → DB →
        summarize → gate. The entire feedback loop needs to work for a
        rate-limit storm to self-protect.
        """
        for _ in range(3):
            venue_health.record_rate_limit_event(
                venue="coinbase", ticker="BTC-USD", source="cb_place_market",
            )
        # Expire any in-session cache.
        db.commit()

        summary = venue_health.summarize_venue(db, venue="coinbase")
        # 3 rate_limit rows, 0 fills → 100% "error" rate → degraded.
        assert summary["n_rate_limits"] == 3
        assert summary["status"] == "degraded"
        assert venue_health.is_venue_degraded(db, venue="coinbase") is True


# ── Percentile helper ────────────────────────────────────────────────────


class TestPercentileHelper:
    def test_empty_returns_none(self):
        assert venue_health._percentile([], 0.5) is None

    def test_single_value(self):
        assert venue_health._percentile([42.0], 0.5) == 42.0
        assert venue_health._percentile([42.0], 0.95) == 42.0

    def test_monotonic_over_quantile(self):
        """P50 <= P95 for any non-empty series."""
        vals = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]
        p50 = venue_health._percentile(vals, 0.50)
        p95 = venue_health._percentile(vals, 0.95)
        assert p50 is not None and p95 is not None
        assert p50 <= p95

    def test_p95_picks_near_top(self):
        """10 values, index at 0.95 * 9 = 8.55 → rounded 9 → the max."""
        vals = list(range(1, 11))  # 1..10
        p95 = venue_health._percentile([float(v) for v in vals], 0.95)
        # round(0.95 * 9) = round(8.55) = 9 (banker's rounding or half-up both → 9).
        assert p95 == 10.0  # index 9 → 10


__all__: list[str] = []  # tests/ is not an import target
