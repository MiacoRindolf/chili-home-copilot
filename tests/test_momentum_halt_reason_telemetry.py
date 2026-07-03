"""WAVE-4 ITEM-2 (R7) — halt-resume reason-overwrite + per-detector reject telemetry.

(a) After ``halt_resume_dip_trigger`` REJECTS, the ladder UNCONDITIONALLY re-runs
    ``momentum_pullback_trigger``, which clobbers ``_trigger_reason``/``_pb_debug`` — the
    resume-dip's actionable reject was silently lost. ``_preserve_resume_dip_reason`` keeps
    the pullback reason ONLY when it produced a DIFFERENT actionable result (a fire or a
    tick-armed wait); otherwise it restores the resume-dip reject.

(b) The ``live_entry_trigger_wait`` event carried ONE reason; a compact per-detector reject
    map (``detector_rejects``: detector -> reason) makes quiet detectors tunable.

Telemetry-only — no trade behavior changes. These tests pin the pure decision helper +
the reject-map payload shape.
"""

from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.live_runner import _preserve_resume_dip_reason
from app.services.trading.momentum_neural.entry_gates import TICK_ARMED_WAIT_REASONS


# --------------------------------------------------------------------------- #
# (a) resume-dip reject preservation                                          #
# --------------------------------------------------------------------------- #
def test_resume_dip_reject_preserved_when_pullback_is_another_inert_wait():
    # resume-dip rejected with its OWN reason; the pullback re-run produced another inert
    # wait ("trigger_wait" is NOT tick-armed) -> restore the resume-dip reject.
    out = _preserve_resume_dip_reason(
        trigger_ok=False,
        pullback_reason="trigger_wait",
        resume_dip_reject="halt_resume_dip_wait_reclaim",
    )
    assert out == "halt_resume_dip_wait_reclaim"


def test_pullback_kept_when_it_fires():
    # The pullback FIRED (a different, actionable result) -> keep its reason.
    out = _preserve_resume_dip_reason(
        trigger_ok=True,
        pullback_reason="pullback_break_ok",
        resume_dip_reject="halt_resume_dip_wait_reclaim",
    )
    assert out == "pullback_break_ok"


def test_pullback_kept_when_tick_armed_wait():
    # The pullback produced a TICK-ARMED wait the event loop can act on -> keep it (a
    # different actionable result than the resume-dip's inert reject).
    assert "waiting_for_break" in TICK_ARMED_WAIT_REASONS
    out = _preserve_resume_dip_reason(
        trigger_ok=False,
        pullback_reason="waiting_for_break",
        resume_dip_reject="halt_resume_dip_wait_reclaim",
    )
    assert out == "waiting_for_break"


def test_no_resume_dip_is_byte_identical():
    # No resumed halt -> resume_dip_reject is None -> the pullback reason is returned
    # unchanged (byte-identical to the pre-R7 behavior).
    out = _preserve_resume_dip_reason(
        trigger_ok=False,
        pullback_reason="trigger_wait",
        resume_dip_reject=None,
    )
    assert out == "trigger_wait"


def test_error_reject_preserved():
    # The resume-dip trigger raised -> its reject is "halt_resume_dip_error"; a later inert
    # pullback wait must not bury it.
    out = _preserve_resume_dip_reason(
        trigger_ok=False,
        pullback_reason="no_pullback_structure",
        resume_dip_reject="halt_resume_dip_error",
    )
    assert out == "halt_resume_dip_error"


# --------------------------------------------------------------------------- #
# (b) per-detector reject map — payload shape                                 #
# --------------------------------------------------------------------------- #
def _build_wait_payload(trigger_reason, reject_map):
    """Mirror the emit-site construction (live_runner ~7550): reason + optional map."""
    payload = {"reason": trigger_reason}
    if reject_map:
        payload["detector_rejects"] = dict(reject_map)
    return payload


def test_reject_map_carries_both_detectors():
    # Both the resume-dip AND the pullback rejected -> the map carries BOTH, and the
    # primary reason is the preserved resume-dip reject.
    reject_map = {
        "halt_resume_dip": "halt_resume_dip_wait_reclaim",
        "momentum_pullback": "trigger_wait",
    }
    primary = _preserve_resume_dip_reason(
        trigger_ok=False,
        pullback_reason="trigger_wait",
        resume_dip_reject="halt_resume_dip_wait_reclaim",
    )
    payload = _build_wait_payload(primary, reject_map)
    assert payload["reason"] == "halt_resume_dip_wait_reclaim"
    assert payload["detector_rejects"]["halt_resume_dip"] == "halt_resume_dip_wait_reclaim"
    assert payload["detector_rejects"]["momentum_pullback"] == "trigger_wait"


def test_empty_reject_map_omits_key():
    # No detector rejects captured (e.g. score_only path) -> the payload has only `reason`.
    payload = _build_wait_payload("score_only", {})
    assert payload == {"reason": "score_only"}
    assert "detector_rejects" not in payload


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
