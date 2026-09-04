"""The adaptive-risk block event must say WHAT failed, not only that something did.

MEASURED 2026-09-04 (SDOT 2026-06-26 Ross bench, alpaca_spot): 26 entry attempts, 26
``live_entry_adaptive_risk_blocked reason=adaptive_risk_builder_source_invalid`` with no
other field. The except arm folds AdaptiveRiskBuilderError, TypeError and ValueError into
the same label and drops the builder's own detail argument, so a ``float(None)`` on a
missing structural stop is indistinguishable from a corrupt capture source. DB-free.
"""
from __future__ import annotations

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.adaptive_risk_request_builder import (
    AdaptiveRiskBuilderError,
)


def test_builder_error_keeps_its_reason_and_detail():
    exc = AdaptiveRiskBuilderError("adaptive_risk_builder_source_invalid", "account_scope_missing")
    p = lr._adaptive_risk_blocker_payload(exc)
    assert p["reason"] == "adaptive_risk_builder_source_invalid"
    assert p["error_type"] == "AdaptiveRiskBuilderError"
    assert p["detail"] == "account_scope_missing"


def test_builder_error_without_detail_has_none_detail():
    exc = AdaptiveRiskBuilderError("adaptive_risk_structural_stop_missing")
    p = lr._adaptive_risk_blocker_payload(exc)
    assert p["reason"] == "adaptive_risk_structural_stop_missing"
    assert p["detail"] is None


def test_a_type_error_is_named_as_a_type_error_not_a_source_problem():
    try:
        float(None)  # type: ignore[arg-type]
    except TypeError as exc:
        p = lr._adaptive_risk_blocker_payload(exc)
    assert p["reason"] == "adaptive_risk_builder_source_invalid"  # label unchanged for consumers
    assert p["error_type"] == "TypeError"
    assert "float()" in p["detail"] or "NoneType" in p["detail"]


def test_the_emit_carries_the_same_payload_shape():
    """The event payload is the blocker payload verbatim (plus nothing hidden)."""
    p = lr._adaptive_risk_blocker_payload(ValueError("could not convert string to float: 'x'"))
    assert set(p) == {"reason", "error_type", "detail"}
