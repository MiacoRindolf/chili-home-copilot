"""Regression tests for the emit_short_alerts gate on MomentumScanner.

Background. 2026-05-16 ARCHITECT-FLAG: 2,546 of 6,595 alerts/24h (39%)
were imbalance_short, and 100% of them were rejected by the executor
with `short_unsupported_in_spot` because the only fast-path venue
today is Coinbase spot. The gate stops the scanner from emitting
imbalance_short when emit_short_alerts is False, saving the DB insert
and executor decision-log write upstream.

Tests below verify the gate is correct in both directions and that
backwards-compat is preserved (default ctor kwarg = True so existing
fixtures continue to emit both directions).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.trading.fast_path.scanner import (
    IMBALANCE_LONG_THRESHOLD,
    IMBALANCE_SHORT_THRESHOLD,
    MomentumScanner,
)


def _make_book(*, imbalance: float, ticker: str = "BTC-USD") -> dict:
    """Minimal book dict matching the shape MomentumScanner.on_book_emit reads."""
    return {
        "ticker": ticker,
        "imbalance": imbalance,
        "spread_bps": 1.0,
        "best_bid": 100.0,
        "best_ask": 100.01,
        "snapshot_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }


def test_emit_short_alerts_default_true_preserves_backwards_compat():
    """Existing fixtures that do ``MomentumScanner()`` (no kwarg) must
    keep emitting both directions. Default ctor kwarg = True."""
    s = MomentumScanner()
    assert s._emit_short_alerts is True
    # Strongly short imbalance (below the threshold) should fire.
    book = _make_book(imbalance=IMBALANCE_SHORT_THRESHOLD - 0.01)
    alerts = s.on_book_emit(book, now_monotonic=0.0)
    assert any(a["alert_type"] == "imbalance_short" for a in alerts)
    assert s.fired_imbalance_short == 1
    assert s.suppressed_short_alert_disabled == 0


def test_zero_imbalance_is_preserved_as_short_pressure():
    s = MomentumScanner()

    alerts = s.on_book_emit(_make_book(imbalance=0.0), now_monotonic=0.0)

    short_alert = next(a for a in alerts if a["alert_type"] == "imbalance_short")
    assert short_alert["signal_score"] == pytest.approx(1.0)
    assert short_alert["features"]["imbalance"] == 0.0
    assert s.fired_imbalance_short == 1


def test_emit_short_alerts_false_suppresses_imbalance_short():
    """Scanner with emit_short_alerts=False must NOT emit imbalance_short
    even on a strong short imbalance. Counter
    suppressed_short_alert_disabled increments instead."""
    s = MomentumScanner(emit_short_alerts=False)
    assert s._emit_short_alerts is False
    book = _make_book(imbalance=IMBALANCE_SHORT_THRESHOLD - 0.01)
    alerts = s.on_book_emit(book, now_monotonic=0.0)
    assert not any(a["alert_type"] == "imbalance_short" for a in alerts)
    assert s.fired_imbalance_short == 0
    assert s.suppressed_short_alert_disabled == 1


def test_emit_short_alerts_false_does_not_affect_imbalance_long():
    """The gate must be one-directional: imbalance_long still fires
    when emit_short_alerts=False."""
    s = MomentumScanner(emit_short_alerts=False)
    book = _make_book(imbalance=IMBALANCE_LONG_THRESHOLD + 0.01)
    alerts = s.on_book_emit(book, now_monotonic=0.0)
    assert any(a["alert_type"] == "imbalance_long" for a in alerts)
    assert s.fired_imbalance_long == 1
    assert s.suppressed_short_alert_disabled == 0


def test_emit_raw_imbalance_false_suppresses_long_and_short_raw_alerts():
    """Raw imbalance can be disabled while book-pressure still uses it as input."""
    s = MomentumScanner(
        emit_raw_imbalance_alerts=False,
        emit_short_alerts=True,
    )

    long_alerts = s.on_book_emit(
        _make_book(imbalance=IMBALANCE_LONG_THRESHOLD + 0.01),
        now_monotonic=0.0,
    )
    short_alerts = s.on_book_emit(
        _make_book(imbalance=IMBALANCE_SHORT_THRESHOLD - 0.01, ticker="ETH-USD"),
        now_monotonic=1.0,
    )

    assert long_alerts == []
    assert short_alerts == []
    assert s.fired_imbalance_long == 0
    assert s.fired_imbalance_short == 0
    assert s.suppressed_raw_imbalance_disabled == 2
    assert s.stats()["config"]["emit_raw_imbalance_alerts"] is False


def test_custom_imbalance_threshold_controls_emission():
    """Scanner thresholds should be runtime knobs, not hidden constants."""
    s = MomentumScanner(
        emit_short_alerts=False,
        imbalance_long_threshold=0.90,
    )
    alerts = s.on_book_emit(
        _make_book(imbalance=IMBALANCE_LONG_THRESHOLD + 0.01),
        now_monotonic=0.0,
    )
    assert alerts == []
    assert s.fired_imbalance_long == 0
    assert s.stats()["config"]["imbalance_long_threshold"] == 0.90


def test_suppressed_short_alert_disabled_surfaces_in_stats():
    """Counter must round-trip through .stats() so the supervisor
    metrics line can show the suppression rate."""
    s = MomentumScanner(emit_short_alerts=False)
    # Two short imbalances → two suppressions
    s.on_book_emit(_make_book(imbalance=0.30), now_monotonic=0.0)
    s.on_book_emit(_make_book(imbalance=0.25, ticker="ETH-USD"), now_monotonic=0.1)
    stats = s.stats()
    assert stats["suppressed_short_alert_disabled"] == 2
    assert stats["fired_imbalance_short"] == 0


def test_settings_load_default_is_false():
    """Default settings (no env var set) must have
    emit_short_alerts=False so production is gated by default."""
    import os
    from app.services.trading.fast_path.settings import load as fp_load
    # Ensure env var isn't set leftover from another test
    os.environ.pop("CHILI_FAST_PATH_EMIT_SHORT_ALERTS", None)
    os.environ.pop("CHILI_FAST_PATH_EMIT_RAW_IMBALANCE_ALERTS", None)
    cfg = fp_load()
    assert cfg.emit_short_alerts is False
    assert cfg.emit_raw_imbalance_alerts is False


def test_settings_load_env_overrides_true():
    """Operators can explicitly re-enable suppressed scanner families."""
    import os
    from app.services.trading.fast_path.settings import load as fp_load
    os.environ["CHILI_FAST_PATH_EMIT_SHORT_ALERTS"] = "true"
    os.environ["CHILI_FAST_PATH_EMIT_RAW_IMBALANCE_ALERTS"] = "true"
    try:
        cfg = fp_load()
        assert cfg.emit_short_alerts is True
        assert cfg.emit_raw_imbalance_alerts is True
    finally:
        os.environ.pop("CHILI_FAST_PATH_EMIT_SHORT_ALERTS", None)
        os.environ.pop("CHILI_FAST_PATH_EMIT_RAW_IMBALANCE_ALERTS", None)
