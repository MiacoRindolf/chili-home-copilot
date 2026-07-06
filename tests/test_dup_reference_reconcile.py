"""Dup-Reference 409 reconcile detector (BJDX sid 10484 orphan, 2026-07-06).

The agentic RH rail passes our deterministic entry cid as the order Reference ID, so
an idempotent re-submit of the SAME logical entry (ack-timeout re-place / parallel
watcher) gets ``409 {"detail":"Reference ID must be unique."}``. That 409 CONFIRMS the
first submit is already live at the venue — it must NOT terminalize the session to
live_error (which orphans the filled position, unmanaged). ``_is_dup_reference_reject``
is the narrow detector that routes such a reject into the reconcile-and-adopt branch.
"""

from app.services.trading.momentum_neural.live_runner import _is_dup_reference_reject


# The EXACT error string that stranded BJDX session 10484 (from
# trading_automation_events.payload_json->result->error, 2026-07-06 10:38:37 ET).
BJDX_409 = (
    "MCP tool 'place_equity_order' returned isError: API error 409: "
    '{"detail":"Reference ID must be unique."}'
)


def test_bjdx_live_incident_string_is_detected():
    assert _is_dup_reference_reject(BJDX_409) is True


def test_detects_phrase_variants():
    assert _is_dup_reference_reject("Reference ID must be unique.") is True
    assert _is_dup_reference_reject("reference id must be unique") is True
    assert _is_dup_reference_reject("API error 409: reference must be unique") is True


def test_ignores_unrelated_rejects():
    # A generic 409 without the reference-uniqueness phrase must NOT trip it — those
    # ARE real place failures that should still terminalize (no silent orphan-adopt).
    assert _is_dup_reference_reject("API error 409: rate limited") is False
    assert _is_dup_reference_reject("EQUITY_SUITABILITY: not tradable") is False
    assert _is_dup_reference_reject("401 not available for agentic trading") is False
    # An unrelated 'must be unique' (e.g. some other field) is not a Reference-ID 409.
    assert _is_dup_reference_reject("client_order_id must be unique") is False


def test_empty_and_none_are_false():
    assert _is_dup_reference_reject(None) is False
    assert _is_dup_reference_reject("") is False
