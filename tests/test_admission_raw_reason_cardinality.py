"""The admission census must not crowd itself out.

2026-08-11, r165: `ingress_rejected_sequence_{N}` embeds a monotonically increasing
sequence number, so every rejection minted a DISTINCT key. That saturated the 8-key
sample cap with eight one-hit variants (_107, _163, _218, _302, _402, _1119, ...)
summing to 10, against a fallback bucket of 16 -- six rejections vanished unseen,
and the eight that survived all said the same thing.

A census whose cardinality is driven by an event counter degrades to noise exactly
when volume is highest, which is when it matters most.
"""

import re

import pytest

from app.services.trading.momentum_neural.live_runner_loop import (
    _CAPTURED_PAPER_ADMISSION_RAW_SAMPLE_KEYS,
    _DIGIT_RUN_RE,
)


def _collapse(raw: str) -> str:
    """The normalisation the sampler applies, isolated."""
    clean = "".join(ch if ch.isprintable() else " " for ch in str(raw))
    clean = " ".join(clean.split())
    return _DIGIT_RUN_RE.sub("<n>", clean)


def test_sequence_numbered_reasons_collapse_to_one_key():
    """THE regression: eight variants of one reason must occupy one slot."""
    observed = [
        f"capture lifecycle is already noncertifiable: ingress_rejected_sequence_{n}"
        for n in (107, 163, 218, 302, 402, 1119, 77, 9001)
    ]
    collapsed = {_collapse(r) for r in observed}

    assert len(collapsed) == 1, collapsed
    assert len(collapsed) < _CAPTURED_PAPER_ADMISSION_RAW_SAMPLE_KEYS


def test_the_reason_survives_the_collapse():
    """Collapsing must lose the counter and keep the diagnosis."""
    out = _collapse("capture lifecycle is already noncertifiable: ingress_rejected_sequence_402")
    assert "ingress_rejected_sequence" in out
    assert "402" not in out


def test_distinct_reasons_stay_distinct():
    """The point is to merge one reason's variants, not to merge reasons."""
    distinct = {
        _collapse("ingress_rejected_sequence_163"),
        _collapse("capture_resource_pressure_sample_stale"),
        _collapse("provider first frame was not durably admitted"),
        _collapse("capture startup identity evidence was not durably admitted"),
    }
    assert len(distinct) == 4


def test_single_digits_are_preserved():
    """Single digits carry meaning; only identifier-width runs are noise."""
    assert _collapse("l2 depth 0 for 3 symbols") == "l2 depth 0 for 3 symbols"


def test_two_digit_runs_are_collapsed():
    """Two is the floor -- any counter that can saturate the cap is wider."""
    assert _collapse("attempt 42") == "attempt <n>"


def test_eight_real_reasons_still_fit_the_cap():
    """The cap is only a problem when one reason forges many keys."""
    reasons = [
        "capture_resource_pressure_sample_stale",
        "captured_paper_admission_rejected",
        "l1_pretrigger_promotion_unavailable",
        "iqfeed_notify_stale",
        "initial_recovery_symbol_inventory_ambiguous",
        "capture_ingress_closed",
        "capture_event_exceeds_queue_byte_budget",
        "ingress_rejected_sequence_1",
    ]
    assert len({_collapse(r) for r in reasons}) <= _CAPTURED_PAPER_ADMISSION_RAW_SAMPLE_KEYS


@pytest.mark.parametrize("raw", ["", "   ", "\x00\x01"])
def test_degenerate_input_is_still_handled(raw):
    """The sampler runs on a rejection path; it must never raise."""
    assert isinstance(_collapse(raw), str)


def test_regex_is_anchored_on_width_not_position():
    """Guards against a future 'only strip trailing digits' simplification."""
    assert _collapse("seq_402_of_1119") == "seq_<n>_of_<n>"
    assert re.search(r"\d{2,}", _collapse("seq_402_of_1119")) is None
