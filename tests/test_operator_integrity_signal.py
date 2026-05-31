from __future__ import annotations

from app.routers.trading_sub.operator import _annotate_pattern_integrity_signals


def test_integrity_signal_uses_directional_sample_floor() -> None:
    patterns = [
        {
            "scan_pattern_id": 42,
            "live_closed_trades": 8,
            "live_win_sample_n": 2,
            "live_win_rate_pct": 0.0,
            "research_oos_win_rate_pct": 80.0,
        }
    ]

    _annotate_pattern_integrity_signals(patterns)

    assert patterns[0]["integrity_signal"] == "insufficient_data"


def test_integrity_signal_falls_back_to_closed_count_for_legacy_payloads() -> None:
    patterns = [
        {
            "scan_pattern_id": 43,
            "live_closed_trades": 8,
            "live_win_rate_pct": 0.0,
            "research_oos_win_rate_pct": 80.0,
        }
    ]

    _annotate_pattern_integrity_signals(patterns)

    assert patterns[0]["integrity_signal"] == "degraded"


def test_integrity_signal_treats_bad_numeric_fields_as_insufficient_data() -> None:
    patterns = [
        {
            "scan_pattern_id": 44,
            "live_closed_trades": 8,
            "live_win_sample_n": 8,
            "live_win_rate_pct": "not-a-number",
            "research_oos_win_rate_pct": 80.0,
        },
        {
            "scan_pattern_id": 45,
            "live_closed_trades": 8,
            "live_win_sample_n": 8,
            "live_win_rate_pct": 40.0,
            "research_oos_win_rate_pct": "not-a-number",
            "research_win_rate_pct": None,
        },
    ]

    _annotate_pattern_integrity_signals(patterns)

    assert [p["integrity_signal"] for p in patterns] == [
        "insufficient_data",
        "insufficient_data",
    ]
