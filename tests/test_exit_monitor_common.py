"""Tests for f-exit-monitor-quote-guard-unification.

Pins the two helpers in ``app/services/trading/_exit_monitor_common.py``
that all three exit-monitor lanes (equity / crypto / options) now
share:

  - ``is_implausible_quote(px, entry)``
  - ``should_consult_monitor_after_refusal(reason, abstained_implausible=False)``

Helper-level only (no DB / no broker / no fixtures). Sub-millisecond.
"""
from __future__ import annotations

from app.services.trading._exit_monitor_common import (
    IMPLAUSIBLE_QUOTE_RATIO_HIGH,
    IMPLAUSIBLE_QUOTE_RATIO_LOW,
    is_implausible_quote,
    should_consult_monitor_after_refusal,
)


# ---------------------------------------------------------------------------
# is_implausible_quote
# ---------------------------------------------------------------------------

def test_is_implausible_quote_below_threshold():
    """ratio = 0.5 / 10.0 = 0.05 < 0.1 -> True."""
    assert is_implausible_quote(0.5, 10.0) is True


def test_is_implausible_quote_above_threshold():
    """ratio = 200 / 10 = 20 > 10 -> True."""
    assert is_implausible_quote(200.0, 10.0) is True


def test_is_implausible_quote_normal_range():
    """ratio = 11 / 10 = 1.1 (well within (0.1, 10)) -> False."""
    assert is_implausible_quote(11.0, 10.0) is False


def test_is_implausible_quote_zero_entry():
    """No anchor -> caller's responsibility; helper returns False."""
    assert is_implausible_quote(10.0, 0.0) is False
    assert is_implausible_quote(10.0, None) is False  # type: ignore[arg-type]


def test_is_implausible_quote_negative_px():
    """No usable px -> caller's responsibility; helper returns False."""
    assert is_implausible_quote(-5.0, 10.0) is False
    assert is_implausible_quote(0.0, 10.0) is False
    assert is_implausible_quote(None, 10.0) is False  # type: ignore[arg-type]


def test_is_implausible_quote_constants_match_documentation():
    """Pin the constant values so future drift is loud."""
    assert IMPLAUSIBLE_QUOTE_RATIO_LOW == 0.1
    assert IMPLAUSIBLE_QUOTE_RATIO_HIGH == 10.0


# ---------------------------------------------------------------------------
# should_consult_monitor_after_refusal
# ---------------------------------------------------------------------------

def test_should_consult_monitor_after_implausible_prefix():
    """Crypto's prefix-match contract: refusal reason starting with
    ``no_trigger:implausible_quote`` blocks consultation."""
    reason = (
        "no_trigger:implausible_quote px=0.5 entry=10 "
        "ratio=0.0500 (rejected; refusing to act on data error)"
    )
    assert should_consult_monitor_after_refusal(reason) is False


def test_should_consult_monitor_after_no_trigger():
    """Plain ``no_trigger`` is "no exit signal," NOT "we don't trust
    the feed." Consultation IS permitted -- the LLM is the secondary
    signal."""
    assert should_consult_monitor_after_refusal("no_trigger") is True


def test_should_consult_monitor_after_no_quote():
    """``no_quote`` is "px=0," NOT a data-quality refusal. Consultation
    IS permitted (though crypto's lane returns earlier on no-quote, so
    this case is mostly defensive)."""
    assert should_consult_monitor_after_refusal("no_quote") is True


def test_should_consult_monitor_after_options_abstain_flag():
    """Options' boolean signal: abstained_implausible=True blocks
    consultation regardless of reason string (which is None for
    options' tuple-return shape)."""
    assert (
        should_consult_monitor_after_refusal(None, abstained_implausible=True)
        is False
    )


def test_should_consult_monitor_after_normal_no_signal():
    """reason=None and abstained=False -> consult (LLM is fallback)."""
    assert (
        should_consult_monitor_after_refusal(None, abstained_implausible=False)
        is True
    )
    # Default abstained_implausible is False; ensure that path also
    # consults when reason is None.
    assert should_consult_monitor_after_refusal(None) is True
