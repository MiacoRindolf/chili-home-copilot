"""Profit-taking exits fail open sa freshness seams (#1263).

SINUKAT (SSM 2026-09-01, session 19315): 31 sunod-sunod na
`live_exit_deferred_final_bbo` na reason=`target`; ang pinakamaliit na
nasukat na quote age ay 2.165s laban sa 2.0s ceiling — KULANG NG 165
MILLISECONDS. Sa 7 araw: 77 target-deferrals at `live_partial_exit_filled`
= 0 — WALANG kahit isang share na naibenta sa itaas ng entry ng anumang
partial path mula 2026-08-01.

Ang #1254/#1255/#1258/#1259 ay nagbukas para sa mga exit na PUMUPUTOL NG
TALO; ang mga exit na KUMUKUHA NG TUBO ay naiwang nakakulong. Pareho silang
naglalabas ng panganib — pareho dapat ang karapatan sa stand-in pricing.

Runnable: pytest tests/test_profit_exit_fail_open.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.live_runner import (
    _FRESHNESS_FAIL_OPEN_EXIT_REASONS,
    _captured_exit_bbo_ceiling,
    _exit_reason_fails_open,
)


def test_protective_reasons_still_fail_open():
    """Ang dating #1254/#1255 na saklaw ay buo."""
    for r in ("stop", "bailout", "deadman_stop", "operator_flatten",
              "kill_switch_flatten"):
        assert _exit_reason_fails_open(r) is True, r


def test_profit_taking_reasons_now_fail_open():
    """ANG BAGO: ang exit na kumukuha ng tubo ay hindi na nakakulong."""
    for r in ("target", "scale_out_target", "scale_out_limit",
              "momentum_break_stop", "trail_stop", "grind_trail_stop"):
        assert _exit_reason_fails_open(r) is True, r


def test_stop_class_tokens_still_work():
    """Ang token-based na classifier ay dumadaan pa rin (decorated reasons)."""
    assert _exit_reason_fails_open("stop_broker_zero_reconcile") is True
    assert _exit_reason_fails_open("trail_stop_retry_cap_broker_zero") is True


def test_unknown_reason_does_not_fail_open():
    """Hindi blanket — ang hindi kilalang dahilan ay mahigpit pa rin."""
    assert _exit_reason_fails_open("some_random_reason") is False
    assert _exit_reason_fails_open("") is False
    assert _exit_reason_fails_open(None) is False


def test_captured_paper_ceiling_raised_for_target():
    """Ang staged captured-paper ceiling ay tumataas din para sa `target`."""
    base = _captured_exit_bbo_ceiling("some_random_reason", 2.0)
    prof = _captured_exit_bbo_ceiling("target", 2.0)
    assert base == 2.0
    assert prof > 100.0   # emergency stand-in max age


def test_the_exact_ssm_miss():
    """Ang eksaktong 165ms na palya: age 2.165s, reason=target."""
    ceiling = _captured_exit_bbo_ceiling("target", 2.0)
    assert 2.165 <= ceiling, "ang target exit ay dapat nang dumaan sa 2.165s"


def test_reason_set_is_explicit():
    """Ang listahan ay tahasan — walang pattern matching na makakalusot."""
    assert "target" in _FRESHNESS_FAIL_OPEN_EXIT_REASONS
    assert "stop" in _FRESHNESS_FAIL_OPEN_EXIT_REASONS
    assert "some_random_reason" not in _FRESHNESS_FAIL_OPEN_EXIT_REASONS
